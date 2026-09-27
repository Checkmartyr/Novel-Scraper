#!/usr/bin/env python3
"""
Autonomous Scraper Recipe for: freewebnovel.com
Generated: 2026-09-27 11:54:19 UTC
Sample TOC URL: https://freewebnovel.com/novel/the-harem-system-rewards-me-for-everything
Sample Chapter URL: 
Quality Score: 0.5/1.0
"""

import sys
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup

# ==============================================================================
# 1. TABLE OF CONTENTS EXTRACTOR
# ==============================================================================

def extract_toc(html: str, base_url: str) -> list[dict]:
    """Extract table of contents chapters from HTML for freewebnovel.com."""
    soup = BeautifulSoup(html, "lxml")
    chapters = []
    seen_urls = set()

    # Action button regex filter
    action_btn_re = re.compile(
        r"^(?:read\s*(?:first|latest|now)|first\s*chapter|last\s*chapter|continue\s*reading|bookmark|share|follow|1話目から読む|最初から読む)\b",
        re.I,
    )

    container_sel = ''
    root = soup.select_one(container_sel) if container_sel else soup
    if not root:
        root = soup

    link_selector = 'a[href]'
    candidate_links = root.select(link_selector) if link_selector != "a[href]" else root.find_all("a", href=True)

    extracted_items = []
    for a in candidate_links:
        href = (a.get("href") or "").strip()
        if not href or href.startswith("javascript:") or href.startswith("#"):
            continue
        text = a.get_text(" ", strip=True)
        text_clean = re.sub(r"[\s\xa0]+", " ", text).strip()
        if not text_clean or len(text_clean) > 200:
            continue
        if action_btn_re.search(text_clean):
            continue

        full_url = urljoin(base_url, href)
        
        # Check chapter pattern
        ch_num = 999999
        m = re.search(r"(?:chapter|ch[\.-]?|episode|ep[\.-]?|第)\s*(\d+)", text_clean, re.I)
        if not m:
            m = re.search(r"/chapter[\-_]?(\d+)", full_url, re.I)
        if m:
            try:
                ch_num = int(m.group(1))
            except ValueError:
                pass

        extracted_items.append({"title": text_clean, "url": full_url, "ch_num": ch_num})

    # Deduplicate preferring richer titles
    url_to_item = {}
    for item in extracted_items:
        u = item["url"]
        if u not in url_to_item or len(item["title"]) > len(url_to_item[u]["title"]):
            url_to_item[u] = item

    final_items = list(url_to_item.values())
    if any(x["ch_num"] != 999999 for x in final_items):
        final_items.sort(key=lambda x: x["ch_num"])

    for idx, it in enumerate(final_items, start=1):
        chapters.append({
            "index": idx,
            "title": it["title"],
            "url": it["url"]
        })

    return chapters


# ==============================================================================
# 2. CHAPTER CONTENT EXTRACTOR
# ==============================================================================

from bs4 import BeautifulSoup
import re

def extract_chapter(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    
    # Remove clutter elements
    remove_selectors = []
    for sel in remove_selectors:
        for tag in soup.select(sel):
            tag.decompose()
            
    # Extract Title
    title = ""
    title_el = soup.select_one("h1")
    if title_el:
        title = title_el.get_text(strip=True)
        
    # Extract Content
    content_elements = soup.select("body")
    top_elements = [
        el for el in content_elements
        if not any(other is not el and other in el.parents for other in content_elements)
    ]
    if not top_elements:
        return {"title": title, "content": "", "word_count": 0, "error": "Content container not found"}
        
    # Process paragraphs into Markdown
    paragraphs = []
    for container in top_elements:
        p_tags = container.find_all("p")
        if p_tags:
            for p in p_tags:
                text = p.get_text(strip=True)
                if text:
                    paragraphs.append(text)
        else:
            text = container.get_text("\n", strip=True)
            for line in text.split("\n"):
                line = line.strip()
                if line:
                    paragraphs.append(line)
                
    content_md = "\n\n".join(paragraphs)
    words = len(re.findall(r"\w+", content_md))
    
    return {
        "title": title,
        "content": content_md,
        "word_count": words,
        "char_count": len(content_md),
        "error": None
    }


# ==============================================================================
# CLI TEST RUNNER
# ==============================================================================

if __name__ == "__main__":
    import argparse
    import httpx

    parser = argparse.ArgumentParser(description="Standalone scraper for freewebnovel.com")
    parser.add_argument("--toc", help="URL of the novel Table of Contents")
    parser.add_argument("--chapter", help="URL of a chapter page")
    parser.add_argument("--batch", action="store_true", help="Scrape all chapters discovered via TOC")
    parser.add_argument("--output-dir", default="novels", help="Target directory for batch download")
    args = parser.parse_args()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    }

    if args.batch:
        if not args.toc:
            print("Error: --batch requires --toc <URL>")
            sys.exit(1)
        from pathlib import Path
        print(f"Fetching TOC for batch: {args.toc}...")
        resp = httpx.get(args.toc, headers=headers, follow_redirects=True)
        chapters = extract_toc(resp.text, args.toc)
        print(f"Found {len(chapters)} chapters. Starting batch scrape into '{args.output_dir}'...")
        out_dir = Path(args.output_dir) / "freewebnovel.com"
        out_dir.mkdir(parents=True, exist_ok=True)
        for ch in chapters:
            c_url = ch["url"]
            c_idx = ch["index"]
            c_title = ch["title"]
            print(f"[{c_idx}/{len(chapters)}] Fetching: {c_title} ({c_url})...")
            try:
                c_resp = httpx.get(c_url, headers=headers, follow_redirects=True)
                c_data = extract_chapter(c_resp.text)
                title = c_data.get("title") or c_title
                content = c_data.get("content") or ""
                clean_title = re.sub(r'[\/*?:"<>|]', "", title)[:60].strip()
                filename = f"{c_idx:04d} - {clean_title}.md"
                fpath = out_dir / filename
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(f"# {title}\n\nSource: {c_url}\n\n{content}\n")
                print(f"  -> Saved {len(content)} chars to {fpath.name}")
            except Exception as e:
                print(f"  -> Error scraping chapter {c_idx}: {e}")
        print("Batch scrape completed successfully!")

    elif args.toc:
        print(f"Fetching TOC: {args.toc}...")
        resp = httpx.get(args.toc, headers=headers, follow_redirects=True)
        chapters = extract_toc(resp.text, args.toc)
        print(f"Extracted {len(chapters)} chapters:")
        for ch in chapters[:10]:
            print(f"  {ch['index']:03d}. {ch['title']} -> {ch['url']}")
        if len(chapters) > 10:
            print(f"  ... and {len(chapters) - 10} more.")

    elif args.chapter:
        print(f"Fetching Chapter: {args.chapter}...")
        resp = httpx.get(args.chapter, headers=headers, follow_redirects=True)
        res = extract_chapter(resp.text)
        print(f"Title: {res.get('title')}")
        print(f"Words: {res.get('word_count')} | Chars: {res.get('char_count')}")
        content = res.get("content", "")
        print(f"Preview:\n{content[:300]}...")
    else:
        parser.print_help()
