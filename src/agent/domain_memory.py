"""Domain Memory and Recipe System for autonomous novel scraping.

Persists learned TOC extraction recipes and Chapter extraction recipes per domain
as both structured JSON metadata and standalone, executable Python scripts.
On subsequent scrapes from the same domain, allows instant 0-token fast-path execution.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from src.config import RECIPES_DIR
from src.agent.analyzer import DOMStructurePlan
from src.agent.classifier import ChapterLink
from src.agent.code_generator import ChapterCodeGenerator, ExtractedChapter, ParserVerificationResult

logger = logging.getLogger("agent.domain_memory")


class TocRecipeConfig(BaseModel):
    """Configuration for TOC extraction."""
    strategy: str = "dom_heuristic"
    link_selector: Optional[str] = None
    container_selector: Optional[str] = None
    title_cleanup_regex: Optional[str] = None
    pagination_pattern: Optional[str] = None
    code_script: Optional[str] = None


class ChapterRecipeConfig(BaseModel):
    """Configuration for Chapter content extraction."""
    title_selector: str
    content_selector: str
    remove_selectors: List[str] = Field(default_factory=list)
    clean_paragraphs: bool = True
    quality_score: float = 1.0
    code_script: Optional[str] = None

    def to_plan(self) -> DOMStructurePlan:
        return DOMStructurePlan(
            title_selector=self.title_selector,
            content_selector=self.content_selector,
            remove_selectors=self.remove_selectors,
            clean_paragraphs=self.clean_paragraphs,
        )


class DomainRecipe(BaseModel):
    """Persisted scraping recipe for a specific website domain."""
    domain: str
    created_at: str
    updated_at: str
    sample_toc_url: str = ""
    sample_chapter_url: str = ""
    toc_config: TocRecipeConfig
    chapter_config: ChapterRecipeConfig
    times_used: int = 0
    last_used_at: Optional[str] = None


class DomainMemoryManager:
    """Manages domain-level scraping recipes and standalone scripts."""

    def __init__(self, recipes_dir: Optional[Path] = None):
        self.recipes_dir = recipes_dir or RECIPES_DIR
        self.recipes_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, DomainRecipe] = {}
        self._load_all_from_disk()

    @staticmethod
    def normalize_domain(url_or_domain: str) -> str:
        """Extract and normalize clean base domain name from a URL or raw domain string."""
        if not url_or_domain:
            return ""
        
        target = url_or_domain.strip()
        if not (target.startswith("http://") or target.startswith("https://")):
            target = "https://" + target
            
        parsed = urlparse(target)
        netloc = parsed.netloc.lower()
        # Remove port if present
        host = netloc.split(":")[0]
        # Remove leading www.
        if host.startswith("www."):
            host = host[4:]
            
        # Clean filesystem-unsafe characters
        return re.sub(r"[^\w\.-]", "_", host)

    def _get_json_path(self, domain: str) -> Path:
        return self.recipes_dir / f"{domain}.json"

    def _get_script_path(self, domain: str) -> Path:
        return self.recipes_dir / f"{domain}.py"

    def _load_all_from_disk(self) -> None:
        """Load all existing JSON recipes from recipes_dir into memory."""
        try:
            for json_file in self.recipes_dir.glob("*.json"):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        recipe = DomainRecipe(**data)
                        self._cache[recipe.domain] = recipe
                except Exception as e:
                    logger.warning(f"Failed to load recipe {json_file.name}: {e}")
        except Exception as e:
            logger.warning(f"Failed to read recipes directory: {e}")

    def get_recipe(self, url_or_domain: str) -> Optional[DomainRecipe]:
        """Lookup saved recipe for a URL or domain."""
        domain = self.normalize_domain(url_or_domain)
        if not domain:
            return None

        # Check memory cache first
        if domain in self._cache:
            return self._cache[domain]

        # Check disk
        json_path = self._get_json_path(domain)
        if json_path.exists():
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    recipe = DomainRecipe(**data)
                    self._cache[domain] = recipe
                    return recipe
            except Exception as e:
                logger.warning(f"Error reading recipe file {json_path}: {e}")
        return None

    def record_usage(self, domain: str) -> None:
        """Increment usage counter and update last_used_at timestamp."""
        norm_domain = self.normalize_domain(domain)
        recipe = self.get_recipe(norm_domain)
        if recipe:
            recipe.times_used += 1
            recipe.last_used_at = datetime.now(timezone.utc).isoformat()
            self._save_recipe_files(recipe)

    def generate_toc_script(self, toc_config: TocRecipeConfig, domain: str) -> str:
        """Generate standalone Python code for extract_toc(html, base_url)."""
        link_sel = repr(toc_config.link_selector or "a[href]")
        container_sel = repr(toc_config.container_selector or "")
        strategy = repr(toc_config.strategy)

        code = f'''def extract_toc(html: str, base_url: str) -> list[dict]:
    """Extract table of contents chapters from HTML for {domain}."""
    soup = BeautifulSoup(html, "lxml")
    chapters = []
    seen_urls = set()

    # Action button regex filter
    action_btn_re = re.compile(
        r"^(?:read\\s*(?:first|latest|now)|first\\s*chapter|last\\s*chapter|continue\\s*reading|bookmark|share|follow|1話目から読む|最初から読む)\\b",
        re.I,
    )

    container_sel = {container_sel}
    root = soup.select_one(container_sel) if container_sel else soup
    if not root:
        root = soup

    link_selector = {link_sel}
    candidate_links = root.select(link_selector) if link_selector != "a[href]" else root.find_all("a", href=True)

    extracted_items = []
    for a in candidate_links:
        href = (a.get("href") or "").strip()
        if not href or href.startswith("javascript:") or href.startswith("#"):
            continue
        text = a.get_text(" ", strip=True)
        text_clean = re.sub(r"[\\s\\xa0]+", " ", text).strip()
        if not text_clean or len(text_clean) > 200:
            continue
        if action_btn_re.search(text_clean):
            continue

        full_url = urljoin(base_url, href)
        
        # Check chapter pattern
        ch_num = 999999
        m = re.search(r"(?:chapter|ch[\\.-]?|episode|ep[\\.-]?|第)\\s*(\\d+)", text_clean, re.I)
        if not m:
            m = re.search(r"/chapter[\\-_]?(\\d+)", full_url, re.I)
        if m:
            try:
                ch_num = int(m.group(1))
            except ValueError:
                pass

        extracted_items.append({{"title": text_clean, "url": full_url, "ch_num": ch_num}})

    # Deduplicate preferring richer titles
    url_to_item = {{}}
    for item in extracted_items:
        u = item["url"]
        if u not in url_to_item or len(item["title"]) > len(url_to_item[u]["title"]):
            url_to_item[u] = item

    final_items = list(url_to_item.values())
    if any(x["ch_num"] != 999999 for x in final_items):
        final_items.sort(key=lambda x: x["ch_num"])

    for idx, it in enumerate(final_items, start=1):
        chapters.append({{
            "index": idx,
            "title": it["title"],
            "url": it["url"]
        }})

    return chapters
'''
        return code

    def generate_full_standalone_script(self, recipe: DomainRecipe) -> str:
        """Generate a complete standalone Python script with both TOC and Chapter extractors."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        toc_code = recipe.toc_config.code_script or self.generate_toc_script(recipe.toc_config, recipe.domain)
        chapter_code = recipe.chapter_config.code_script or ""

        script = f'''#!/usr/bin/env python3
"""
Autonomous Scraper Recipe for: {recipe.domain}
Generated: {now}
Sample TOC URL: {recipe.sample_toc_url}
Sample Chapter URL: {recipe.sample_chapter_url}
Quality Score: {recipe.chapter_config.quality_score}/1.0
"""

import sys
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup

# ==============================================================================
# 1. TABLE OF CONTENTS EXTRACTOR
# ==============================================================================

{toc_code}

# ==============================================================================
# 2. CHAPTER CONTENT EXTRACTOR
# ==============================================================================

{chapter_code}

# ==============================================================================
# CLI TEST RUNNER
# ==============================================================================

if __name__ == "__main__":
    import argparse
    import httpx

    parser = argparse.ArgumentParser(description="Standalone scraper for {recipe.domain}")
    parser.add_argument("--toc", help="URL of the novel Table of Contents")
    parser.add_argument("--chapter", help="URL of a chapter page")
    args = parser.parse_args()

    headers = {{
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    }}

    if args.toc:
        print(f"Fetching TOC: {{args.toc}}...")
        resp = httpx.get(args.toc, headers=headers, follow_redirects=True)
        chapters = extract_toc(resp.text, args.toc)
        print(f"Extracted {{len(chapters)}} chapters:")
        for ch in chapters[:10]:
            print(f"  {{ch['index']:03d}}. {{ch['title']}} -> {{ch['url']}}")
        if len(chapters) > 10:
            print(f"  ... and {{len(chapters) - 10}} more.")

    elif args.chapter:
        print(f"Fetching Chapter: {{args.chapter}}...")
        resp = httpx.get(args.chapter, headers=headers, follow_redirects=True)
        res = extract_chapter(resp.text)
        print(f"Title: {{res.get('title')}}")
        print(f"Words: {{res.get('word_count')}} | Chars: {{res.get('char_count')}}")
        content = res.get("content", "")
        print(f"Preview:\\n{{content[:300]}}...")
    else:
        parser.print_help()
'''
        return script

    def save_recipe(
        self,
        domain_or_url: str,
        sample_toc_url: str,
        sample_chapter_url: str,
        toc_strategy: str,
        chapter_plan: DOMStructurePlan,
        toc_link_selector: Optional[str] = None,
        toc_container_selector: Optional[str] = None,
        quality_score: float = 1.0,
    ) -> DomainRecipe:
        """Save or update a domain recipe to both JSON metadata and standalone Python script."""
        domain = self.normalize_domain(domain_or_url)
        now_str = datetime.now(timezone.utc).isoformat()

        # Check for existing recipe to preserve creation date and stats
        existing = self.get_recipe(domain)
        created_at = existing.created_at if existing else now_str
        times_used = existing.times_used if existing else 0

        # Generate chapter script code
        generator = ChapterCodeGenerator(chapter_plan)
        chapter_script = generator.generate_code_string()

        toc_config = TocRecipeConfig(
            strategy=toc_strategy,
            link_selector=toc_link_selector,
            container_selector=toc_container_selector,
        )
        toc_config.code_script = self.generate_toc_script(toc_config, domain)

        chapter_config = ChapterRecipeConfig(
            title_selector=chapter_plan.title_selector,
            content_selector=chapter_plan.content_selector,
            remove_selectors=chapter_plan.remove_selectors,
            clean_paragraphs=chapter_plan.clean_paragraphs,
            quality_score=quality_score,
            code_script=chapter_script,
        )

        recipe = DomainRecipe(
            domain=domain,
            created_at=created_at,
            updated_at=now_str,
            sample_toc_url=sample_toc_url or (existing.sample_toc_url if existing else ""),
            sample_chapter_url=sample_chapter_url or (existing.sample_chapter_url if existing else ""),
            toc_config=toc_config,
            chapter_config=chapter_config,
            times_used=times_used,
            last_used_at=existing.last_used_at if existing else None,
        )

        self._save_recipe_files(recipe)
        self._cache[domain] = recipe
        logger.info(f"Successfully saved recipe for domain '{domain}' in {self.recipes_dir}")
        return recipe

    def _save_recipe_files(self, recipe: DomainRecipe) -> None:
        """Write recipe to both JSON and standalone .py script."""
        # 1. Write JSON
        json_path = self._get_json_path(recipe.domain)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(recipe.model_dump(), f, indent=2, ensure_ascii=False)

        # 2. Write Python script
        script_path = self._get_script_path(recipe.domain)
        standalone_code = self.generate_full_standalone_script(recipe)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(standalone_code)

    def test_toc_recipe(self, recipe: DomainRecipe, html: str, base_url: str) -> Tuple[bool, List[ChapterLink]]:
        """Test the saved TOC recipe on given HTML."""
        try:
            from src.agent.toc.tools import DomLinkExtractor
            parsed_base = urlparse(base_url)
            base_path = parsed_base.path.rstrip("/")

            # 1. If explicit link_selector is specified and matches elements
            link_sel = recipe.toc_config.link_selector
            if link_sel and link_sel != "a[href]":
                soup = BeautifulSoup(html, "lxml")
                root = soup.select_one(recipe.toc_config.container_selector) if recipe.toc_config.container_selector else soup
                if not root:
                    root = soup
                elements = root.select(link_sel)
                if elements:
                    extracted: List[ChapterLink] = []
                    seen = set()
                    for idx, a in enumerate(elements, start=1):
                        href = (a.get("href") or "").strip()
                        if not href or href.startswith("javascript:") or href.startswith("#"):
                            continue
                        full_url = urljoin(base_url, href)
                        if full_url in seen:
                            continue
                        seen.add(full_url)
                        title = a.get_text(" ", strip=True)
                        title = re.sub(r"[\s\xa0]+", " ", title).strip()
                        extracted.append(ChapterLink(index=len(extracted) + 1, title=title or f"Chapter {len(extracted) + 1}", url=full_url))
                    if extracted:
                        return True, extracted

            # 2. Use DomLinkExtractor with platform heuristics
            chapters = DomLinkExtractor.extract(html, base_url)
            if chapters and base_path and len(base_path) > 3:
                same_novel = [c for c in chapters if base_path in c.url]
                if len(same_novel) >= 1:
                    # Re-index
                    for idx, c in enumerate(same_novel, start=1):
                        c.index = idx
                    return True, same_novel

            return len(chapters) >= 1, chapters
        except Exception as e:
            logger.warning(f"Error executing TOC recipe for {recipe.domain}: {e}")
            return False, []

    def test_chapter_recipe(self, recipe: DomainRecipe, html: str) -> ParserVerificationResult:
        """Test the saved chapter recipe on given HTML."""
        plan = recipe.chapter_config.to_plan()
        generator = ChapterCodeGenerator(plan)
        return generator.test_and_verify(html)

    def list_recipes(self) -> List[Dict[str, Any]]:
        """Return summary of all saved domain recipes."""
        summaries = []
        for domain, recipe in self._cache.items():
            summaries.append({
                "domain": domain,
                "strategy": recipe.toc_config.strategy,
                "title_selector": recipe.chapter_config.title_selector,
                "content_selector": recipe.chapter_config.content_selector,
                "quality_score": recipe.chapter_config.quality_score,
                "times_used": recipe.times_used,
                "updated_at": recipe.updated_at,
            })
        return summaries

    def delete_recipe(self, domain_or_url: str) -> bool:
        """Remove a saved recipe from memory and disk."""
        domain = self.normalize_domain(domain_or_url)
        json_path = self._get_json_path(domain)
        script_path = self._get_script_path(domain)

        removed = False
        if json_path.exists():
            json_path.unlink()
            removed = True
        if script_path.exists():
            script_path.unlink()
            removed = True

        if domain in self._cache:
            del self._cache[domain]
            removed = True

        return removed


# Global singleton manager instance
domain_memory = DomainMemoryManager()
