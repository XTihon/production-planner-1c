"""
google_sheets.py
================
All Google Sheets I/O, isolated behind one class so the rest of the app never
imports gspread directly.

Fault tolerance:
  * **Exponential backoff with jitter** on quota (429) and transient server
    errors (500/502/503/504) and network drops — see ``_retry``.
  * **Idempotent writes.** Each plan sheet is *fully owned* by this script.
    ``replace_sheet`` overwrites the whole used range in a single
    ``values_update`` call and blanks any trailing stale rows, so re-running the
    planner can never duplicate rows, and a crash mid-run leaves a sheet that is
    either fully old or fully new — never a half-merged hybrid.
  * Every network call is wrapped; failures raise :class:`SheetsError` with
    context instead of leaking a raw stack trace.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Sequence, TypeVar

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, WorksheetNotFound

from config import Settings
from logger import get_logger

log = get_logger("sheets")

T = TypeVar("T")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class SheetsError(Exception):
    """Wraps any unrecoverable Google Sheets failure."""


@dataclass
class SheetWrite:
    """One plan to (re)write: a tab name, its header row, and data rows."""

    name: str
    header: Sequence[str]
    rows: Sequence[Sequence]


class GoogleSheetsClient:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self._gc: gspread.Client | None = None
        self._sh: gspread.Spreadsheet | None = None

    # ------------------------------------------------------------- connect
    def connect(self) -> None:
        """Authenticate and open the target spreadsheet (with retry)."""
        creds_path: Path = self.cfg.google_credentials_file
        if not creds_path.exists():
            raise SheetsError(f"Service-account file not found: {creds_path}")
        try:
            creds = Credentials.from_service_account_file(str(creds_path), scopes=_SCOPES)
            self._gc = gspread.authorize(creds)
        except Exception as exc:  # malformed key, etc.
            raise SheetsError(f"Failed to load credentials: {exc}") from exc

        self._sh = self._retry(
            lambda: self._gc.open_by_key(self.cfg.spreadsheet_id),
            what="open spreadsheet",
        )
        log.info("Connected to spreadsheet '%s'.", self._safe_title())

    def _safe_title(self) -> str:
        try:
            return self._sh.title if self._sh else "?"
        except Exception:  # noqa: BLE001
            return "?"

    # ------------------------------------------------------------- read
    def read_records(self, sheet_name: str) -> List[dict]:
        """Return a worksheet as a list of dicts (header row = keys)."""
        ws = self._open_ws(sheet_name, create_if_missing=False)
        if ws is None:
            log.warning("Worksheet '%s' not found — treating as empty.", sheet_name)
            return []
        return self._retry(ws.get_all_records, what=f"read '{sheet_name}'")

    def read_values(self, sheet_name: str) -> List[list]:
        """Return a worksheet as raw rows (list of lists). [] if missing.

        Used for irregular layouts (multi-row headers) where header-keyed
        records don't apply.
        """
        ws = self._open_ws(sheet_name, create_if_missing=False)
        if ws is None:
            log.warning("Worksheet '%s' not found — treating as empty.", sheet_name)
            return []
        return self._retry(ws.get_all_values, what=f"read values '{sheet_name}'")

    def tab_exists(self, sheet_name: str) -> bool:
        return self._open_ws(sheet_name, create_if_missing=False) is not None

    def ensure_input_tab(self, sheet_name: str, header: Sequence[str]) -> bool:
        """Create a header-only INPUT tab if it is missing. Never overwrites.

        Returns True if it was created. Used to bootstrap 'Готовый крой' so the
        manager has somewhere to record already-cut items.
        """
        if self._open_ws(sheet_name, create_if_missing=False) is not None:
            return False
        ws = self._retry(
            lambda: self._sh.add_worksheet(title=sheet_name, rows=100,
                                           cols=max(5, len(header))),
            what=f"create input tab '{sheet_name}'",
        )
        self._retry(
            lambda: ws.update(range_name="A1", values=[list(header)],
                              value_input_option="USER_ENTERED"),
            what=f"write header '{sheet_name}'",
        )
        log.info("Created empty input tab '%s'.", sheet_name)
        return True

    # ------------------------------------------------------------- write
    def replace_plans_atomic(self, writes: Sequence[SheetWrite]) -> None:
        """Replace several plan sheets in a SINGLE ``values.batchUpdate`` call.

        Why one call: the whole batch is one HTTP request, so it either fully
        succeeds or fully fails. A crash can therefore never leave one plan
        updated and another stale — the core atomicity requirement. Each grid is
        padded to the sheet's previous extent so no orphaned rows survive, and
        because we always overwrite from A1, re-running can never duplicate rows.
        """
        if self._sh is None:
            raise SheetsError("Not connected — call connect() first.")

        data = []
        for w in writes:
            ws = self._open_ws(w.name, create_if_missing=True)
            grid = self._build_padded_grid(ws, w.header, w.rows)
            self._ensure_capacity(ws, rows=len(grid), cols=len(w.header))
            escaped = w.name.replace("'", "''")  # sheet names with apostrophes
            data.append({"range": f"'{escaped}'!A1", "values": grid})

        body = {"valueInputOption": "USER_ENTERED", "data": data}
        self._retry(
            lambda: self._sh.values_batch_update(body),
            what="batch-write plans",
        )
        log.info("Atomically wrote %d plan sheet(s) in one batchUpdate: %s",
                 len(writes), ", ".join(f"{w.name}={len(w.rows)}" for w in writes))

    def _build_padded_grid(
        self, ws, header: Sequence[str], rows: Sequence[Sequence]
    ) -> List[List]:
        """[header, *rows] padded to the sheet's previous extent (erases stale)."""
        grid: List[List] = [list(header)] + [list(r) for r in rows]
        prev_used = self._retry(
            lambda: len(ws.get_all_values()), what=f"measure '{ws.title}'"
        )
        width = len(header)
        while len(grid) < prev_used:
            grid.append([""] * width)
        return [row + [""] * (width - len(row)) for row in grid]  # rectangular

    # ------------------------------------------------------------- helpers
    def _open_ws(self, sheet_name: str, *, create_if_missing: bool):
        if self._sh is None:
            raise SheetsError("Not connected — call connect() first.")
        try:
            return self._retry(
                lambda: self._sh.worksheet(sheet_name),
                what=f"open '{sheet_name}'",
                _no_retry=(WorksheetNotFound,),
            )
        except WorksheetNotFound:
            if not create_if_missing:
                return None
            log.info("Creating missing worksheet '%s'.", sheet_name)
            return self._retry(
                lambda: self._sh.add_worksheet(title=sheet_name, rows=100, cols=20),
                what=f"create '{sheet_name}'",
            )

    def _ensure_capacity(self, ws, *, rows: int, cols: int) -> None:
        try:
            if ws.row_count < rows or ws.col_count < cols:
                self._retry(
                    lambda: ws.resize(rows=max(rows, ws.row_count),
                                      cols=max(cols, ws.col_count)),
                    what=f"resize '{ws.title}'",
                )
        except APIError as exc:
            # Non-fatal: update() will still try; surface as a warning.
            log.warning("Could not resize '%s': %s", ws.title, exc)

    # ------------------------------------------------------------- retry core
    def _retry(self, func: Callable[[], T], *, what: str,
               _no_retry: tuple = ()) -> T:
        """Call ``func`` with exponential backoff + jitter on transient errors."""
        cfg = self.cfg
        delay = cfg.backoff_initial
        last_exc: Exception | None = None

        for attempt in range(1, cfg.max_retries + 2):  # initial try + retries
            try:
                return func()
            except _no_retry:
                raise  # caller wants to handle this (e.g. WorksheetNotFound)
            except APIError as exc:
                status = self._status_of(exc)
                last_exc = exc
                if status not in _RETRYABLE_STATUS or attempt > cfg.max_retries:
                    raise SheetsError(
                        f"{what}: Google API error {status} — giving up after "
                        f"{attempt} attempt(s): {exc}"
                    ) from exc
                log.warning("%s: API %s (attempt %d/%d) — retrying in %.1fs.",
                            what, status, attempt, cfg.max_retries, delay)
            except (ConnectionError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt > cfg.max_retries:
                    raise SheetsError(
                        f"{what}: network error — giving up after {attempt} "
                        f"attempt(s): {exc}"
                    ) from exc
                log.warning("%s: network error (attempt %d/%d) — retrying in %.1fs.",
                            what, attempt, cfg.max_retries, delay)

            # Sleep with jitter, then escalate the delay (capped).
            time.sleep(delay + random.uniform(0, delay * 0.25))
            delay = min(delay * cfg.backoff_base, cfg.backoff_max)

        # Should be unreachable, but be explicit.
        raise SheetsError(f"{what}: exhausted retries: {last_exc}")

    @staticmethod
    def _status_of(exc: APIError) -> int:
        try:
            return int(exc.response.status_code)
        except Exception:  # noqa: BLE001
            return -1
