"""Unit tests for chapter title normalization, reverse order detection, and content disclaimer cleaning."""

import pytest
from src.utils.title_cleaner import (
    clean_chapter_title,
    clean_chapter_content,
    extract_chapter_number,
    detect_and_fix_reverse_order,
)


class DummyChapter:
    """Mock chapter link for testing ordering."""
    def __init__(self, index: int, title: str, url: str):
        self.index = index
        self.title = title
        self.url = url


def test_clean_chapter_title_duplicate_labels_and_date():
    raw = "Vol. 1 Ch. 169 Chapter 169 September 23, 2026"
    assert clean_chapter_title(raw) == "Vol. 1 Ch. 169"

    raw2 = "Chapter 50 Chapter 50 August 15, 2025"
    assert clean_chapter_title(raw2) == "Chapter 50"


def test_clean_chapter_title_missing_space_and_views():
    raw = "Ch.1I Think I've Been Possessed by the Protagonist Who Becomes a Saint62,826"
    expected = "Ch. 1 - I Think I've Been Possessed by the Protagonist Who Becomes a Saint"
    assert clean_chapter_title(raw) == expected


def test_clean_chapter_title_subpage_and_trailing_views():
    raw = "Ch.2Is This Possession or Reincarnation (1)22,747"
    expected = "Ch. 2 - Is This Possession or Reincarnation (1)"
    assert clean_chapter_title(raw) == expected


def test_clean_chapter_title_relative_dates():
    assert clean_chapter_title("Chapter 42 - 3 hours ago") == "Chapter 42"
    assert clean_chapter_title("Chapter 10 2026-05-12") == "Chapter 10"


def test_clean_chapter_title_empty_or_whitespace():
    assert clean_chapter_title("") == "Chapter"
    assert clean_chapter_title("   ") == "Chapter"


def test_extract_chapter_number():
    assert extract_chapter_number("Vol. 1 Ch. 169") == 169.0
    assert extract_chapter_number("Ch. 5.5 Side Story") == 5.5
    assert extract_chapter_number("Unknown Title", "https://site.com/series/novel/chapter-42") == 42.0
    assert extract_chapter_number("Episode 99") == 99.0


def test_detect_and_fix_reverse_order_reversed():
    chapters = [
        DummyChapter(1, "Chapter 10", "https://site.com/ch-10"),
        DummyChapter(2, "Chapter 9", "https://site.com/ch-9"),
        DummyChapter(3, "Chapter 8", "https://site.com/ch-8"),
        DummyChapter(4, "Chapter 1", "https://site.com/ch-1"),
    ]
    reordered, was_inverted = detect_and_fix_reverse_order(chapters)
    assert was_inverted is True
    assert [ch.title for ch in reordered] == ["Chapter 1", "Chapter 8", "Chapter 9", "Chapter 10"]
    assert [ch.index for ch in reordered] == [1, 2, 3, 4]


def test_detect_and_fix_reverse_order_already_ascending():
    chapters = [
        DummyChapter(1, "Chapter 1", "https://site.com/ch-1"),
        DummyChapter(2, "Chapter 2", "https://site.com/ch-2"),
        DummyChapter(3, "Chapter 3", "https://site.com/ch-3"),
    ]
    reordered, was_inverted = detect_and_fix_reverse_order(chapters)
    assert was_inverted is False
    assert [ch.title for ch in reordered] == ["Chapter 1", "Chapter 2", "Chapter 3"]


def test_clean_chapter_content_disclaimer_and_paywall():
    paras = [
        "Lin Donglin was an ordinary Foundation Establishment disciple of Clear Ripple Peak.",
        "This is a PREMIUM chapter. It will be available shortly but if you would like to read it now, please subscribe to ourPREMIUM membership plan. IF YOU ARE A MEMBER THEN LOGIN FIRST PLEASE.LOGIN HERE",
        "Over the many years that followed, she diligently studied the medical arts.",
        "Disclaimer: This siteLight Novels AIdoes not store any files on its server. All contents are provided by non-affiliated third parties."
    ]
    cleaned, is_paywalled = clean_chapter_content(paras)
    assert len(cleaned) == 2
    assert "Lin Donglin" in cleaned[0]
    assert "Over the many years" in cleaned[1]
    assert is_paywalled is True
