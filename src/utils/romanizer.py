import re
from typing import Optional

try:
    import pykakasi
    _kks = pykakasi.kakasi()
except ImportError:
    _kks = None

try:
    import anyascii
except ImportError:
    anyascii = None


def is_japanese(text: str) -> bool:
    """Check if text contains Japanese Hiragana, Katakana, or Japanese punctuation."""
    return bool(re.search(r"[\u3040-\u309f\u30a0-\u30ff\u3000-\u303f]", text))


def romanize_text(text: str) -> str:
    """Universal Romanization:
    - Japanese (Hiragana/Katakana/Kanji) -> Hepburn Romaji with capitalization
    - Chinese (Hanzi) -> Pinyin
    - Korean (Hangul) -> Romaja
    - Cyrillic / Russian -> Latin
    - Other scripts -> Latin ASCII transliteration
    """
    if not text:
        return ""

    cleaned = text.strip()

    # 1. Japanese conversion using pykakasi
    if is_japanese(cleaned) and _kks:
        try:
            converted = _kks.convert(cleaned)
            parts = [item["hepburn"].capitalize() for item in converted if item.get("hepburn")]
            res = " ".join(parts).strip()
            if res:
                return res
        except Exception:
            pass

    # 2. Universal transliteration via anyascii (Chinese, Korean, Cyrillic, Arabic, etc.)
    if anyascii:
        try:
            translit = anyascii.anyascii(cleaned)
            res = re.sub(r"\s+", " ", translit).strip()
            if res:
                return res
        except Exception:
            pass

    # 3. Fallback to pykakasi for pure-kanji Japanese if anyascii was not used
    if _kks:
        try:
            converted = _kks.convert(cleaned)
            parts = [item["hepburn"].capitalize() for item in converted if item.get("hepburn")]
            res = " ".join(parts).strip()
            if res:
                return res
        except Exception:
            pass

    return cleaned
