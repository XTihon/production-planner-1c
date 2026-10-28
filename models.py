"""
models.py
=========
Typed data contracts shared across the pipeline.

* ``*Record`` classes are **inputs** — validated with pydantic as data is read
  from 1C exports / Google Sheets. Validation here is the firewall that stops
  corrupted source data from ever reaching the write stage.

* ``*Task`` / ``TransferAlert`` classes are **outputs** — the rows the analyzer
  produces, later serialised to the plan worksheets.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, field_validator


# --------------------------------------------------------------------------- #
# Inputs (validated)
# --------------------------------------------------------------------------- #
class RatingRecord(BaseModel):
    """One line of the sales-rating export."""

    raw_name: str
    revenue: float
    rank: Optional[int] = None

    @field_validator("raw_name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("empty product name in rating export")
        return v

    @field_validator("revenue")
    @classmethod
    def _revenue_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"negative revenue: {v}")
        return v


class StockRecord(BaseModel):
    """One product/location/quantity triple from the stock export."""

    raw_name: str
    store: str
    qty: float

    @field_validator("raw_name", "store")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("empty name/store in stock export")
        return v

    @field_validator("qty")
    @classmethod
    def _qty_non_negative(cls, v: float) -> float:
        # Stock can legitimately be 0; negative means a broken export.
        if v < 0:
            raise ValueError(f"negative stock quantity: {v}")
        return v


class ReadyCutRecord(BaseModel):
    """One row of the 'Готовый крой' sheet (already-cut, awaiting sewing)."""

    raw_name: str
    qty: float

    @field_validator("raw_name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("empty name in 'Готовый крой'")
        return v

    @field_validator("qty")
    @classmethod
    def _qty_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"negative ready-cut quantity: {v}")
        return v


# --------------------------------------------------------------------------- #
# Outputs (plain dataclasses — no validation needed, we built them ourselves)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class TransferAlert:
    """Move finished goods from the ГП warehouse to a retail store."""

    sku: str
    name: str
    store: str
    qty: int
    source_warehouse: str
    characteristic: str = ""


@dataclass(slots=True)
class CutTask:
    """Add an item to the cutting plan (План кроя).

    ``sold``/``on_hand`` are carried purely so the exported sheet can show the
    arithmetic — "why only 1?" is the question every plan gets asked.
    """

    sku: str
    name: str
    qty: int
    revenue: float
    rank: Optional[int]
    characteristic: str = ""
    sold: int = 0
    on_hand: int = 0


@dataclass(slots=True)
class SewTask:
    """Add an item to the sewing plan (План пошива) — it is already cut."""

    sku: str
    name: str
    qty: int
    revenue: float
    characteristic: str = ""
    sold: int = 0
    on_hand: int = 0


@dataclass(slots=True)
class DistributionItem:
    """A best-selling model with ZERO stock anywhere — absent from the stock
    export, so its sizes can't be inferred. Goes to 'Бестселлеры к
    распределению' for a manager to fill sizes in by hand."""

    model: str
    revenue: float
    rank: int
