# Production Planner (План кроя / План пошива)

Replaces the manual production-planning routine: it reads **sales ratings** and
**warehouse stock** exported from 1C (Excel/CSV), reconciles messy product
names, applies deficit logic, and writes **Cutting** (План кроя), **Sewing**
(План пошива) and **Transfer** (Перемещения) plans straight into Google Sheets.

Runs locally from a one-line CLI. No data leaves the machine except the final
plans pushed to the configured spreadsheet.

---

## 1. Architecture / data flow

```
            ┌──────────────────────────────────────────────────────────┐
            │                  1C EXPORTS  (local files)                │
            │     rating.xlsx/csv            stock.xlsx/csv             │
            └───────────┬───────────────────────────┬──────────────────┘
                        │                            │
                        ▼                            ▼
            ┌──────────────────────────────────────────────────────────┐
            │  data_loader.py   —  read + Pydantic validate             │
            │  · column-alias resolution   · NaN / blank-row handling   │
            │  · FAIL-FAST on missing required columns (no half-runs)   │
            └───────────────────────────┬──────────────────────────────┘
                                        ▼
            ┌──────────────────────────────────────────────────────────┐
            │  normalizer.py   —  raw name  ->  canonical SKU           │
            │  · strict mappings.json (authoritative)                   │
            │  · algorithmic fallback (ё→е, drop quotes, split size,    │
            │    expand colour, sort tokens)                            │
            │  · unmatched names reported, never silently dropped       │
            └───────────────────────────┬──────────────────────────────┘
                                        ▼
   read "Готовый крой"  ┌───────────────────────────────┐
   ─────────────────────►  analyzer.py  (PURE, no I/O)   │
   Google Sheets        │  deficit engine, cascade:      │
                        │   ГП transfer ► sew cut ► cut  │
                        └───────────────┬───────────────┘
                                        ▼
            ┌──────────────────────────────────────────────────────────┐
            │  google_sheets.py  —  write (or --dry-run prints)         │
            │  · exponential backoff + jitter (429/5xx/network)         │
            │  · idempotent full-replace  ·  single update() per sheet  │
            └───────────────────────────┬──────────────────────────────┘
                                        ▼
                   План кроя   ·   План пошива   ·   Перемещения
                                        │
                                        ▼
                       app.log  (rotating, INFO/WARNING/ERROR)

            main.py orchestrates every stage and owns the exit codes.
```

## 2. Project layout

```
production-planner/
├─ main.py            # CLI orchestrator + exit codes
├─ config.py          # pydantic-settings: .env -> typed, validated Settings
├─ logger.py          # console + rotating app.log
├─ models.py          # pydantic input records + output task dataclasses
├─ normalizer.py      # name -> canonical SKU (mappings + algorithm)
├─ data_loader.py     # Excel/CSV readers, validation, alias resolution
├─ analyzer.py        # the deficit engine (pure, unit-tested)
├─ google_sheets.py   # gspread client: retry/backoff + atomic writes
├─ requirements.txt
├─ .env.example       # copy -> .env
├─ mappings.example.json  # copy -> mappings.json
├─ .gitignore         # ignores .env, credentials.json, *.xlsx, app.log …
├─ samples/           # runnable demo exports (rating.csv, stock.csv)
└─ tests/             # pytest: deficit engine + matcher, no network needed
```

## 3. Setup

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt

copy .env.example .env                 # then fill SPREADSHEET_ID
copy mappings.example.json mappings.json
```

**Google credentials (service account):**
1. Google Cloud Console → enable *Google Sheets API*.
2. Create a *Service Account*, add a JSON key, save it as `credentials.json`
   in the project folder.
3. Open the target spreadsheet → **Share** it with the service account's
   `client_email` (Editor).
4. Put the spreadsheet id (the `…/d/<ID>/edit` part) into `.env`.

## 4. Run

```bash
# Safe: compute + print the plans, write nothing
python main.py --rating samples/rating.csv --stock samples/stock.csv --dry-run

# Real run: also writes to Google Sheets
python main.py --rating rating.xlsx --stock stock.xlsx

python main.py ... --verbose          # DEBUG logging
python -m pytest                      # run the test suite
```

Exit codes: `0` ok · `1` unexpected · `2` config · `3` data · `4` Sheets.

## 5. The deficit logic

### Warehouse roles

Three distinct roles, all set in `.env` — the split matters, because only the
first one may pull goods into production:

| role | env var | meaning |
|---|---|---|
| retail points | `TARGET_STORES_CSV` | replenished; a shortfall here creates demand |
| service warehouses | `STOCK_ONLY_STORES_CSV` | примерка / химчистка / ремонт — stock counts as on-hand (the garments come back) but is **never** replenished |
| finished goods | `FINISHED_GOODS_WAREHOUSE` | shared ГП pool, shipped to stores instead of being produced |

`Склад 1Брака` is deliberately in none of them — a write-off is not stock.

### Planning models (`PLANNING_MODE`, or `--mode`)

**`stores`** — keep `TARGET_STORE_STOCK` units of every high-priority SKU at
every retail point. Demand per store is `TARGET_STORE_STOCK − on_hand` whenever
`on_hand < MIN_STORE_STOCK`. Sizes the shelves, ignores how fast anything sells.

**`sales`** — replace what was sold. Demand per variant is the units sold in the
rating period (the `Количество` column) minus everything already on hand:
retail points, service warehouses, and ГП. Volumes track actual turnover.

Both then satisfy the residual through the same **cascade**:

1. **Transfer (Перемещения)** — cover demand from `Е Склад ГП` finished goods
   first (a shipment, *not* production). ГП is a shared pool, allocated to the
   highest-revenue items first.
2. **Sew (План пошива)** — for whatever ГП couldn't cover, sew pieces that are
   already cut (`Готовый крой`).
3. **Cut (План кроя)** — only the residual that is neither in ГП nor already
   cut goes to the cutting plan.

This guarantees an item is never both transferred *and* re-produced, and never
both cut *and* sewn for the same units.

## 6. Fault tolerance

| Risk | Handling |
|------|----------|
| API quota / 429 / 5xx / network drop | exponential backoff **+ jitter**, capped, configurable retries (`google_sheets.py::_retry`) |
| Missing column in a 1C export | `DataValidationError`, abort **before** any write (exit 3) |
| NaN / blank / garbage cells | coerced or row-skipped with a WARNING; totals logged |
| Name mismatch 1C ↔ Sheets | mappings.json + normaliser; unmatched names reported |
| Crash mid-write → corrupt/duplicate sheet | each plan sheet is **fully replaced in one `update()` call**; re-runs are idempotent, a crash leaves a sheet fully-old or fully-new |
| Bad config / missing creds | validated at start-up (`require_runtime_values`), exit 2 |
| Anything unforeseen | top-level guard in `main()` logs the traceback to `app.log`, exit 1 |

## 7. Tunable assumptions

These are deliberate defaults — change in `.env`, no code edits:

- A store is "missing" an item when `on_hand < MIN_STORE_STOCK` (default 1).
- Replenish target is `TARGET_STORE_STOCK` per store (default 3), `stores` mode only.
- Sewing is scoped to high-priority deficit items (matches "high-deficit").
- Sizes are folded onto the latin grid, so 'с'/'хс' typed in "Готовый крой"
  match 'S'/'XS' coming out of 1C.
- Quantities are treated as integers (garments); revenue stays float.
- Plan sheets are **owned** by the tool — it overwrites them every run. Keep
  manual notes in separate tabs.
```
