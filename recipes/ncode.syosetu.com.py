#!/usr/bin/env python3
"""
Autonomous Scraper Recipe for: ncode.syosetu.com
Generated: 2026-09-17 22:06:54 UTC
Sample TOC URL: https://ncode.syosetu.com/n3881dn/
Sample Chapter URL: https://ncode.syosetu.com/n3881dn/1/
Quality Score: 1.0/1.0
"""

import sys
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup

# ==============================================================================
# 1. TABLE OF CONTENTS EXTRACTOR
# ==============================================================================

def extract_toc(html: str, base_url: str) -> list[dict]:
    """Extract table of contents chapters from HTML for ncode.syosetu.com."""
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
    remove_selectors = ['div.p-novel__subtitle', 'div.p-reaction', 'div.c-pager-novel', 'div.l-foot-contents', 'div.p-novel__banner', 'div.js-novel-pr', 'div.p-novel__control', 'div.c-menu-bar']
    for sel in remove_selectors:
        for tag in soup.select(sel):
            tag.decompose()
            
    # Extract Title
    title = ""
    title_el = soup.select_one("h1.p-novel__title.p-novel__title--rensai")
    if title_el:
        title = title_el.get_text(strip=True)
        
    # Extract Content
    content_elements = soup.select("div.p-novel__body, #novel_honbun")
    top_elements = [
        el for el in content_elements
        if not any(other is not el and other in el.parents for other in content_elements)
    ]
    if not top_elements:
        top_elements = [
            el for el in soup.select(".p-novel__text")
            if not any(other is not el and other in el.parents for other in soup.select(".p-novel__text"))
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

    parser = argparse.ArgumentParser(description="Standalone scraper for ncode.syosetu.com")
    parser.add_argument("--toc", help="URL of the novel Table of Contents")
    parser.add_argument("--chapter", help="URL of a chapter page")
    args = parser.parse_args()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    }

    if args.toc:
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
