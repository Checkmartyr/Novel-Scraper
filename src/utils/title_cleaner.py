"""Utility functions for chapter title normalization, reverse order detection, and content disclaimer cleaning."""

import re
from typing import List, Optional, Tuple, Any
from urllib.parse import urlparse


# Regex patterns for trailing publication dates
DATE_PATTERNS = [
    re.compile(
        r"[\s\xa0\-\|—]*\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b.*$",
        re.I,
    ),
    re.compile(r"[\s\xa0\-\|—]*\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b.*$", re.I),
    re.compile(r"[\s\xa0\-\|—]*\b\d+\s+(?:hours?|days?|weeks?|months?|years?|mins?|minutes?|secs?|seconds?)\s+ago\b.*$", re.I),
]

# Regex patterns for trailing stats / view counts / word counts (e.g. 62,826 or 19,360)
TRAILING_STATS_PATTERNS = [
    re.compile(r"([A-Za-z\)\'\"])\s*(\d{1,3}(?:,\d{3})+)$"),  # Attached or spaced comma number e.g. Saint62,826, (1)22,747
    re.compile(r"[\s\xa0]+(\d{1,3}(?:,\d{3})+)$"),           # Standalone comma number at end e.g. 62,826
]

# Common aggregator / site disclaimers
DISCLAIMER_PATTERNS = [
    re.compile(r"^(?:disclaimer\s*:?\s*)?this\s+site.*?does\s+not\s+store\s+any\s+files", re.I),
    re.compile(r"^all\s+contents\s+are\s+provided\s+by\s+non-affiliated\s+third\s+parties", re.I),
    re.compile(r"^follow\s+us\s+on\s+(?:discord|twitter|facebook|patreon|telegram)", re.I),
    re.compile(r"^read\s+latest\s+chapters\s+at\s+[\w\.\-]+(?:\s+only)?", re.I),
    re.compile(r"^if\s+you\s+find\s+any\s+errors\s*,\s*please\s+report\s+them", re.I),
    re.compile(r"^(?:this\s+is\s+a\s+)?premium\s+chapter.*?subscribe\s+to\s+our\s*premium", re.I),
    re.compile(r"^if\s+you\s+are\s+a\s+member\s+then\s+login\s+first", re.I),
]


def clean_chapter_title(raw_title: str) -> str:
    """Normalize and clean messy chapter titles.
    
    Removes:
    - Trailing publication dates (e.g. 'September 23, 2026')
    - Trailing view counts / stats (e.g. '62,826')
    - Subpage indicators (e.g. '(1/2)')
    - Redundant duplicated chapter tags (e.g. 'Vol. 1 Ch. 169 Chapter 169')
    - Fixes missing spacing between prefix and title (e.g. 'Ch.1I Think' -> 'Ch. 1 - I Think')
    """
    if not raw_title:
        return "Chapter"

    title = raw_title.strip()

    # 1. Strip trailing dates
    for pat in DATE_PATTERNS:
        title = pat.sub("", title).strip()

    # 2. Strip trailing view counts / stats (e.g. Saint62,826 -> Saint, (1)22,747 -> (1))
    for pat in TRAILING_STATS_PATTERNS:
        if pat.groups == 2:
            title = pat.sub(r"\1", title).strip()
        else:
            title = pat.sub("", title).strip()

    # Strip standalone trailing 4+ digit stats if not preceded by chapter prefix
    m_num = re.search(r"[\s\xa0]+(\d{4,})$", title)
    if m_num:
        prefix_check = title[:m_num.start()].lower()
        if not re.search(r"\b(?:chapter|ch|episode|ep|vol|volume|part)\s*$", prefix_check):
            title = title[:m_num.start()].strip()

    # 3. Strip subpage pagination indicators (1/2, 2/5, etc.)
    title = re.sub(r"\s*[（\(\[]\s*\d+\s*/\s*\d+\s*[）\)\]]", "", title).strip()

    # 4. Remove duplicate chapter labels (e.g. 'Vol. 1 Ch. 169 Chapter 169' -> 'Vol. 1 Ch. 169')
    title = re.sub(r"\b(Vol\.?\s*\d+\s+Ch\.?\s*\d+)\s+Chapter\s*\d+\b", r"\1", title, flags=re.I)
    title = re.sub(r"\b(Chapter\s*\d+)\s+Chapter\s*\d+\b", r"\1", title, flags=re.I)
    title = re.sub(r"\b(Ch\.?\s*\d+)\s+Ch\.?\s*\d+\b", r"\1", title, flags=re.I)

    # 5. Fix missing spacing between prefix and title text
    # e.g. 'Ch.1I Think' -> 'Ch. 1 - I Think'
    title = re.sub(r"\b(Ch(?:apter)?\.?)\s*(\d+)\s*([A-Za-z])", r"\1 \2 - \3", title, flags=re.I)

    # 6. Normalize chapter numbering whitespace e.g. 'Ch.1' -> 'Ch. 1'
    title = re.sub(r"\b(Ch(?:apter)?\.?)\s*(\d+)\b", r"\1 \2", title, flags=re.I)

    # 7. Collapse whitespace and trim stray trailing/leading punctuation
    title = re.sub(r"[\s\xa0]+", " ", title).strip()
    title = title.strip(" -:|—,.")

    return title or raw_title.strip() or "Chapter"


def extract_chapter_number(title: str, url: str = "") -> Optional[float]:
    """Extract numeric chapter indicator from chapter title or URL path."""
    # 1. Search title for explicit chapter tokens
    m = re.search(r"\b(?:chapter|ch[\.-]?|ep[\.-]?|episode|ตอนที่|ตอน|第)\s*(\d+(?:\.\d+)?)\b", title, re.I)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    # 2. Check Japanese/Chinese kanji chapters (e.g. 第169話)
    m = re.search(r"第\s*(\d+(?:\.\d+)?)\s*[話章节回]", title)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    # 3. Search URL path for chapter indicator
    if url:
        m = re.search(r"/(?:chapter|ch|episode|ep)[-_]?(\d+(?:\.\d+)?)\b", url, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        m = re.search(r"[?&](?:chapter|ch|p|ep)=(\d+(?:\.\d+)?)", url, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        # Numeric leaf e.g. /novel-id/169/
        parsed = urlparse(url)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if parts and parts[-1].isdigit():
            try:
                return float(parts[-1])
            except ValueError:
                pass

    # 4. Fallback: first standalone number in title
    m = re.search(r"\b(\d+(?:\.\d+)?)\b", title)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    return None


def detect_and_fix_reverse_order(chapters: List[Any]) -> Tuple[List[Any], bool]:
    """Detect if chapter list is in reverse chronological order and invert to reading order (1..N).
    
    Returns:
        (ordered_chapters, was_inverted)
    """
    if len(chapters) < 2:
        return chapters, False

    # Extract chapter numbers for all items where possible
    numbers: List[Optional[float]] = [
        extract_chapter_number(ch.title, ch.url) for ch in chapters
    ]

    valid_indices = [i for i, n in enumerate(numbers) if n is not None]
    if len(valid_indices) < 2:
        return chapters, False

    # Compare first valid vs last valid
    first_num = numbers[valid_indices[0]]
    last_num = numbers[valid_indices[-1]]

    # Check overall decreasing trend among valid numbers
    decreases = 0
    increases = 0
    for i in range(len(valid_indices) - 1):
        idx_a = valid_indices[i]
        idx_b = valid_indices[i + 1]
        diff = numbers[idx_b] - numbers[idx_a]
        if diff < 0:
            decreases += 1
        elif diff > 0:
            increases += 1

    total_transitions = decreases + increases
    is_reverse = False

    # If first is significantly greater than last, or sequence is predominantly decreasing
    if first_num is not None and last_num is not None:
        if first_num > last_num and (decreases > increases or decreases >= 2):
            is_reverse = True
        elif total_transitions >= 3 and (decreases / total_transitions) >= 0.7:
            is_reverse = True

    if is_reverse:
        inverted = list(reversed(chapters))
        for idx, ch in enumerate(inverted, start=1):
            ch.index = idx
            ch.title = clean_chapter_title(ch.title)
        return inverted, True
    else:
        # Just clean titles and preserve order
        for idx, ch in enumerate(chapters, start=1):
            ch.index = idx
            ch.title = clean_chapter_title(ch.title)
        return chapters, False


def clean_chapter_content(paragraphs: List[str]) -> Tuple[List[str], bool]:
    """Filter out aggregator disclaimers and paywall teasers from chapter paragraphs.
    
    Returns:
        (cleaned_paragraphs, is_paywalled)
    """
    cleaned: List[str] = []
    is_paywalled = False

    paywall_keywords = [
        "this is a premium chapter",
        "subscribe to our premium",
        "login first please",
        "vip chapter",
        "purchase coins to unlock",
    ]

    for p in paragraphs:
        p_strip = p.strip()
        if not p_strip:
            continue

        lower = p_strip.lower()

        # Check paywall prompt
        if any(kw in lower for kw in paywall_keywords):
            is_paywalled = True
            continue

        # Check disclaimers
        matched_disclaimer = False
        for pat in DISCLAIMER_PATTERNS:
            if pat.search(p_strip):
                matched_disclaimer = True
                break

        if matched_disclaimer:
            continue

        cleaned.append(p_strip)

    # If the remaining content is very short (< 300 chars) and a paywall prompt was present
    total_len = sum(len(p) for p in cleaned)
    if is_paywalled and total_len < 300:
        is_paywalled = True

    return cleaned, is_paywalled
