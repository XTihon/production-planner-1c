"""
normalizer.py
=============
The matching layer. 1C and Google Sheets rarely spell a product the same way:

    "Корсет Асти беж L"   vs   "Корсет 'Асти' (бежевый), размер L"

We resolve both to a single canonical SKU using a two-stage strategy:

1. **Strict mapping (authoritative).** ``mappings.json`` maps an exact raw
   string (or its normalised form) to a canonical SKU. Anything a human has
   pinned here wins, no questions asked.

2. **Algorithmic normalisation (fallback).** Lower-case, unify ``ё→е``, drop
   punctuation/quotes/keywords, expand colour abbreviations, and pull the size
   token out. The remaining tokens are sorted so word-order differences don't
   matter. Result: ``корсет асти|бежевый|L``.

Anything that still can't be confidently parsed is reported as *unmatched* —
never silently dropped — so an operator can add it to ``mappings.json``.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from logger import get_logger

log = get_logger("normalizer")

# Words that carry no identifying value and only add noise.
_STOPWORDS = {"размер", "разм", "р", "цвет", "арт", "артикул", "шт"}

# Colour abbreviations -> canonical colour. Extend freely in mappings.json too.
_COLOR_ALIASES = {
    "беж": "бежевый",
    "бел": "белый",
    "чер": "черный",
    "черн": "черный",
    "крас": "красный",
    "син": "синий",
    "зел": "зеленый",
    "роз": "розовый",
    "сер": "серый",
    "корич": "коричневый",
    "бордо": "бордовый",
}

# Recognised size tokens (letter sizes + RU numeric grid).
_LETTER_SIZES = {"XS", "S", "M", "L", "XL", "XXL", "XXXL"}

# Sizes typed by hand arrive in whatever keyboard layout was active: Cyrillic
# 'с'/'м'/'л'/'хс', or latin 'xc'. 1C always emits the latin grid, so both sides
# must be folded onto it. Latin 'C' maps to 'S' too — it is never a real size
# letter, it is someone typing "эс".
_SIZE_CHAR_FIX = str.maketrans({
    "С": "S", "C": "S",
    "М": "M",
    "Л": "L",
    "Х": "X",
})
_CANONICAL_SIZES = {"XS", "S", "M", "L", "XL", "XXL", "XXXL", "3XL", "4XL"}
_SIZE_RE = re.compile(
    r"\b(XS|S|M|L|XL|XXL|XXXL|3XL|4XL|\d{2})\b", flags=re.IGNORECASE
)
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


@dataclass
class MatchResult:
    sku: str
    name: str           # human-readable canonical name (without size)
    size: Optional[str]
    matched_by: str     # "mapping" | "normalized"


@dataclass
class ProductMatcher:
    """Resolves raw product strings to canonical SKUs."""

    mapping: Dict[str, str] = field(default_factory=dict)
    _unmatched: Dict[str, int] = field(default_factory=dict)

    # ----------------------------------------------------------------- build
    @classmethod
    def from_file(cls, path: Path) -> "ProductMatcher":
        """Load the strict mapping file. A missing file is tolerated (warn)."""
        mapping: Dict[str, str] = {}
        try:
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                # Normalise the *keys* so lookups are robust to spacing/case.
                mapping = {cls._normalize_text(k): str(v) for k, v in raw.items()}
                log.info("Loaded %d explicit product mappings from %s",
                         len(mapping), path.name)
            else:
                # Absence is the normal case: the algorithm handles the usual
                # spelling drift, and the file only exists once a human has had
                # to pin something down. Not a problem to shout about.
                log.info("Словарь соответствий %s не найден — названия "
                         "сопоставляются автоматически.", path.name)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Could not read mapping file %s: %s — continuing with "
                      "algorithmic normalisation only.", path, exc)
        return cls(mapping=mapping)

    # ----------------------------------------------------------------- match
    def match(self, raw_name: str) -> Optional[MatchResult]:
        """Return a MatchResult, or ``None`` if the string can't be parsed."""
        if not raw_name or not raw_name.strip():
            return None

        norm = self._normalize_text(raw_name)

        # 1. Strict mapping wins.
        if norm in self.mapping:
            sku = self.mapping[norm]
            size = self._extract_size(norm)[1]
            return MatchResult(sku=sku, name=sku, size=size, matched_by="mapping")

        # 2. Algorithmic fallback.
        body, size = self._extract_size(norm)
        tokens = self._tokenize(body)
        if not tokens:
            self._record_unmatched(raw_name)
            return None

        name = " ".join(tokens)
        sku = f"{name}|{size or '-'}"
        return MatchResult(sku=sku, name=name, size=size, matched_by="normalized")

    def match_or_warn(self, raw_name: str) -> Optional[MatchResult]:
        res = self.match(raw_name)
        if res is None:
            self._record_unmatched(raw_name)
        return res

    # --------------------------------------------------- structured matching
    @classmethod
    def split_characteristic(cls, characteristic: str):
        """Split a 1C 'Характеристика' field into (colour, size).

        The size is the segment after the LAST comma — never guessed with a
        regex over the whole string, which used to grab '90' out of
        'Корсет "90/60"'. When the comma is missing, the last word is taken only
        if it is a real size token.

            'пыльно-розовый, XS' -> ('пыльно-розовый', 'XS')
            'бежевый, M'         -> ('бежевый', 'M')
            'прозрачный, one size' -> ('прозрачный', 'one size')
            'черный S'           -> ('черный', 'S')
            'черный'             -> ('черный', None)
        """
        if characteristic is None:
            return "", None
        s = str(characteristic).strip()
        if not s:
            return "", None
        if "," in s:
            colour, size = s.rsplit(",", 1)
            return colour.strip(), (size.strip() or None)
        # No comma — operators sometimes type 'черный S'. Peel the last word off
        # only when it really is a size, so 'Корсет 90/60' or a plain 'белый'
        # are left alone.
        head, _, tail = s.rpartition(" ")
        if head and cls._canonical_size(tail) in _CANONICAL_SIZES:
            return head.strip(), tail.strip()
        return s, None

    def match_structured(self, name: str, colour, size) -> MatchResult:
        """Build a canonical SKU from already-separated name / colour / size.

        Used by the 1C pipeline, where colour and size come from dedicated
        columns. The model key = sorted, de-noised tokens of (name + colour);
        the size is kept verbatim (never inferred from digits in the name).
        Both the rating (name+colour, no size) and the stock (name+colour+size)
        sides go through this, so a model and its variants share the same key.
        """
        name_n = self._normalize_text(name or "")
        colour_n = self._normalize_text(colour or "")
        tokens = self._tokenize(f"{name_n} {colour_n}".strip())
        model = " ".join(tokens)
        size_n = self._canonical_size(size)
        sku = f"{model}|{size_n or '-'}"
        return MatchResult(sku=sku, name=model, size=size_n, matched_by="structured")

    # ------------------------------------------------------------- reporting
    @property
    def unmatched(self) -> Dict[str, int]:
        """Raw strings that could not be matched, with occurrence counts."""
        return dict(self._unmatched)

    def _record_unmatched(self, raw_name: str) -> None:
        key = raw_name.strip()
        self._unmatched[key] = self._unmatched.get(key, 0) + 1

    # ------------------------------------------------------------- internals
    @staticmethod
    def _normalize_text(value: str) -> str:
        """Lower-case, unify ё→е, strip punctuation & collapse whitespace."""
        value = unicodedata.normalize("NFKC", str(value))
        value = value.replace("ё", "е").replace("Ё", "Е")
        value = value.lower()
        value = _PUNCT_RE.sub(" ", value)
        value = _WS_RE.sub(" ", value).strip()
        return value

    @classmethod
    def _canonical_size(cls, size) -> Optional[str]:
        """Fold a size token onto the latin grid 1C uses.

        The substitution is applied only when it actually lands on a real size,
        so free-text characteristics ('one size', 'индив пошив') survive intact
        instead of being mangled letter by letter.
        """
        if size in (None, ""):
            return None
        s = str(size).strip().upper()
        if not s or s in _CANONICAL_SIZES:
            return s or None
        fixed = s.translate(_SIZE_CHAR_FIX)
        return fixed if fixed in _CANONICAL_SIZES else s

    @classmethod
    def _extract_size(cls, norm_text: str):
        """Split a normalised string into (body_without_size, size|None)."""
        size: Optional[str] = None
        m = _SIZE_RE.search(norm_text)
        if m:
            token = m.group(1).upper()
            size = token if token in _LETTER_SIZES or token.endswith("XL") else token
            norm_text = (norm_text[: m.start()] + " " + norm_text[m.end():]).strip()
        return norm_text, size

    @classmethod
    def _tokenize(cls, body: str) -> List[str]:
        """Produce sorted, de-noised, colour-normalised tokens."""
        tokens: List[str] = []
        for tok in body.split():
            if tok in _STOPWORDS:
                continue
            tokens.append(_COLOR_ALIASES.get(tok, tok))
        # Sort so word order never affects the key (set-like comparison).
        return sorted(set(tokens))
