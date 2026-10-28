"""
Tests for the matching layer — the requirement that
"Корсет Асти беж L" and "Корсет 'Асти' (бежевый), размер L" resolve identically.
"""
from pathlib import Path

from normalizer import ProductMatcher


def test_algorithmic_match_reconciles_spelling_variants():
    m = ProductMatcher()  # no mapping file -> pure normalisation
    a = m.match("Корсет Асти беж L")
    b = m.match("Корсет 'Асти' (бежевый), размер L")
    assert a is not None and b is not None
    assert a.sku == b.sku
    assert a.size == "L"
    assert a.matched_by == "normalized"


def test_size_is_separated_from_name():
    m = ProductMatcher()
    s = m.match("Боди Лея чер S")
    assert s.size == "S"
    assert "s" not in s.name.split()  # size token removed from the name


def test_strict_mapping_overrides_algorithm(tmp_path: Path):
    mapping_file = tmp_path / "mappings.json"
    mapping_file.write_text(
        '{"Корсет Асти беж L": "CANON-123"}', encoding="utf-8"
    )
    m = ProductMatcher.from_file(mapping_file)
    res = m.match("Корсет Асти беж L")
    assert res.sku == "CANON-123"
    assert res.matched_by == "mapping"


def test_unmatched_is_recorded_not_dropped_silently():
    m = ProductMatcher()
    assert m.match("") is None
    m.match_or_warn("")  # blank -> unmatched bucket via match_or_warn
    # A string of only punctuation has no tokens -> unmatched.
    assert m.match("!!! ??? ...") is None
    assert m.unmatched  # something was recorded


def test_split_characteristic():
    assert ProductMatcher.split_characteristic("пыльно-розовый, XS") == ("пыльно-розовый", "XS")
    assert ProductMatcher.split_characteristic("бежевый, M") == ("бежевый", "M")
    assert ProductMatcher.split_characteristic("прозрачный, one size") == ("прозрачный", "one size")
    assert ProductMatcher.split_characteristic("черный") == ("черный", None)


def test_structured_match_fixes_numeric_name_bug():
    """'Корсет "90/60"' must keep 90/60 in the NAME; size comes from the field."""
    m = ProductMatcher()
    colour, size = m.split_characteristic("бежевый, M")
    res = m.match_structured('Корсет "90/60"', colour, size)
    assert res.size == "M"                 # size from field, NOT the digits
    assert "90" in res.name and "60" in res.name  # digits stayed in the model name
    assert res.sku.endswith("|M")


def test_structured_model_and_variant_share_key():
    """Rating (name+colour) and stock (name+colour+size) join on the model key."""
    m = ProductMatcher()
    model = m.match_structured('Корсет "Асти"', "бежевый", None)      # rating side
    variant = m.match_structured('Корсет "Асти"', "бежевый", "L")     # stock side
    assert model.name == variant.name      # same model key -> they reconcile
    assert variant.sku == f"{model.name}|L"


def test_structured_compound_colour_normalized_consistently():
    m = ProductMatcher()
    rating = m.match_structured('Корсет "Алмаз"', "пыльно-розовый", None)
    colour, size = m.split_characteristic("пыльно-розовый, S")
    stock = m.match_structured('Корсет "Алмаз"', colour, size)
    assert rating.name == stock.name       # compound colour matches on both sides


def test_cyrillic_sizes_fold_onto_the_latin_grid():
    """Ready-cut is typed by hand ('с', 'хс', even latin 'xc'); 1C emits 'S'/'XS'.
    Both must produce the same SKU or already-cut stock gets re-cut."""
    m = ProductMatcher()
    from_1c = m.match_structured("Корсет \"Лотус\"", "белый", "S")
    for typed in ("с", "С", "c"):
        assert m.match_structured("Корсет \"Лотус\"", "белый", typed).sku == from_1c.sku

    xs_1c = m.match_structured("Корсет \"Лотус\"", "белый", "XS")
    for typed in ("хс", "ХС", "xc"):
        assert m.match_structured("Корсет \"Лотус\"", "белый", typed).sku == xs_1c.sku

    assert m.match_structured("Корсет", "белый", "м").size == "M"
    assert m.match_structured("Корсет", "белый", "л").size == "L"


def test_free_text_characteristics_survive_size_folding():
    """'one size' / 'индив пошив' are not sizes — they must not be letter-mangled."""
    m = ProductMatcher()
    assert m.match_structured("Блуза из органзы", "белый", "one size").size == "ONE SIZE"
    assert m.match_structured("Платье \"Фатал\"", "персиковый", "индив пошив").size == "ИНДИВ ПОШИВ"


def test_size_without_a_comma_is_still_separated():
    """1C sometimes emits 'черный S' with no comma — the size must not glue to the colour."""
    m = ProductMatcher()
    assert m.split_characteristic("черный S") == ("черный", "S")
    assert m.split_characteristic("черный с") == ("черный", "с")
    # ...but only when the last word really is a size:
    assert m.split_characteristic("белый") == ("белый", None)
    assert m.split_characteristic("пыльно-розовый") == ("пыльно-розовый", None)
    assert m.split_characteristic('Корсет 90/60') == ("Корсет 90/60", None)
