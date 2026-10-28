"""
data_loader.py
==============
Reads the 1C exports (Excel or CSV) into *validated* records.

1C exports are messy: column captions drift between operators, there are blank
spacer rows, merged headers, ``NaN`` cells and stray totals. This module is the
firewall:

  * resolves real column names against a list of accepted aliases,
  * raises :class:`DataValidationError` (fail-fast) if a *required* column is
    absent — so we never run half-blind,
  * skips individual junk rows with a WARNING instead of crashing,
  * returns typed pydantic records (see ``models.py``).

The stock export is assumed "long" (one row per product *per* location). A
"wide" layout (one column per store) is also supported via ``melt``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence

import pandas as pd
from pydantic import ValidationError

from logger import get_logger
from models import RatingRecord, ReadyCutRecord, StockRecord

log = get_logger("data_loader")


class DataValidationError(Exception):
    """Raised when source data is structurally unusable (missing columns…)."""


# Accepted header aliases (normalised: lower-cased, stripped). Extend as 1C
# templates evolve — this is cheaper than re-mapping every export by hand.
_RATING_ALIASES = {
    "name": ["номенклатура", "товар", "наименование", "name", "позиция"],
    "revenue": ["выручка", "сумма", "сумма продаж", "revenue", "оборот"],
    "rank": ["рейтинг", "ранг", "место", "rank", "#", "№"],
}
_STOCK_ALIASES = {
    "name": ["номенклатура", "товар", "наименование", "name", "позиция"],
    "store": ["склад", "магазин", "точка", "store", "подразделение"],
    "qty": ["количество", "остаток", "кол-во", "qty", "наличие"],
}
_READY_CUT_ALIASES = {
    "name": ["номенклатура", "товар", "наименование", "name", "позиция"],
    "qty": ["количество", "кол-во", "qty", "крой", "раскроено"],
}


# --------------------------------------------------------------------------- #
# Low-level file reading
# --------------------------------------------------------------------------- #
def _read_table(path: Path) -> pd.DataFrame:
    """Read .xlsx/.xls/.csv into a DataFrame, with clear errors."""
    if not path.exists():
        raise DataValidationError(f"Input file not found: {path}")
    try:
        suffix = path.suffix.lower()
        if suffix in {".xlsx", ".xls"}:
            df = pd.read_excel(path, dtype=object)
        elif suffix == ".csv":
            # 1C commonly emits cp1251 / ';' — try a few combinations.
            df = _read_csv_resilient(path)
        else:
            raise DataValidationError(f"Unsupported file type: {path.suffix}")
    except DataValidationError:
        raise
    except Exception as exc:  # openpyxl / parser explosions
        raise DataValidationError(f"Failed to read {path.name}: {exc}") from exc

    if df.empty:
        raise DataValidationError(f"{path.name} contains no rows.")
    # Normalise headers immediately.
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def _read_csv_resilient(path: Path) -> pd.DataFrame:
    last_exc: Exception | None = None
    for sep in (";", ",", "\t"):
        for enc in ("utf-8-sig", "cp1251", "utf-8"):
            try:
                df = pd.read_csv(path, sep=sep, encoding=enc, dtype=object)
                if df.shape[1] > 1:  # a sane parse has >1 column
                    return df
            except Exception as exc:  # noqa: BLE001 - we try the next combo
                last_exc = exc
    raise DataValidationError(
        f"Could not parse CSV {path.name} with common separators/encodings: "
        f"{last_exc}"
    )


def _resolve_columns(
    df: pd.DataFrame, aliases: Dict[str, Sequence[str]], required: Sequence[str]
) -> Dict[str, str]:
    """Map our canonical field -> the actual column present in the file."""
    resolved: Dict[str, str] = {}
    present = set(df.columns)
    for field, candidates in aliases.items():
        for cand in candidates:
            if cand in present:
                resolved[field] = cand
                break
    missing = [f for f in required if f not in resolved]
    if missing:
        raise DataValidationError(
            f"Missing required column(s) {missing}. "
            f"Columns present: {sorted(present)}"
        )
    return resolved


def _to_float(value, *, default: float | None = 0.0) -> float | None:
    """Best-effort numeric coercion. Handles '1 234,50', '—', '', NaN."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    s = str(value).strip()
    if s in {"", "-", "—", "–", "nan", "None"}:
        return default
    s = s.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None  # signals "present but garbage" to the caller


# --------------------------------------------------------------------------- #
# Public loaders
# --------------------------------------------------------------------------- #
def load_ratings(path: Path) -> List[RatingRecord]:
    df = _read_table(path)
    cols = _resolve_columns(df, _RATING_ALIASES, required=["name", "revenue"])
    records: List[RatingRecord] = []
    skipped = 0
    for idx, row in df.iterrows():
        name = row.get(cols["name"])
        if name is None or str(name).strip() == "" or pd.isna(name):
            skipped += 1
            continue
        revenue = _to_float(row.get(cols["revenue"]))
        if revenue is None:
            log.warning("Rating row %s: unparseable revenue %r — skipped.",
                        idx, row.get(cols["revenue"]))
            skipped += 1
            continue
        rank_val = _to_float(row.get(cols["rank"]), default=None) if "rank" in cols else None
        try:
            records.append(RatingRecord(
                raw_name=str(name),
                revenue=revenue,
                rank=int(rank_val) if rank_val is not None else None,
            ))
        except ValidationError as exc:
            log.warning("Rating row %s failed validation: %s — skipped.", idx, exc)
            skipped += 1
    log.info("Ratings: %d valid record(s), %d skipped, from %s",
             len(records), skipped, path.name)
    if not records:
        raise DataValidationError(f"No valid rating rows in {path.name}.")
    return records


def load_stock(path: Path, target_stores: Sequence[str], gp_warehouse: str) -> List[StockRecord]:
    df = _read_table(path)
    cols = _resolve_columns(df, _STOCK_ALIASES, required=["name", "qty"])

    # If there is no 'store' column, treat the file as WIDE: each known
    # location is its own column. Melt it into the long format we expect.
    if "store" not in cols:
        known = list(target_stores) + [gp_warehouse]
        store_cols = [c for c in df.columns if c in {k.lower() for k in known}]
        if not store_cols:
            raise DataValidationError(
                "Stock export has neither a 'склад/магазин' column nor any "
                f"recognised store columns ({known})."
            )
        df = df.melt(id_vars=[cols["name"]], value_vars=store_cols,
                     var_name="store", value_name=cols["qty"])
        cols["store"] = "store"
        log.info("Stock export detected as WIDE; melted %d store column(s).",
                 len(store_cols))

    records: List[StockRecord] = []
    skipped = 0
    for idx, row in df.iterrows():
        name = row.get(cols["name"])
        store = row.get(cols["store"])
        if (name is None or pd.isna(name) or str(name).strip() == ""
                or store is None or pd.isna(store) or str(store).strip() == ""):
            skipped += 1
            continue
        qty = _to_float(row.get(cols["qty"]))
        if qty is None:
            log.warning("Stock row %s: unparseable qty %r — treated as 0.",
                        idx, row.get(cols["qty"]))
            qty = 0.0
        try:
            records.append(StockRecord(raw_name=str(name), store=str(store), qty=qty))
        except ValidationError as exc:
            log.warning("Stock row %s failed validation: %s — skipped.", idx, exc)
            skipped += 1
    log.info("Stock: %d valid record(s), %d skipped, from %s",
             len(records), skipped, path.name)
    if not records:
        raise DataValidationError(f"No valid stock rows in {path.name}.")
    return records


def parse_ready_cut(rows: List[dict]) -> List[ReadyCutRecord]:
    """Validate the 'Готовый крой' rows already fetched from Google Sheets.

    ``rows`` is the list-of-dicts from gspread ``get_all_records`` — keys are the
    sheet's header row. We resolve aliases the same way as for files.
    """
    if not rows:
        log.warning("'Готовый крой' sheet is empty — sewing plan will be empty.")
        return []

    headers = {str(k).strip().lower() for k in rows[0].keys()}
    # Build a tiny DataFrame-free resolver.
    resolved: Dict[str, str] = {}
    for field, candidates in _READY_CUT_ALIASES.items():
        for cand in candidates:
            if cand in headers:
                # find the *original* key (case preserved) for lookup
                for original in rows[0].keys():
                    if str(original).strip().lower() == cand:
                        resolved[field] = original
                        break
                break
    if "name" not in resolved or "qty" not in resolved:
        raise DataValidationError(
            f"'Готовый крой' is missing required columns. Need name+qty, "
            f"saw headers: {sorted(headers)}"
        )

    records: List[ReadyCutRecord] = []
    skipped = 0
    for i, row in enumerate(rows):
        name = row.get(resolved["name"])
        if name is None or str(name).strip() == "":
            skipped += 1
            continue
        qty = _to_float(row.get(resolved["qty"]))
        if qty is None:
            qty = 0.0
        try:
            records.append(ReadyCutRecord(raw_name=str(name), qty=qty))
        except ValidationError as exc:
            log.warning("'Готовый крой' row %s failed validation: %s — skipped.",
                        i, exc)
            skipped += 1
    log.info("Ready-cut: %d valid record(s), %d skipped.", len(records), skipped)
    return records
