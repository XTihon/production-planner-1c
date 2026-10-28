"""
Unit tests for the deficit engine. No network, no files, no credentials —
this is the payoff of keeping analyzer.py pure.

    python -m pytest
"""
from analyzer import AnalysisInputs, RatingInfo, run_analysis
from config import Settings

AVIA = "Авиапарк ТЦ"
EVRO = "Европейский ТЦ"
GP = "Е Склад ГП"


def _cfg() -> Settings:
    # Explicit overrides => deterministic regardless of any local .env.
    return Settings(
        target_stores_csv=f"{AVIA};{EVRO}",
        finished_goods_warehouse=GP,
        target_store_stock=3,
        min_store_stock=1,
        high_priority_top_n=50,
        min_revenue_threshold=0,
    )


def _inputs() -> AnalysisInputs:
    inp = AnalysisInputs()
    inp.ratings = {
        "A": RatingInfo("корсет асти", 1000.0, 1),  # missing everywhere, ГП=1
        "C": RatingInfo("топ ева", 800.0, 2),       # missing, but ГП covers fully
        "D": RatingInfo("боди лея", 700.0, 3),       # missing, already cut
        "B": RatingInfo("платье мия", 500.0, 4),     # fully stocked
    }
    inp.names = {k: v.name for k, v in inp.ratings.items()}
    inp.stock = {
        "A": {AVIA: 0, EVRO: 0, GP: 1},
        "C": {AVIA: 0, EVRO: 0, GP: 10},
        "D": {AVIA: 0, EVRO: 0, GP: 0},
        "B": {AVIA: 5, EVRO: 5, GP: 0},
    }
    inp.ready_cut = {"D": 4, "B": 4}  # B is cut but not needed
    return inp


def test_transfer_when_gp_has_stock():
    res = run_analysis(_inputs(), _cfg())
    # A pulls its single ГП unit to one store; C is fully covered from ГП.
    a_transfers = [t for t in res.transfers if t.sku == "A"]
    c_transfers = [t for t in res.transfers if t.sku == "C"]
    assert sum(t.qty for t in a_transfers) == 1
    assert sum(t.qty for t in c_transfers) == 6  # 3 + 3, fully from ГП


def test_cut_when_missing_in_stores_and_gp():
    res = run_analysis(_inputs(), _cfg())
    cut = {t.sku: t.qty for t in res.cut_tasks}
    assert cut["A"] == 5          # need 6, 1 transferred, 5 to cut
    assert cut["D"] == 2          # need 6, 4 sewn from ready-cut, 2 to cut
    assert "C" not in cut         # fully satisfied from ГП -> no cutting
    assert "B" not in cut         # fully stocked -> nothing


def test_sew_uses_already_cut_before_cutting():
    res = run_analysis(_inputs(), _cfg())
    sew = {t.sku: t.qty for t in res.sew_tasks}
    assert sew["D"] == 4          # all 4 ready-cut pieces get sewn
    assert "A" not in sew         # nothing pre-cut for A
    assert "B" not in sew         # cut, but no deficit -> not sewn


def test_fully_stocked_item_produces_nothing():
    res = run_analysis(_inputs(), _cfg())
    assert all(t.sku != "B" for t in res.transfers)
    assert all(t.sku != "B" for t in res.cut_tasks)
    assert all(t.sku != "B" for t in res.sew_tasks)


def test_ordering_is_by_revenue_desc():
    res = run_analysis(_inputs(), _cfg())
    revenues = [t.revenue for t in res.cut_tasks]
    assert revenues == sorted(revenues, reverse=True)


def _sales_cfg(**over):
    """Settings for the 'sales' model, with the store split the client asked for."""
    from config import Settings
    base = dict(planning_mode="sales",
                target_stores_csv="Авиапарк ТЦ;Европейский ТЦ",
                stock_only_stores_csv="Склад химчистка;Склад ремонт",
                finished_goods_warehouse="ЕСклад ГП",
                spreadsheet_id="x")
    base.update(over)
    return Settings(**base)


def test_sales_mode_produces_only_the_shortfall():
    """Sold 5, three already on hand -> make 2, not 5 and not a shelf-full."""
    from analyzer import AnalysisInputs, RatingInfo, run_analysis
    inputs = AnalysisInputs()
    inputs.sold = {"корсет|S": 5}
    inputs.stock = {"корсет|S": {"Авиапарк ТЦ": 2, "Склад химчистка": 1}}
    inputs.names = {"корсет|S": "Корсет, S"}
    inputs.ratings = {"корсет|S": RatingInfo("Корсет, S", 1000.0, 1)}

    r = run_analysis(inputs, _sales_cfg())
    assert [(t.name, t.characteristic, t.qty) for t in r.cut_tasks] == [("Корсет", "S", 2)]
    assert r.sew_tasks == []


def test_sales_mode_counts_service_warehouses_as_stock():
    """Everything sold is sitting in химчистка/ремонт — produce nothing."""
    from analyzer import AnalysisInputs, run_analysis
    inputs = AnalysisInputs()
    inputs.sold = {"корсет|M": 4}
    inputs.stock = {"корсет|M": {"Склад химчистка": 3, "Склад ремонт": 1}}
    inputs.names = {"корсет|M": "Корсет, M"}

    r = run_analysis(inputs, _sales_cfg())
    assert r.cut_tasks == [] and r.sew_tasks == []


def test_sales_mode_sews_before_cutting():
    from analyzer import AnalysisInputs, run_analysis
    inputs = AnalysisInputs()
    inputs.sold = {"корсет|L": 6}
    inputs.stock = {"корсет|L": {"Авиапарк ТЦ": 1}}
    inputs.ready_cut = {"корсет|L": 3}
    inputs.names = {"корсет|L": "Корсет, L"}

    r = run_analysis(inputs, _sales_cfg())
    assert [(t.name, t.characteristic, t.qty) for t in r.sew_tasks] == [("Корсет", "L", 3)]
    assert [(t.name, t.characteristic, t.qty) for t in r.cut_tasks] == [("Корсет", "L", 2)]


def test_service_warehouses_never_pull_production_in_stores_mode():
    """The client's rule: примерка/химчистка are counted, never replenished."""
    from analyzer import AnalysisInputs, RatingInfo, run_analysis
    inputs = AnalysisInputs()
    inputs.stock = {"корсет|S": {"Склад химчистка": 9}}
    inputs.names = {"корсет|S": "Корсет, S"}
    inputs.ratings = {"корсет|S": RatingInfo("Корсет, S", 1000.0, 1)}

    r = run_analysis(inputs, _sales_cfg(planning_mode="stores"))
    assert r.cut_tasks == [], "stock parked in химчистка must offset the deficit"


def test_stock_only_warehouses_are_actually_read_from_the_export(tmp_path):
    """Regression: 'Только учитывать' columns were never read, so химчистка/ремонт
    counted as zero and the planner asked to re-make garments that already existed."""
    import pandas as pd
    from normalizer import ProductMatcher
    from pipeline_1c import load_stock_variants

    path = tmp_path / "stock.xlsx"
    pd.DataFrame([
        ["", "", "", "", "", "", "", "", "", ""],
        ["Остатки товаров на складах", "", "", "", "", "", "", "", "", ""],
        ["", "", "", "", "", "", "", "", "", ""],
        ["Организация", "", "", "", "", "", "Итого по всем складам",
         "Авиапарк ТЦ", "Склад химчистка", "ЕСклад ГП"],
        ["Номенклатура", "", "", "Характеристика", "", "", "Свободно",
         "Свободно", "Свободно", "Свободно"],
        ["Заказ покупателя", "", "", "", "", "", "", "", "", ""],
        ["Корсет", "", "", "белый, S", "", "", 5, 1, 3, 1],
    ]).to_excel(path, header=False, index=False)

    cfg = _sales_cfg(target_stores_csv="Авиапарк ТЦ",
                     stock_only_stores_csv="Склад химчистка")
    variants, stats = load_stock_variants(path, "Sheet1", ProductMatcher(), cfg)

    assert len(variants) == 1
    qty = variants[0].store_qty
    assert qty.get("Авиапарк ТЦ") == 1
    assert qty.get("Склад химчистка") == 3, "service warehouse must be read"
    assert qty.get("ЕСклад ГП") == 1


def test_service_stock_offsets_demand_in_sales_mode():
    """Sold 2, one on the shop floor and one in химчистка -> nothing to make."""
    from analyzer import AnalysisInputs, run_analysis
    inputs = AnalysisInputs()
    inputs.sold = {"корсет|S": 2}
    inputs.stock = {"корсет|S": {"Авиапарк ТЦ": 1, "Склад химчистка": 1}}
    inputs.names = {"корсет|S": "Корсет, S"}

    r = run_analysis(inputs, _sales_cfg(target_stores_csv="Авиапарк ТЦ",
                                        stock_only_stores_csv="Склад химчистка"))
    assert r.cut_tasks == [] and r.sew_tasks == []


def test_plan_rows_carry_the_arithmetic():
    """Sheets must show продано / в наличии so 'why only 1?' answers itself."""
    from analyzer import AnalysisInputs, run_analysis
    inputs = AnalysisInputs()
    inputs.sold = {"корсет|S": 3}
    inputs.stock = {"корсет|S": {"Авиапарк ТЦ": 1}}
    inputs.names = {"корсет|S": "Корсет, S"}
    inputs.parts = {"корсет|S": ("Корсет", "белый, S")}

    task = run_analysis(inputs, _sales_cfg(target_stores_csv="Авиапарк ТЦ")).cut_tasks[0]
    assert (task.name, task.characteristic) == ("Корсет", "белый, S")
    assert (task.sold, task.on_hand, task.qty) == (3, 1, 2)
