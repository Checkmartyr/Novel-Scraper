"""Dedicated handler for WebNovel (webnovel.com).

Fetches complete novel catalog from WebNovel's catalog page
(https://www.webnovel.com/book/{book_id}/catalog)
and synthesizes an enriched HTML document with full metadata and embedded __WEBNOVEL_DATA__.
"""

import asyncio
import json
import logging
import re
from typing import Optional, Tuple, List, Dict, Any
from urllib.parse import urlparse, urljoin
import httpx
from bs4 import BeautifulSoup

from src.handlers.base import BasePlatformHandler

logger = logging.getLogger("handlers.webnovel")

# Regex to match WebNovel book and chapter URLs
WEBNOVEL_DOMAIN_REGEX = re.compile(
    r"^https?://(?:www\.|m\.)?webnovel\.com/book/",
    re.IGNORECASE,
)

# In-memory cache: book_id -> (meta_dict, chapters_list)
_webnovel_cache: Dict[str, Tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}

WEBNOVEL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def is_webnovel_url(url: str) -> bool:
    """Check if URL targets WebNovel book, catalog, or chapter page."""
    if not url:
        return False
    return bool(WEBNOVEL_DOMAIN_REGEX.search(url.strip()))


def parse_webnovel_url(url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (book_id, book_slug, chapter_id) from any WebNovel URL.

    Examples:
        https://www.webnovel.com/book/the-villainess-with-a-heroine-harem_21092118006417205
            -> ("21092118006417205", "the-villainess-with-a-heroine-harem", None)
        https://www.webnovel.com/book/21092118006417205
            -> ("21092118006417205", None, None)
        https://www.webnovel.com/book/21092118006417205/catalog
            -> ("21092118006417205", None, None)
        https://www.webnovel.com/book/the-villainess-with-a-heroine-harem_21092118006417205/the-'villainess-system'-to-the-rescue!_56618917044478356
            -> ("21092118006417205", "the-villainess-with-a-heroine-harem", "56618917044478356")
    """
    if not url:
        return None, None, None

    parsed = urlparse(url.strip())
    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0].lower() != "book" or len(parts) < 2:
        return None, None, None

    book_part = parts[1]
    m_book = re.search(r"^(?:(.+)_)?(\d{15,25})$", book_part)
    if not m_book:
        return None, None, None

    book_slug = m_book.group(1)
    book_id = m_book.group(2)

    chapter_id = None
    if len(parts) > 2:
        seg2 = parts[2].lower()
        if seg2 != "catalog":
            m_ch = re.search(r"(?:^|_)(\d{15,25})$", parts[2])
            if m_ch:
                chapter_id = m_ch.group(1)

    return book_id, book_slug, chapter_id


async def fetch_webnovel_toc_data(book_id: str, obscura_client: Optional[Any] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Fetch complete catalog HTML from WebNovel and parse metadata and chapter list."""
    if book_id in _webnovel_cache:
        return _webnovel_cache[book_id]

    catalog_url = f"https://www.webnovel.com/book/{book_id}/catalog"
    logger.info(f"Fetching WebNovel catalog from: {catalog_url}")

    html = ""
    # Try up to 3 times via fast HTTP with backoff
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(headers=WEBNOVEL_HEADERS, follow_redirects=True, timeout=15.0) as client:
                resp = await client.get(catalog_url)
                if resp.status_code == 200 and len(resp.text) > 1000:
                    html = resp.text
                    break
                elif resp.status_code in (403, 429, 503):
                    logger.warning(f"WebNovel catalog HTTP {resp.status_code} on attempt {attempt+1}. Retrying...")
                    await asyncio.sleep(0.8 * (attempt + 1))
        except Exception as e:
            logger.warning(f"WebNovel catalog fetch exception on attempt {attempt+1}: {e}")
            await asyncio.sleep(0.8)

    # Fallback to Obscura browser if fast HTTP failed
    if not html or len(html) < 1000:
        if obscura_client is not None:
            logger.info("Fast HTTP failed for WebNovel catalog. Falling back to browser...")
            try:
                browser_html = await obscura_client.fetch_html(catalog_url, stealth=True)
                if browser_html and len(browser_html) > 1000:
                    html = browser_html
            except Exception as e:
                logger.error(f"Browser fallback failed for WebNovel catalog: {e}")

    if not html:
        raise RuntimeError(f"Failed to fetch WebNovel catalog for book {book_id}")

    soup = BeautifulSoup(html, "lxml")

    # Extract novel title
    h1 = soup.find("h1")
    novel_title = h1.get_text(strip=True) if h1 else f"WebNovel {book_id}"

    # Extract author
    author = ""
    author_el = soup.find("a", class_="c_primary") or soup.find(class_="author")
    if author_el:
        author = author_el.get_text(strip=True)
    else:
        m_author = re.search(r"Author:\s*([^\n<]+)", html)
        if m_author:
            author = m_author.group(1).strip()

    # Extract description
    description = ""
    desc_meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if desc_meta and desc_meta.get("content"):
        description = desc_meta["content"].strip()

    # Extract chapters from li.g_col
    chapters: List[Dict[str, Any]] = []
    items = soup.find_all("li", class_="g_col")
    for idx, it in enumerate(items, start=1):
        a = it.find("a")
        if not a:
            continue
        href = a.get("href", "").strip()
        if not href:
            continue
        full_url = urljoin("https://www.webnovel.com", href)

        strong = a.find("strong")
        title = a.get("title") or (strong.get_text(strip=True) if strong else a.get_text(strip=True))
        title = re.sub(
            r"\d+\s+(?:years?|months?|weeks?|days?|hours?|mins?|minutes?|secs?|seconds?)\s+ago",
            "",
            title,
            flags=re.I,
        ).strip()

        chapters.append({
            "index": idx,
            "order": idx,
            "title": title or f"Chapter {idx}",
            "url": full_url,
        })

    if not chapters:
        ch_links = soup.find_all("a", href=re.compile(rf"/book/.*{book_id}/.*"))
        seen_urls = set()
        for idx, a in enumerate(ch_links, start=1):
            href = a.get("href", "").strip()
            if not href or href in seen_urls or "/catalog" in href:
                continue
            seen_urls.add(href)
            full_url = urljoin("https://www.webnovel.com", href)
            title = a.get("title") or a.get_text(strip=True) or f"Chapter {idx}"
            chapters.append({
                "index": idx,
                "order": idx,
                "title": title,
                "url": full_url,
            })

    meta = {
        "book_id": book_id,
        "novel_title": novel_title,
        "author": author,
        "description": description,
        "total_chapters": len(chapters),
    }

    _webnovel_cache[book_id] = (meta, chapters)
    return meta, chapters


def build_webnovel_toc_html(
    book_id: str,
    meta: Dict[str, Any],
    chapters: List[Dict[str, Any]],
    base_url: str = "",
) -> str:
    """Construct an enriched synthetic HTML document containing novel metadata and complete chapter links."""
    novel_title = meta.get("novel_title") or f"WebNovel {book_id}"
    author = meta.get("author") or ""
    description = meta.get("description") or ""
    total_chapters = len(chapters)

    links_html = []
    embedded_chapters = []

    for idx, ch in enumerate(chapters, start=1):
        order = ch.get("order", idx)
        title = ch.get("title") or f"Chapter {order}"
        ch_url = ch.get("url") or f"https://www.webnovel.com/book/{book_id}/{order}"
        links_html.append(f'<a href="{ch_url}" class="chapter-link">{order}. {title}</a>')
        embedded_chapters.append({
            "index": idx,
            "order": order,
            "title": title,
            "url": ch_url,
        })

    json_payload = {
        "platform": "webnovel",
        "novel_id": book_id,
        "novel_title": novel_title,
        "author": author,
        "description": description,
        "total_chapters": total_chapters,
        "chapters": embedded_chapters,
    }

    html = f"""<!DOCTYPE HTML>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{novel_title} - WebNovel</title>
  <meta name="title" content="{novel_title}">
  <meta property="og:title" content="{novel_title}">
  <meta name="author" content="{author}">
  <meta name="description" content="{description}">
</head>
<body>
  <div class="novel-detail">
    <h1 class="novel-name">{novel_title}</h1>
    <div class="author">
      <a class="st-color">{author}</a>
    </div>
    <div class="stat-text">
      <strong><span class="stat_number">{total_chapters}</span></strong>
      <p class="iconname">Chapters</p>
    </div>
  </div>
  <div class="chapter-list">
    {''.join(links_html)}
  </div>
  <script id="__WEBNOVEL_DATA__" type="application/json">
{json.dumps(json_payload, ensure_ascii=False, indent=2)}
  </script>
</body>
</html>"""
    return html


async def handle_webnovel_url(url: str, obscura_client: Optional[Any] = None) -> Optional[str]:
    """High-level router: for WebNovel TOC / Book URLs, return clean synthetic HTML containing all chapters.

    For chapter pages, returns None so that ObscuraClient fetches the chapter text directly.
    """
    book_id, book_slug, chapter_id = parse_webnovel_url(url)
    if not book_id:
        return None

    # If this is an individual chapter URL, pass through to browser/HTTP fetch
    if chapter_id is not None:
        return None

    # Otherwise, it's a novel TOC / book landing page
    try:
        meta, chapters = await fetch_webnovel_toc_data(book_id, obscura_client=obscura_client)
        return build_webnovel_toc_html(book_id, meta, chapters, base_url=url)
    except Exception as e:
        logger.warning(f"Error in handle_webnovel_url for {url}: {e}")
        return None


class WebNovelHandler(BasePlatformHandler):
    """Platform handler for WebNovel."""

    def matches(self, url: str) -> bool:
        return is_webnovel_url(url)

    async def handle(self, url: str, obscura_client: Optional[Any] = None) -> Optional[str]:
        return await handle_webnovel_url(url, obscura_client=obscura_client)
