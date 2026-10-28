"""
main.py
=======
CLI entry point / orchestrator.

Generic (simple single-header exports):
    python main.py --rating rating.xlsx --stock stock.xlsx [--dry-run]

1C (real cross-tab exports — multi-row headers, цвет+размер field):
    python main.py --source 1c \
        --rating "рейтинг_остатки.xlsx" --rating-sheet "рейтинг май 2026" \
        --stock  "остатки изделий.xls"  --stock-sheet  "TDSheet" [--dry-run]

Flow: logging -> config -> load+validate exports -> connect Sheets ->
read 'Готовый крой' -> match/assemble -> deficit engine -> write plans
(План кроя / План пошива / Перемещения [+ Бестселлеры к распределению]) or print.

Exit codes:  0 ok · 1 unexpected · 2 config · 3 data · 4 Sheets
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from analyzer import AnalysisResult, prepare_inputs, run_analysis
from config import get_settings
from data_loader import DataValidationError, load_ratings, load_stock, parse_ready_cut
from google_sheets import GoogleSheetsClient, SheetsError, SheetWrite
from logger import get_logger, setup_logging
from models import DistributionItem
from normalizer import ProductMatcher
from pipeline_1c import load_all as load_all_1c

EXIT_OK, EXIT_ERR, EXIT_CONFIG, EXIT_DATA, EXIT_SHEETS = 0, 1, 2, 3, 4


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="production-planner",
        description="Generate Cutting/Sewing plans in Google Sheets from 1C exports.",
    )
    p.add_argument("--rating", required=True, type=Path,
                   help="Path to the sales-rating export.")
    p.add_argument("--stock", required=True, type=Path,
                   help="Path to the warehouse-stock export.")
    p.add_argument("--source", choices=["generic", "1c"], default="generic",
                   help="'1c' for the real cross-tab exports; 'generic' (default) "
                        "for simple single-header files.")
    p.add_argument("--rating-sheet", default=None,
                   help="[1c] worksheet name inside the rating workbook.")
    p.add_argument("--stock-sheet", default=None,
                   help="[1c] worksheet name inside the stock workbook.")
    p.add_argument("--mode", choices=["stores", "sales"], default=None,
                   help="Planning model, overrides PLANNING_MODE in .env. "
                        "'stores' keeps N units per point; 'sales' replaces "
                        "what was sold.")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute and print the plans but DO NOT write to Sheets.")
    p.add_argument("--verbose", action="store_true", help="DEBUG logging.")
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# Output helpers (shared by both sources)
# --------------------------------------------------------------------------- #
def _plan_writes(cfg, result: AnalysisResult,
                 blind: Optional[List[DistributionItem]]) -> List[SheetWrite]:
    # Наименование and характеристика are kept in separate columns so the sheet
    # can be sorted/filtered by model; продано/в наличии make the arithmetic
    # behind "К раскрою" visible instead of something to be queried by phone.
    writes = [
        SheetWrite(
            cfg.sheet_cutting_plan,
            ["Номенклатура", "Характеристика", "К раскрою",
             "Продано", "Есть в наличии", "Выручка", "Рейтинг"],
            [[t.name, t.characteristic, t.qty, t.sold, t.on_hand, t.revenue, t.rank]
             for t in result.cut_tasks],
        ),
        SheetWrite(
            cfg.sheet_sewing_plan,
            ["Номенклатура", "Характеристика", "К пошиву",
             "Продано", "Есть в наличии", "Выручка"],
            [[t.name, t.characteristic, t.qty, t.sold, t.on_hand, t.revenue]
             for t in result.sew_tasks],
        ),
        SheetWrite(
            cfg.sheet_transfer_alerts,
            ["Номенклатура", "Характеристика", "В магазин", "Кол-во", "Со склада"],
            [[t.name, t.characteristic, t.store, t.qty, t.source_warehouse]
             for t in result.transfers],
        ),
    ]
    if blind is not None:
        writes.append(SheetWrite(
            cfg.sheet_distribution,
            ["Модель (наимен.+цвет)", "Выручка", "Рейтинг",
             "XS", "S", "M", "L", "XL", "Комментарий"],
            [[d.model, d.revenue, d.rank, "", "", "", "", "", ""] for d in blind],
        ))
    return writes


def _stock_raw_write(cfg, variants) -> SheetWrite:
    """Raw per-warehouse stock, unfiltered — every warehouse seen in the 1C
    export, not just the target stores used by the deficit engine."""
    all_wh = sorted({wh for v in variants for wh in v.store_qty_raw})
    header = ["Номенклатура", "Характеристика"] + all_wh
    rows = [[v.base, v.characteristic]
            + [v.store_qty_raw.get(wh, "") for wh in all_wh]
            for v in variants]
    return SheetWrite(cfg.sheet_stock_raw, header, rows)


def _print_dry_run(result: AnalysisResult,
                   blind: Optional[List[DistributionItem]]) -> None:
    def block(title, rows):
        print(f"\n=== {title} ({len(rows)}) ===")
        for r in rows[:50]:
            print("   ", r)
        if len(rows) > 50:
            print(f"    ... +{len(rows) - 50} more")

    block("ПЛАН КРОЯ", [(t.name, t.qty) for t in result.cut_tasks])
    block("ПЛАН ПОШИВА", [(t.name, t.qty) for t in result.sew_tasks])
    block("ПЕРЕМЕЩЕНИЯ", [(t.name, t.store, t.qty) for t in result.transfers])
    if blind is not None:
        block("БЕСТСЕЛЛЕРЫ К РАСПРЕДЕЛЕНИЮ",
              [(d.model, f"#{d.rank}", d.revenue) for d in blind])


# --------------------------------------------------------------------------- #
# 1C pipeline
# --------------------------------------------------------------------------- #
def _run_1c(args, cfg, matcher, log) -> int:
    client = GoogleSheetsClient(cfg)
    ready_raw: List[list] = []
    connected = False
    if cfg.spreadsheet_id:
        try:
            client.connect()
            connected = True
            if client.tab_exists(cfg.sheet_ready_cut):
                ready_raw = client.read_values(cfg.sheet_ready_cut)
            else:
                log.warning("Tab '%s' is absent — План пошива will be empty and "
                            "План кроя is an UPPER BOUND.", cfg.sheet_ready_cut)
                if not args.dry_run:
                    client.ensure_input_tab(
                        cfg.sheet_ready_cut,
                        ["Номенклатура", "Характеристика (цвет, размер)", "Количество"])
        except SheetsError as exc:
            log.error("Google Sheets error while reading: %s", exc)
            return EXIT_SHEETS
    else:
        log.warning("No SPREADSHEET_ID — running fully offline; sewing empty.")

    rating_sheet = args.rating_sheet or "рейтинг май 2026"
    stock_sheet = args.stock_sheet or "TDSheet"
    try:
        inputs, blind, stats, variants = load_all_1c(
            cfg, args.rating, rating_sheet, args.stock, stock_sheet, ready_raw, matcher)
    except FileNotFoundError as exc:
        log.error("Input file not found: %s", exc)
        return EXIT_DATA
    except Exception as exc:  # noqa: BLE001 — surface parse failures cleanly
        log.exception("1C load/parse failed: %s", exc)
        return EXIT_DATA

    result = run_analysis(inputs, cfg)

    log.info("MATCH: %d/%d variants matched to a rated model; "
             "%d models with stock; %d blind best-seller(s); ready_cut rows=%d.",
             stats.matched_variants,
             stats.matched_variants + stats.unmatched_variants,
             stats.models_with_stock, stats.blind_models, stats.ready_cut_rows)
    for issue in stats.issues:
        log.warning("data issue: %s", issue)

    if args.dry_run:
        log.info("DRY RUN — no sheets will be modified.")
        _print_dry_run(result, blind)
        print(f"\nCOUNTS: План кроя={len(result.cut_tasks)}, "
              f"План пошива={len(result.sew_tasks)}, "
              f"Перемещения={len(result.transfers)}, "
              f"Бестселлеры к распределению={len(blind)}")
        log.info("Done (dry-run). %s; blind=%d", result.summary(), len(blind))
        return EXIT_OK

    if not connected:
        log.error("Cannot write: not connected to Google Sheets.")
        return EXIT_SHEETS
    try:
        client.replace_plans_atomic(
            _plan_writes(cfg, result, blind) + [_stock_raw_write(cfg, variants)])
    except SheetsError as exc:
        log.error("Google Sheets error while writing: %s", exc)
        return EXIT_SHEETS
    log.info("Done (LIVE). %s; Бестселлеры=%d", result.summary(), len(blind))
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Generic pipeline (simple single-header files)
# --------------------------------------------------------------------------- #
def _run_generic(args, cfg, matcher, log) -> int:
    try:
        log.info("Loading exports (generic) …")
        ratings = load_ratings(args.rating)
        stock = load_stock(args.stock, cfg.target_stores, cfg.finished_goods_warehouse)
    except DataValidationError as exc:
        log.error("Input data invalid: %s", exc)
        return EXIT_DATA

    client = GoogleSheetsClient(cfg)
    ready_cut = []
    if args.dry_run and not cfg.spreadsheet_id:
        log.warning("Dry-run without SPREADSHEET_ID — 'Готовый крой' skipped.")
    else:
        try:
            client.connect()
            ready_cut = parse_ready_cut(client.read_records(cfg.sheet_ready_cut))
        except SheetsError as exc:
            log.error("Google Sheets error while reading: %s", exc)
            return EXIT_SHEETS
        except DataValidationError as exc:
            log.error("'Готовый крой' invalid: %s", exc)
            return EXIT_DATA

    inputs = prepare_inputs(ratings, stock, ready_cut, matcher)
    result = run_analysis(inputs, cfg)

    if args.dry_run:
        log.info("DRY RUN — no sheets will be modified.")
        _print_dry_run(result, None)
        log.info("Done (dry-run). %s", result.summary())
        return EXIT_OK
    try:
        client.replace_plans_atomic(_plan_writes(cfg, result, None))
    except SheetsError as exc:
        log.error("Google Sheets error while writing: %s", exc)
        return EXIT_SHEETS
    log.info("Done. %s", result.summary())
    return EXIT_OK


def run(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(verbose=args.verbose)
    log = get_logger("main")

    try:
        cfg = get_settings()
        if args.mode:
            cfg.planning_mode = args.mode
        if not args.dry_run:
            cfg.require_runtime_values()
        log.info("Planning mode: %s | replenished: %s | counted-only: %s",
                 cfg.planning_mode, ", ".join(cfg.target_stores),
                 ", ".join(cfg.stock_only_stores) or "—")
    except Exception as exc:  # pydantic ValidationError or our ValueError
        log.error("Configuration error: %s", exc)
        return EXIT_CONFIG

    matcher = ProductMatcher.from_file(cfg.mappings_file)
    if args.source == "1c":
        return _run_1c(args, cfg, matcher, log)
    return _run_generic(args, cfg, matcher, log)


def main() -> None:
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        get_logger("main").error("Interrupted by user.")
        sys.exit(EXIT_ERR)
    except Exception as exc:  # last-resort guard: nothing escapes unlogged
        get_logger("main").exception("Unexpected fatal error: %s", exc)
        sys.exit(EXIT_ERR)


if __name__ == "__main__":
    main()
