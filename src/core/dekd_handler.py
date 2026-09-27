"""Dedicated handler for Dek-D web novels (writer.dek-d.com and novel.dek-d.com).

Fetches complete novel metadata and all chapters directly from Dek-D's public REST API
(https://www.dek-d.com/api/rest/novel/{novel_id}/chapter/list?page={page})
and synthesizes an enriched HTML document with full metadata and embedded __DEKD_DATA__.
"""

import asyncio
import json
import logging
import re
from typing import Optional, Tuple, List, Dict, Any
from urllib.parse import urlparse, parse_qs
import httpx

logger = logging.getLogger("core.dekd_handler")

# Regex to match Dek-D novel URLs
# e.g.:
# https://writer.dek-d.com/Kumari/writer/view.php?id=2655851
# https://writer.dek-d.com/Kumari/writer/viewlongc.php?id=2655851&chapter=1
# https://novel.dek-d.com/novel/2655851
# https://novel.dek-d.com/novel/2655851/chapter/1
DEKD_DOMAIN_REGEX = re.compile(
    r"^https?://(?:writer|novel|www)\.dek-d\.com/",
    re.IGNORECASE,
)

# In-memory cache for project info and chapter lists: novel_id -> (project_dict, chapters_list)
_dekd_cache: Dict[str, Tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}


def is_dekd_url(url: str) -> bool:
    """Check if URL targets Dek-D writer or novel platform."""
    if not url:
        return False
    return bool(DEKD_DOMAIN_REGEX.search(url.strip()))


def parse_dekd_url(url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (novel_id, username_slug, chapter_no) from any Dek-D URL.
    
    Examples:
        https://writer.dek-d.com/Kumari/writer/view.php?id=2655851 -> ("2655851", "Kumari", None)
        https://writer.dek-d.com/Kumari/writer/viewlongc.php?id=2655851&chapter=1 -> ("2655851", "Kumari", "1")
        https://novel.dek-d.com/novel/2655851 -> ("2655851", None, None)
        https://novel.dek-d.com/novel/2655851/chapter/1 -> ("2655851", None, "1")
    """
    if not url:
        return None, None, None

    parsed = urlparse(url.strip())
    path = parsed.path
    qs = parse_qs(parsed.query)

    novel_id = None
    username = None
    chapter_no = None

    # 1. Query param format: ?id=2655851
    if "id" in qs and qs["id"]:
        novel_id = qs["id"][0].strip()
    if "chapter" in qs and qs["chapter"]:
        chapter_no = qs["chapter"][0].strip()

    # 2. Path formats
    # e.g. /Kumari/writer/view.php or /Kumari/writer/viewlongc.php
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[1].lower() in ("writer", "novel"):
        username = parts[0]
    
    # e.g. /novel/2655851 or /novel/2655851/chapter/1
    if not novel_id:
        m_novel = re.search(r"/novel/(\d+)(?:/chapter/(\d+))?", path, re.I)
        if m_novel:
            novel_id = m_novel.group(1)
            if m_novel.group(2):
                chapter_no = m_novel.group(2)

    return novel_id, username, chapter_no


async def fetch_dekd_toc_data(
    novel_id: str,
    username: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Fetch novel details and all chapter items via Dek-D's REST API.
    
    Fetches page 1, inspects pageInfo for total pages, and concurrently fetches
    all remaining pages in parallel.
    """
    if novel_id in _dekd_cache:
        return _dekd_cache[novel_id]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        "Referer": f"https://writer.dek-d.com/writer/view.php?id={novel_id}",
        "Accept": "application/json, text/plain, */*",
    }

    all_raw_items: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(headers=headers, timeout=12.0) as client:
        # 1. Fetch page 1
        page1_url = f"https://www.dek-d.com/api/rest/novel/{novel_id}/chapter/list?page=1"
        resp = await client.get(page1_url)
        resp.raise_for_status()
        data = resp.json()

        page_info = data.get("pageInfo", {})
        total_pages = page_info.get("numberOfPages", 1)
        all_raw_items.extend(data.get("list", []))

        # 2. Concurrently fetch remaining pages if multi-page
        if total_pages > 1:
            tasks = [
                client.get(f"https://www.dek-d.com/api/rest/novel/{novel_id}/chapter/list?page={p}")
                for p in range(2, total_pages + 1)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            for r in responses:
                if isinstance(r, httpx.Response) and r.status_code == 200:
                    try:
                        p_data = r.json()
                        all_raw_items.extend(p_data.get("list", []))
                    except Exception as e:
                        logger.warning(f"Error parsing Dek-D page response: {e}")

    # Extract metadata from item 0
    meta_item = all_raw_items[0] if all_raw_items else {}
    novel_title = meta_item.get("novelTitle") or f"Dek-D Novel {novel_id}"
    owners = meta_item.get("owners", [])
    author = owners[0].get("alias") or owners[0].get("username") if owners else ""
    user_slug = username or (owners[0].get("username") if owners else "") or "dekdee"
    
    category = meta_item.get("category", {})
    sub_category = category.get("subTitle") or category.get("mainTitle") or ""
    thumbnail = meta_item.get("thumbnail", {}).get("normal", "") if isinstance(meta_item.get("thumbnail"), dict) else ""

    # Filter out intro item (order 0) unless it's the only item
    real_chapters = [it for it in all_raw_items if it.get("order", 0) >= 1]
    if not real_chapters:
        real_chapters = all_raw_items

    # Sort strictly by order
    real_chapters.sort(key=lambda x: x.get("order", 0))

    meta = {
        "novel_id": novel_id,
        "novel_title": novel_title,
        "author": author,
        "user_slug": user_slug,
        "category": sub_category,
        "thumbnail": thumbnail,
        "total_chapters": len(real_chapters),
    }

    _dekd_cache[novel_id] = (meta, real_chapters)
    return meta, real_chapters


def build_dekd_toc_html(
    novel_id: str,
    meta: Dict[str, Any],
    chapters: List[Dict[str, Any]],
    base_url: str = "",
) -> str:
    """Construct an enriched HTML document containing novel metadata and chapter links."""
    novel_title = meta.get("novel_title") or f"Dek-D Novel {novel_id}"
    author = meta.get("author") or ""
    user_slug = meta.get("user_slug") or "dekdee"
    total_chapters = len(chapters)

    links_html = []
    embedded_chapters = []

    for idx, ch in enumerate(chapters, start=1):
        order = ch.get("order", idx)
        title = ch.get("title") or f"Chapter {order}"
        ch_url = f"https://writer.dek-d.com/{user_slug}/writer/viewlongc.php?id={novel_id}&chapter={order}"
        links_html.append(f'<a href="{ch_url}" class="chapter-link">{order}. {title}</a>')
        embedded_chapters.append({
            "index": idx,
            "order": order,
            "title": title,
            "url": ch_url,
            "id": ch.get("id"),
        })

    json_payload = {
        "platform": "dekd",
        "novel_id": novel_id,
        "novel_title": novel_title,
        "author": author,
        "total_chapters": total_chapters,
        "chapters": embedded_chapters,
    }

    html = f"""<!DOCTYPE HTML>
<html lang="th">
<head>
  <meta charset="utf-8">
  <title>นิยาย {novel_title} : Dek-D.com - Writer</title>
  <meta name="title" content="{novel_title}">
  <meta property="og:title" content="{novel_title}">
  <meta name="author" content="{author}">
</head>
<body>
  <div class="novel-detail">
    <h1 class="novel-name">{novel_title}</h1>
    <div class="author">
      <a class="st-color">{author}</a>
    </div>
    <div class="stat-text">
      <strong><span class="stat_number">{total_chapters}</span></strong>
      <p class="iconname">ตอน</p>
    </div>
  </div>
  <div class="chapter-list">
    {''.join(links_html)}
  </div>
  <script id="__DEKD_DATA__" type="application/json">
{json.dumps(json_payload, ensure_ascii=False, indent=2)}
  </script>
</body>
</html>"""
    return html


async def handle_dekd_url(url: str) -> Optional[str]:
    """High-level router: given any Dek-D URL, return clean synthetic HTML for TOC or pass through for Chapter."""
    novel_id, username, chapter_no = parse_dekd_url(url)
    if not novel_id:
        return None

    # If this is a chapter page URL, return None so ObscuraClient fetches the actual chapter HTML directly
    if chapter_no is not None:
        return None

    # Otherwise, it's a TOC landing page
    try:
        meta, chapters = await fetch_dekd_toc_data(novel_id, username)
        return build_dekd_toc_html(novel_id, meta, chapters, base_url=url)
    except Exception as e:
        logger.warning(f"Error in handle_dekd_url for {url}: {e}")
        return None
