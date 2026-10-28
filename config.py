"""
config.py
=========
Centralised, validated configuration.

Every tunable lives here. Secrets (the path to the service-account key and the
spreadsheet id) are read from a local ``.env`` file or the real environment and
are NEVER hard-coded. Business rules (target stores, thresholds, sheet names)
are overridable from ``.env`` too, so the planner can be re-tuned without
touching code.

The settings object is validated by pydantic at start-up: if a required value is
missing or malformed the process fails *before* it ever touches Google Sheets.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
# When bundled by PyInstaller (``sys.frozen``) the code lives in a temp _MEIPASS
# dir, but .env / credentials.json must be read from NEXT TO the .exe. Otherwise
# (normal run) the project directory is the base.
if getattr(sys, "frozen", False):  # running inside a PyInstaller bundle
    _exe_dir = Path(sys.executable).resolve().parent
    # macOS wraps the binary in <App>.app/Contents/MacOS/, so .env and
    # credentials.json must be read from NEXT TO the .app, not inside it.
    if (sys.platform == "darwin" and _exe_dir.name == "MacOS"
            and _exe_dir.parent.name == "Contents"):
        BASE_DIR = _exe_dir.parent.parent.parent
    else:
        BASE_DIR = _exe_dir            # Windows .exe: next to the executable
else:
    BASE_DIR = Path(__file__).resolve().parent

LOG_FILE = BASE_DIR / "app.log"
DEFAULT_MAPPINGS_FILE = BASE_DIR / "mappings.json"
DEFAULT_CREDENTIALS_FILE = BASE_DIR / "credentials.json"


class Settings(BaseSettings):
    """Strongly-typed application settings, sourced from .env / environment."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Google credentials & target document -------------------------------
    google_credentials_file: Path = Field(default=DEFAULT_CREDENTIALS_FILE)
    spreadsheet_id: str = Field(default="")

    # --- Worksheet (tab) names ---------------------------------------------
    # These must match the tab captions in the Google Sheet exactly.
    sheet_ready_cut: str = Field(default="Готовый крой")
    sheet_cutting_plan: str = Field(default="План кроя")
    sheet_sewing_plan: str = Field(default="План пошива")
    sheet_transfer_alerts: str = Field(default="Перемещения")
    sheet_distribution: str = Field(default="Бестселлеры к распределению")
    sheet_stock_raw: str = Field(default="Остатки по складам")

    # --- Warehouse / store topology ----------------------------------------
    # Retail points we want to keep stocked. Semicolon-separated in .env.
    target_stores_csv: str = Field(default="Авиапарк ТЦ;Европейский ТЦ")
    # Service warehouses (примерка, химчистка, ремонт): their stock counts as
    # on-hand — those garments come back into circulation — but they are never
    # replenished, so they must not pull anything into production.
    stock_only_stores_csv: str = Field(
        default="ЖаПримерка;Склад химчистка;Склад ремонт;Химчистка Ави")
    finished_goods_warehouse: str = Field(default="ЕСклад ГП")  # confirmed from real 1C export

    # --- Planning model -----------------------------------------------------
    # "stores" — keep TARGET_STORE_STOCK units of every high-priority item at
    #            every retail point (the original rule).
    # "sales"  — replace what was sold: demand = units sold in the rating period
    #            minus everything already on hand. Far smaller volumes.
    planning_mode: str = Field(default="stores")

    # --- Deficit thresholds -------------------------------------------------
    target_store_stock: int = Field(default=3, ge=0)  # desired qty per store
    min_store_stock: int = Field(default=1, ge=0)     # on_hand < this => "missing"

    # --- Priority (what counts as a "high-rating" item) ---------------------
    high_priority_top_n: int = Field(default=50, ge=1)        # top-N by revenue
    min_revenue_threshold: float = Field(default=0.0, ge=0)   # OR revenue floor

    # --- Google API retry / exponential backoff -----------------------------
    max_retries: int = Field(default=5, ge=0)
    backoff_initial: float = Field(default=1.0, gt=0)
    backoff_base: float = Field(default=2.0, gt=1.0)
    backoff_max: float = Field(default=60.0, gt=0)

    # --- Product-name mapping overrides ------------------------------------
    mappings_file: Path = Field(default=DEFAULT_MAPPINGS_FILE)

    # ----------------------------------------------------------------------- #
    @field_validator("spreadsheet_id")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _resolve_relative_paths(self):
        """Resolve relative credential/mapping paths against BASE_DIR.

        Lets ``.env`` say ``GOOGLE_CREDENTIALS_FILE=credentials.json`` and have it
        found next to the .exe (or project dir), regardless of the working
        directory the app was launched from.
        """
        if not self.google_credentials_file.is_absolute():
            self.google_credentials_file = BASE_DIR / self.google_credentials_file
        if not self.mappings_file.is_absolute():
            self.mappings_file = BASE_DIR / self.mappings_file
        return self

    @field_validator("planning_mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"stores", "sales"}:
            raise ValueError(f"PLANNING_MODE must be 'stores' or 'sales', got {v!r}")
        return v

    @property
    def target_stores(self) -> List[str]:
        """Parsed retail-store list (semicolon separated in the env var)."""
        return [s.strip() for s in self.target_stores_csv.split(";") if s.strip()]

    @property
    def stock_only_stores(self) -> List[str]:
        """Warehouses counted as on-hand but never replenished."""
        return [s.strip() for s in self.stock_only_stores_csv.split(";") if s.strip()]

    @property
    def counted_stores(self) -> List[str]:
        """Every location whose stock offsets demand (excludes ГП, tracked apart)."""
        return self.target_stores + self.stock_only_stores

    def require_runtime_values(self) -> None:
        """Fail fast if mandatory runtime values are absent.

        Called by ``main`` only for operations that actually talk to Google
        Sheets, so ``--help`` / unit tests do not need real credentials.
        """
        problems: List[str] = []
        if not self.spreadsheet_id:
            problems.append("SPREADSHEET_ID is not set (.env).")
        if not self.google_credentials_file.exists():
            problems.append(
                f"Credentials file not found: {self.google_credentials_file}"
            )
        if not self.target_stores:
            problems.append("TARGET_STORES_CSV resolved to an empty list.")
        if problems:
            raise ValueError("Configuration error(s):\n  - " + "\n  - ".join(problems))


@lru_cache
def get_settings() -> Settings:
    """Return a cached, validated Settings instance."""
    return Settings()


def update_env(values: dict[str, str], env_path: Path | None = None) -> Path:
    """Rewrite the given keys in ``.env``, preserving comments and everything else.

    The manager edits warehouses from the UI, so the file must survive round
    trips: existing keys are replaced in place, unknown ones appended, and every
    comment line the operator may rely on is left untouched. The settings cache
    is dropped so the next read sees the new values.
    """
    path = env_path or (BASE_DIR / ".env")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    remaining = dict(values)
    out: List[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key.upper() in {k.upper() for k in remaining}:
                match = next(k for k in remaining if k.upper() == key.upper())
                out.append(f"{key}={remaining.pop(match)}")
                continue
        out.append(line)

    for key, value in remaining.items():
        out.append(f"{key}={value}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    get_settings.cache_clear()
    return path
