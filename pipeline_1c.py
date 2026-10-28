"""
pipeline_1c.py
==============
Adapter that turns the client's real 1C exports into the analyzer's inputs.

The generic ``data_loader.py`` assumes one clean header row; the 1C files are
cross-tabs with multi-row merged headers, title/subtotal rows, and a combined
"цвет, размер" field. This module handles those layouts and reconciles the two
different grains:

  * RATING is per model      (наименование + цвет, NO size)
  * STOCK  is per variant    (наименование + цвет + размер)

We rank at the model level and plan at the variant level: every in-stock size of
a high-revenue model inherits that model's priority. Models that sell well but
have zero stock everywhere (and so never appear in the stock export) are emitted
separately as "blind spots".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from analyzer import AnalysisInputs, RatingInfo
from config import Settings
from data_loader import _to_float
from logger import get_logger
from models import DistributionItem
from normalizer import ProductMatcher

log = get_logger("pipeline_1c")


@dataclass
class _Model:
    display: str
    revenue: float


@dataclass
class _Sale:
    """Units sold of one variant, with the caption the workshop will read."""

    qty: float
    display: str
    base: str = ""
    characteristic: str = ""


@dataclass
class _Variant:
    sku: str
    model_key: str
    display: str
    store_qty: Dict[str, float]
    store_qty_raw: Dict[str, float] = field(default_factory=dict)
    base: str = ""
    characteristic: str = ""


@dataclass
class LoadStats:
    rating_models: int = 0
    rating_skipped_nonpositive: int = 0
    stock_variants: int = 0
    stock_cells: int = 0
    neg_clamped: int = 0
    matched_variants: int = 0
    unmatched_variants: int = 0
    models_with_stock: int = 0
    blind_models: int = 0
    ready_cut_rows: int = 0
    issues: List[str] = field(default_factory=list)
    unmatched_examples: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# RATING (per model)
# --------------------------------------------------------------------------- #
def load_rating_models(path: Path, sheet: str, matcher: ProductMatcher
                       ) -> Tuple[Dict[str, _Model], Dict[str, float], List[str]]:
    issues: List[str] = []
    df = pd.read_excel(path, sheet_name=sheet, header=None, dtype=object)
    row0 = [("" if pd.isna(v) else str(v).strip()) for v in df.iloc[0]]
    row1 = [("" if pd.isna(v) else str(v).strip()) for v in df.iloc[1]]

    name_col, colour_col = 0, 1
    if not row0[name_col].lower().startswith("номенклатура"):
        issues.append(f"rating r0c0 expected 'Номенклатура…', got {row0[name_col]!r}")
    try:
        rev_col = row0.index("Итого") + 1            # Выручка sits right of Количество
        if not row1[rev_col].lower().startswith("выручка"):
            issues.append(f"rating r1c{rev_col} expected 'Выручка', got {row1[rev_col]!r}")
    except ValueError:
        issues.append("rating: 'Итого' group not found; defaulting revenue col=15")
        rev_col = 15

    qty_col = rev_col - 1                     # 'Количество' sits left of 'Выручка'
    if not row1[qty_col].lower().startswith("количество"):
        issues.append(f"rating r1c{qty_col} expected 'Количество', got {row1[qty_col]!r}")

    models: Dict[str, _Model] = {}
    sold: Dict[str, _Sale] = {}
    skipped_nonpos = 0
    skipped_subtotal = 0
    for i in range(2, len(df)):
        name = df.iat[i, name_col]
        if name is None or pd.isna(name):
            continue
        name = str(name).strip()
        if not name or name.lower() == "итого":
            continue
        char = df.iat[i, colour_col]
        char = "" if pd.isna(char) else str(char).strip()
        if not char:
            # Per-model subtotal row: its revenue is the sum of the variant rows
            # below it, which we aggregate ourselves. Counting it too would both
            # double the revenue and create a colourless key the stock side
            # (always наименование+цвет) can never match.
            skipped_subtotal += 1
            continue
        # 'Характеристика' here is "цвет, размер" — same field the stock export
        # carries, so it must be split the same way. Only the colour belongs in
        # the model key; the size lives at the variant grain.
        colour, size = matcher.split_characteristic(char)
        rev = _to_float(df.iat[i, rev_col], default=None)
        if rev is None:
            continue
        if rev <= 0:
            skipped_nonpos += 1
            continue
        # Sales are recorded per VARIANT (size included) — that is the grain the
        # "sales" planning mode replenishes at.
        qty = _to_float(df.iat[i, qty_col], default=0.0) or 0.0
        if qty > 0:
            v_sku = matcher.match_structured(name, colour, size).sku
            prev = sold.get(v_sku)
            sold[v_sku] = _Sale(qty=(prev.qty if prev else 0.0) + qty,
                                display=f"{name}, {char}",
                                base=name, characteristic=char)
        m = matcher.match_structured(name, colour, None)
        existing = models.get(m.name)
        if existing is None:
            models[m.name] = _Model(display=f"{name}, {colour}".strip(", "),
                                    revenue=rev)
        else:
            existing.revenue += rev          # roll the sizes up into the model
    log.info("Rating: %d model(s), %d variant(s) with sales (%d unit(s)), "
             "%d subtotal row(s) skipped, %d non-positive skipped (rev col=%d).",
             len(models), len(sold), int(sum(s.qty for s in sold.values())),
             skipped_subtotal, skipped_nonpos, rev_col)
    issues.append(f"__nonpos__={skipped_nonpos}")
    return models, sold, issues


# --------------------------------------------------------------------------- #
# STOCK (per variant)
# --------------------------------------------------------------------------- #
def list_warehouses(path: Path, sheet: str) -> List[str]:
    """Every warehouse column present in a 1C stock export, in sheet order.

    Lets the UI offer the real captions instead of making the manager retype
    them — a typo there silently drops a whole location from the plan.
    """
    df = pd.read_excel(path, sheet_name=sheet, header=None, dtype=object, nrows=4)
    if len(df) < 4:
        return []
    row = [("" if pd.isna(v) else str(v).strip()) for v in df.iloc[3]]
    # Real warehouses start right of the 'Итого по всем складам' total; to its
    # left sit grouping captions like 'Организация', which are not locations.
    start = next((j + 1 for j, v in enumerate(row) if v.startswith("Итого")), 0)
    seen, out = set(), []
    for name in row[start:]:
        if not name or name.startswith("Итого") or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def load_stock_variants(path: Path, sheet: str, matcher: ProductMatcher,
                        cfg: Settings) -> Tuple[List[_Variant], LoadStats]:
    stats = LoadStats()
    df = pd.read_excel(path, sheet_name=sheet, header=None, dtype=object)
    h_stores = [("" if pd.isna(v) else str(v).strip()) for v in df.iloc[3]]
    h_fields = [("" if pd.isna(v) else str(v).strip()) for v in df.iloc[4]]

    # Every location the engine may consult: the replenished points AND the
    # service warehouses whose stock offsets demand. Reading only the former
    # silently zeroed химчистка/ремонт/примерка and inflated production.
    wanted = set(cfg.target_stores) | set(cfg.stock_only_stores)
    col_for: Dict[str, int] = {}
    for j, v in enumerate(h_stores):
        if v in wanted:
            col_for[v] = j
        elif "Склад ГП" in v:                         # 'ЕСклад ГП' / 'Е Склад ГП'
            col_for[cfg.finished_goods_warehouse] = j
    # Raw per-warehouse dump: only real locations, i.e. everything right of the
    # grand total. Left of it sit captions like 'Организация', which hold text.
    _first = next((j + 1 for j, v in enumerate(h_stores) if v.startswith("Итого")), 0)
    all_store_col: Dict[str, int] = {v: j for j, v in enumerate(h_stores)
                                     if v and j >= _first}
    name_col = 0
    char_col = next((j for j, v in enumerate(h_fields)
                     if v.lower().startswith("характеристика")), 3)
    for need in sorted(wanted) + [cfg.finished_goods_warehouse]:
        if need not in col_for:
            stats.issues.append(f"stock: store column not found: {need!r}")

    variants: List[_Variant] = []
    for i in range(5, len(df)):
        name = df.iat[i, name_col]
        char = df.iat[i, char_col]
        if name is None or pd.isna(name):
            continue
        name = str(name).strip()
        char = "" if (char is None or pd.isna(char)) else str(char).strip()
        if not char or name.lower() == "итого":       # org/subtotal/grand-total rows
            continue
        colour, size = matcher.split_characteristic(char)
        m = matcher.match_structured(name, colour, size)
        store_qty: Dict[str, float] = {}
        for store, col in col_for.items():
            qty = _to_float(df.iat[i, col], default=0.0) or 0.0
            if qty < 0:
                stats.neg_clamped += 1
                qty = 0.0
            store_qty[store] = qty
            stats.stock_cells += 1
        store_qty_raw: Dict[str, float] = {
            store: _to_float(df.iat[i, col], default=0.0) or 0.0
            for store, col in all_store_col.items()
        }
        variants.append(_Variant(sku=m.sku, model_key=m.name,
                                 display=f"{name}, {char}", store_qty=store_qty,
                                 store_qty_raw=store_qty_raw,
                                 base=name, characteristic=char))
    stats.stock_variants = len(variants)
    log.info("Stock: %d variant(s), %d cells, neg→0=%d, store cols=%s.",
             len(variants), stats.stock_cells, stats.neg_clamped, col_for)
    return variants, stats


# --------------------------------------------------------------------------- #
# READY-CUT ("Готовый крой"), when the tab exists
# --------------------------------------------------------------------------- #
def parse_ready_cut_1c(raw_rows: List[list], matcher: ProductMatcher
                       ) -> Dict[str, float]:
    """Parse 'Готовый крой' rows (raw values) into {variant_sku: qty}.

    Tolerant of layout: finds a name column, an optional 'характеристика'
    column (else colour+size columns), and a quantity column by header text.
    Returns {} for an empty/absent sheet.
    """
    if not raw_rows or len(raw_rows) < 2:
        return {}
    header = [str(c).strip().lower() for c in raw_rows[0]]

    def find(*names):
        for n in names:
            for j, h in enumerate(header):
                if h.startswith(n):
                    return j
        return None

    c_name = find("номенклатура", "товар", "наимен", "модель")
    c_char = find("характеристика")
    c_colour = find("цвет")
    c_size = find("размер")
    c_qty = find("количество", "кол-во", "крой", "раскро")
    if c_name is None or c_qty is None:
        log.warning("'Готовый крой': could not locate name/qty columns in %s", header)
        return {}

    out: Dict[str, float] = {}
    for row in raw_rows[1:]:
        if c_name >= len(row):
            continue
        name = str(row[c_name]).strip()
        if not name or name.lower() == "итого":
            continue
        if c_char is not None and c_char < len(row):
            colour, size = matcher.split_characteristic(row[c_char])
        else:
            colour = str(row[c_colour]).strip() if c_colour is not None and c_colour < len(row) else ""
            size = str(row[c_size]).strip() if c_size is not None and c_size < len(row) else None
        qty = _to_float(row[c_qty], default=0.0) if c_qty < len(row) else 0.0
        if not qty or qty <= 0:
            continue
        m = matcher.match_structured(name, colour, size)
        out[m.sku] = out.get(m.sku, 0.0) + qty
    log.info("Ready-cut: parsed %d variant(s) from 'Готовый крой'.", len(out))
    return out


def _readable(sku: str) -> str:
    """'корсет лотус черный|S' -> 'корсет лотус черный, S' — never show a raw key."""
    model, _, size = sku.rpartition("|")
    if not model:
        return sku
    return model if size in ("", "-") else f"{model}, {size}"


# --------------------------------------------------------------------------- #
# Assemble analyzer inputs + blind spots
# --------------------------------------------------------------------------- #
def assemble(models: Dict[str, _Model], variants: List[_Variant],
             ready_cut: Dict[str, float], cfg: Settings, stats: LoadStats,
             sold: Optional[Dict[str, "_Sale"]] = None
             ) -> Tuple[AnalysisInputs, List[DistributionItem]]:
    inputs = AnalysisInputs()
    sold = sold or {}
    inputs.sold = {sku: s.qty for sku, s in sold.items()}

    ranked = sorted(models.items(), key=lambda kv: kv[1].revenue, reverse=True)
    rank_of = {k: i + 1 for i, (k, _) in enumerate(ranked)}

    models_with_stock = set()
    matched = 0
    for v in variants:
        inputs.stock[v.sku] = dict(v.store_qty)
        inputs.names[v.sku] = v.display
        inputs.parts[v.sku] = (v.base, v.characteristic)
        if v.model_key in models:
            info = models[v.model_key]
            inputs.ratings[v.sku] = RatingInfo(v.display, info.revenue, rank_of[v.model_key])
            models_with_stock.add(v.model_key)
            matched += 1
        else:
            stats.unmatched_variants += 1
            if len(stats.unmatched_examples) < 8:
                stats.unmatched_examples.append(v.display)

    for sku, qty in ready_cut.items():
        inputs.ready_cut[sku] = inputs.ready_cut.get(sku, 0.0) + qty

    # A variant can sell out completely and vanish from the stock export; it
    # still needs replacing, so give it a rating entry and a readable name.
    # This runs BEFORE the ready-cut fallback below, otherwise a cut-but-unstocked
    # item would be captioned with its internal key instead of its name.
    for sku, sale in sold.items():
        inputs.names.setdefault(sku, sale.display)
        inputs.parts.setdefault(sku, (sale.base, sale.characteristic))
        if sku not in inputs.ratings:
            model_key = sku.rsplit("|", 1)[0]
            info = models.get(model_key)
            if info is not None:
                inputs.ratings[sku] = RatingInfo(
                    inputs.names[sku], info.revenue, rank_of[model_key])

    # Last resort for rows that exist only in 'Готовый крой': turn the internal
    # key back into something a cutter can read.
    for sku in inputs.ready_cut:
        inputs.names.setdefault(sku, _readable(sku))

    # Blind spots: high-priority models (top-N by revenue) with NO stock at all.
    blind: List[DistributionItem] = []
    for mk, info in ranked:
        if rank_of[mk] <= cfg.high_priority_top_n and mk not in models_with_stock:
            blind.append(DistributionItem(model=info.display, revenue=info.revenue,
                                          rank=rank_of[mk]))
    blind.sort(key=lambda d: d.revenue, reverse=True)

    stats.rating_models = len(models)
    stats.matched_variants = matched
    stats.models_with_stock = len(models_with_stock)
    stats.blind_models = len(blind)
    stats.ready_cut_rows = len(ready_cut)
    log.info("Assembled: %d matched variant(s), %d model(s) with stock, "
             "%d blind best-seller(s).", matched, len(models_with_stock), len(blind))
    return inputs, blind


def load_all(cfg: Settings, rating_path: Path, rating_sheet: str,
             stock_path: Path, stock_sheet: str, ready_cut_raw: List[list],
             matcher: ProductMatcher
             ) -> Tuple[AnalysisInputs, List[DistributionItem], LoadStats, List[_Variant]]:
    models, sold, r_issues = load_rating_models(rating_path, rating_sheet, matcher)
    variants, stats = load_stock_variants(stock_path, stock_sheet, matcher, cfg)
    # fold rating issues / nonpositive count into stats
    for x in r_issues:
        if x.startswith("__nonpos__="):
            stats.rating_skipped_nonpositive = int(x.split("=", 1)[1])
        else:
            stats.issues.append(x)
    ready_cut = parse_ready_cut_1c(ready_cut_raw, matcher)
    inputs, blind = assemble(models, variants, ready_cut, cfg, stats, sold)
    return inputs, blind, stats, variants
