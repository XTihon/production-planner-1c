"""The warehouse editor writes .env back — that file must survive round trips."""
from pathlib import Path

from config import update_env


def test_update_env_replaces_values_and_keeps_everything_else(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "# Настройки\n"
        "SPREADSHEET_ID=abc123\n"
        "\n"
        "# --- Склады ---\n"
        "TARGET_STORES_CSV=Старая точка\n"
        "PLANNING_MODE=sales\n",
        encoding="utf-8")

    update_env({"TARGET_STORES_CSV": "Авиапарк ТЦ;Европейский ТЦ",
                "STOCK_ONLY_STORES_CSV": "Склад ремонт"}, env_path=env)
    out = env.read_text(encoding="utf-8")

    assert "TARGET_STORES_CSV=Авиапарк ТЦ;Европейский ТЦ" in out
    assert "STOCK_ONLY_STORES_CSV=Склад ремонт" in out   # appended
    assert "SPREADSHEET_ID=abc123" in out                # untouched
    assert "PLANNING_MODE=sales" in out
    assert "# Настройки" in out and "# --- Склады ---" in out
    assert "Старая точка" not in out


def test_update_env_creates_the_file_when_missing(tmp_path: Path):
    env = tmp_path / ".env"
    update_env({"TARGET_STORES_CSV": "Авиапарк ТЦ"}, env_path=env)
    assert env.read_text(encoding="utf-8").strip() == "TARGET_STORES_CSV=Авиапарк ТЦ"
