"""
analyzer.py
===========
The deficit engine — the brain of the planner. **Pure logic, zero I/O**, so it
can be unit-tested without Google or files.

Pipeline:
  prepare_inputs(...)  -> AnalysisInputs   (raw records -> SKU-keyed structures)
  run_analysis(...)    -> AnalysisResult   (transfers / cut / sew tasks)

Business rules implemented (all thresholds come from Settings):

  For every retail store in TARGET_STORES, for each high-priority SKU:
    on_hand = stock at that store
    if on_hand < MIN_STORE_STOCK                       -> the store is "missing" it
        need = TARGET_STORE_STOCK - on_hand
        # Finished-goods (Е Склад ГП) is a shared pool, allocated by priority:
        take = min(need, gp_available)
        if take > 0   -> TRANSFER alert (ship from ГП, do NOT sew)
        remainder = need - take
        if remainder  -> add remainder to the SKU's cutting deficit

  Cutting plan (План кроя)  = high-priority SKUs whose cutting deficit > 0
                              (missing in stores AND not coverable from ГП).
  Sewing plan  (План пошива)= SKUs already in 'Готовый крой' that intersect the
                              store deficit; sew min(ready_cut_qty, store_deficit).

"Priority" = revenue rank in the top-N  OR  revenue >= MIN_REVENUE_THRESHOLD.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import Settings
from logger import get_logger
from models import (
    CutTask,
    RatingRecord,
    ReadyCutRecord,
    SewTask,
    StockRecord,
    TransferAlert,
)
from normalizer import ProductMatcher

log = get_logger("analyzer")


@dataclass
class RatingInfo:
    name: str
    revenue: float
    rank: Optional[int]


@dataclass
class AnalysisInputs:
    """Everything the engine needs, keyed by canonical SKU."""

    ratings: Dict[str, RatingInfo] = field(default_factory=dict)
    # stock[sku][store] = qty  (store includes the ГП warehouse)
    stock: Dict[str, Dict[str, float]] = field(default_factory=dict)
    ready_cut: Dict[str, float] = field(default_factory=dict)
    names: Dict[str, str] = field(default_factory=dict)  # sku -> display name
    # sku -> (наименование, "цвет, размер") so the sheets can keep them apart.
    parts: Dict[str, tuple] = field(default_factory=dict)
    # Units sold per variant during the rating period — the demand signal the
    # "sales" planning mode replenishes against.
    sold: Dict[str, float] = field(default_factory=dict)

    def split_name(self, sku: str) -> tuple:
        """(наименование, характеристика) — falls back to splitting the caption."""
        if sku in self.parts:
            return self.parts[sku]
        caption = self.names.get(sku, sku)
        head, sep, tail = caption.partition(",")
        return (head.strip(), tail.strip()) if sep else (caption, "")


@dataclass
class AnalysisResult:
    transfers: List[TransferAlert] = field(default_factory=list)
    cut_tasks: List[CutTask] = field(default_factory=list)
    sew_tasks: List[SewTask] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{len(self.transfers)} transfer(s), "
                f"{len(self.cut_tasks)} cut task(s), "
                f"{len(self.sew_tasks)} sew task(s)")


# --------------------------------------------------------------------------- #
# Stage 1: assemble SKU-keyed inputs from validated records
# --------------------------------------------------------------------------- #
def prepare_inputs(
    ratings: List[RatingRecord],
    stock: List[StockRecord],
    ready_cut: List[ReadyCutRecord],
    matcher: ProductMatcher,
) -> AnalysisInputs:
    inputs = AnalysisInputs()

    # Display name = the ORIGINAL human-readable string (factory staff must
    # recognise it). The canonical SKU is used only for matching/aggregation.
    for r in ratings:
        m = matcher.match_or_warn(r.raw_name)
        if not m:
            continue
        display = r.raw_name.strip()
        # Keep the best (highest) revenue if a SKU appears twice.
        existing = inputs.ratings.get(m.sku)
        if existing is None or r.revenue > existing.revenue:
            inputs.ratings[m.sku] = RatingInfo(display, r.revenue, r.rank)
        inputs.names.setdefault(m.sku, display)

    for s in stock:
        m = matcher.match_or_warn(s.raw_name)
        if not m:
            continue
        bucket = inputs.stock.setdefault(m.sku, {})
        bucket[s.store] = bucket.get(s.store, 0.0) + s.qty
        inputs.names.setdefault(m.sku, s.raw_name.strip())

    for c in ready_cut:
        m = matcher.match_or_warn(c.raw_name)
        if not m:
            continue
        inputs.ready_cut[m.sku] = inputs.ready_cut.get(m.sku, 0.0) + c.qty
        inputs.names.setdefault(m.sku, c.raw_name.strip())

    log.info("Prepared inputs: %d rated SKU(s), %d stocked SKU(s), "
             "%d ready-cut SKU(s).",
             len(inputs.ratings), len(inputs.stock), len(inputs.ready_cut))
    return inputs


# --------------------------------------------------------------------------- #
# Stage 2: priority ranking
# --------------------------------------------------------------------------- #
def _high_priority_skus(inputs: AnalysisInputs, cfg: Settings) -> Dict[str, RatingInfo]:
    """Return the subset of rated SKUs considered high priority."""
    # Rank by revenue desc; assign positional rank if the export lacked one.
    ordered = sorted(inputs.ratings.items(),
                     key=lambda kv: kv[1].revenue, reverse=True)
    high: Dict[str, RatingInfo] = {}
    for pos, (sku, info) in enumerate(ordered, start=1):
        rank = info.rank if info.rank is not None else pos
        is_top_n = rank <= cfg.high_priority_top_n
        meets_floor = (cfg.min_revenue_threshold > 0
                       and info.revenue >= cfg.min_revenue_threshold)
        if is_top_n or meets_floor:
            high[sku] = RatingInfo(info.name, info.revenue, rank)
    log.info("High-priority SKUs: %d (top_n=%d, revenue_floor=%s).",
             len(high), cfg.high_priority_top_n, cfg.min_revenue_threshold or "off")
    return high


# --------------------------------------------------------------------------- #
# Stage 3: the deficit engine
# --------------------------------------------------------------------------- #
def run_analysis(inputs: AnalysisInputs, cfg: Settings) -> AnalysisResult:
    """Dispatch to the configured planning model."""
    if cfg.planning_mode == "sales":
        return _run_by_sales(inputs, cfg)
    return _run_by_stores(inputs, cfg)


def _spare_stock(per_sku_stock: Dict[str, float], cfg: Settings) -> int:
    """Units sitting on service warehouses (примерка/химчистка/ремонт).

    They are never replenished, but the garments do come back, so they offset
    demand instead of triggering production.
    """
    return sum(int(per_sku_stock.get(s, 0)) for s in cfg.stock_only_stores)


def _run_by_stores(inputs: AnalysisInputs, cfg: Settings) -> AnalysisResult:
    result = AnalysisResult()
    high = _high_priority_skus(inputs, cfg)
    gp = cfg.finished_goods_warehouse
    target = cfg.target_store_stock
    min_stock = cfg.min_store_stock

    # Process SKUs by descending revenue so the shared ГП pool is allocated to
    # the most valuable items first (greedy, priority-respecting allocation).
    for sku, info in sorted(high.items(), key=lambda kv: kv[1].revenue, reverse=True):
        per_sku_stock = inputs.stock.get(sku, {})
        gp_available = int(per_sku_stock.get(gp, 0))

        # --- Step 1: per-store demand, covered first from ГП (a transfer) -----
        need_total = 0
        for store in cfg.target_stores:
            on_hand = int(per_sku_stock.get(store, 0))
            if on_hand >= min_stock:
                continue  # store adequately stocked
            need = max(0, target - on_hand)
            if need == 0:
                continue
            need_total += need

            take = min(need, gp_available)  # transfer instead of producing
            if take > 0:
                t_base, t_char = inputs.split_name(sku)
                result.transfers.append(TransferAlert(
                    sku=sku, name=t_base, characteristic=t_char, store=store,
                    qty=take, source_warehouse=gp,
                ))
                gp_available -= take

        # Residual demand that ГП could NOT cover -> must be produced, minus
        # whatever is parked on the service warehouses and will come back.
        residual = need_total - (int(per_sku_stock.get(gp, 0)) - gp_available)
        residual -= _spare_stock(per_sku_stock, cfg)
        if residual <= 0:
            continue  # fully satisfied from stock on hand

        # --- Step 2: produce the residual — SEW what is already cut first -----
        base, char = inputs.split_name(sku)
        on_hand = sum(int(per_sku_stock.get(s, 0))
                      for s in cfg.counted_stores) + int(per_sku_stock.get(gp, 0))
        ready_qty = int(inputs.ready_cut.get(sku, 0))
        sew_qty = min(residual, ready_qty)
        if sew_qty > 0:
            result.sew_tasks.append(SewTask(
                sku=sku, name=base, characteristic=char, qty=sew_qty,
                revenue=info.revenue, sold=int(inputs.sold.get(sku, 0)),
                on_hand=on_hand,
            ))

        # --- Step 3: CUT only what is still missing after sewing -------------
        cut_qty = residual - sew_qty
        if cut_qty > 0:
            result.cut_tasks.append(CutTask(
                sku=sku, name=base, characteristic=char, qty=cut_qty,
                revenue=info.revenue, rank=info.rank,
                sold=int(inputs.sold.get(sku, 0)), on_hand=on_hand,
            ))

    _finalise(result)
    return result


# --------------------------------------------------------------------------- #
# Stage 3b: the "replace what was sold" engine
# --------------------------------------------------------------------------- #
def _run_by_sales(inputs: AnalysisInputs, cfg: Settings) -> AnalysisResult:
    """Produce what was sold, not what would fill every shelf.

    Demand for a variant = units sold during the rating period. Anything already
    on hand — retail points, service warehouses, and the ГП finished-goods pool —
    counts against it, because those garments are available to sell. Only the
    shortfall is produced, sewing what is already cut before cutting anew.

    Transfers are computed separately: they redistribute goods that already
    exist and never add to production.
    """
    result = AnalysisResult()
    gp = cfg.finished_goods_warehouse
    counted = cfg.counted_stores

    ordered = sorted(inputs.sold.items(),
                     key=lambda kv: inputs.ratings.get(kv[0]).revenue
                     if kv[0] in inputs.ratings else 0.0, reverse=True)

    for sku, sold in ordered:
        need = int(sold)
        if need <= 0:
            continue
        per_sku_stock = inputs.stock.get(sku, {})
        on_hand = sum(int(per_sku_stock.get(s, 0)) for s in counted)
        on_hand += int(per_sku_stock.get(gp, 0))
        residual = need - on_hand
        if residual <= 0:
            continue  # stock already covers what sold

        info = inputs.ratings.get(sku)
        base, char = inputs.split_name(sku)
        revenue = info.revenue if info else 0.0
        rank = info.rank if info else None

        ready_qty = int(inputs.ready_cut.get(sku, 0))
        sew_qty = min(residual, ready_qty)
        if sew_qty > 0:
            result.sew_tasks.append(SewTask(
                sku=sku, name=base, characteristic=char, qty=sew_qty,
                revenue=revenue, sold=need, on_hand=on_hand))
        cut_qty = residual - sew_qty
        if cut_qty > 0:
            result.cut_tasks.append(CutTask(
                sku=sku, name=base, characteristic=char, qty=cut_qty,
                revenue=revenue, rank=rank, sold=need, on_hand=on_hand))

    _plan_transfers(inputs, cfg, result)
    log.info("Sales mode: %d variant(s) sold, %d needed production.",
             len(inputs.sold), len(result.cut_tasks) + len(result.sew_tasks))
    _finalise(result)
    return result


def _plan_transfers(inputs: AnalysisInputs, cfg: Settings,
                    result: AnalysisResult) -> None:
    """Ship existing finished goods from ГП to retail points that ran dry.

    Pure logistics — moving stock that already exists — so it is independent of
    the production plan and never inflates it.
    """
    gp = cfg.finished_goods_warehouse
    high = _high_priority_skus(inputs, cfg)
    for sku, info in sorted(high.items(), key=lambda kv: kv[1].revenue, reverse=True):
        per_sku_stock = inputs.stock.get(sku, {})
        gp_available = int(per_sku_stock.get(gp, 0))
        if gp_available <= 0:
            continue
        for store in cfg.target_stores:
            if gp_available <= 0:
                break
            on_hand = int(per_sku_stock.get(store, 0))
            if on_hand >= cfg.min_store_stock:
                continue
            take = min(cfg.target_store_stock - on_hand, gp_available)
            if take > 0:
                base, char = inputs.split_name(sku)
                result.transfers.append(TransferAlert(
                    sku=sku, name=base, characteristic=char, store=store,
                    qty=take, source_warehouse=gp,
                ))
                gp_available -= take


def _finalise(result: AnalysisResult) -> None:
    """Stable, business-friendly ordering: most valuable first."""
    result.cut_tasks.sort(key=lambda t: t.revenue, reverse=True)
    result.sew_tasks.sort(key=lambda t: t.revenue, reverse=True)
    result.transfers.sort(key=lambda t: t.name)
    log.info("Analysis complete: %s", result.summary())
