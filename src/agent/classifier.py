import json
import logging
import re
from urllib.parse import urljoin, urlparse
from typing import List, Optional, Any
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from src.agent.llm import LLMClient
from src.utils.title_cleaner import clean_chapter_title, detect_and_fix_reverse_order
from src.utils.novel_url import belongs_to_recipe_novel, recipe_page_kind, toc_url_from_recipe

logger = logging.getLogger("agent.classifier")


class ChapterLink(BaseModel):
    index: int
    title: str
    url: str

class ClassificationResult(BaseModel):
    page_type: str = Field(description="'TOC' for Table of Contents or 'CHAPTER' for single chapter page")
    novel_title: str = Field(description="Name of the novel")
    author: Optional[str] = Field(default=None, description="Author name if found")
    description: Optional[str] = Field(default=None, description="Novel synopsis if found on TOC")
    chapter_links: List[ChapterLink] = Field(default_factory=list, description="List of chapter links if TOC")
    toc_url: Optional[str] = Field(default=None, description="URL pointing to TOC if this is a chapter page")
    next_chapter_url: Optional[str] = Field(default=None, description="URL of next chapter if this is a chapter page")
    chapter_title: Optional[str] = Field(default=None, description="Title of current chapter if this is a chapter page")
    chapter_number: Optional[int] = Field(default=None, description="Chapter number if this is a chapter page")
    toc_state: Optional[dict] = Field(default=None, description="Detailed state dictionary from TocAgent if TOC")

class PageClassifier:
    """Classifies novel web pages into TOC landing page vs Single Chapter."""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        obscura_client: Optional[Any] = None,
        toc_agent: Optional[Any] = None,
    ):
        self.llm = llm_client or LLMClient()
        self.obscura = obscura_client
        self._toc_agent_instance = toc_agent

    @property
    def toc_agent(self):
        if self._toc_agent_instance is None:
            from src.agent.toc.agent import TocAgent
            self._toc_agent_instance = TocAgent(obscura_client=self.obscura)
        return self._toc_agent_instance

    def _clean_novel_title(self, raw_title: str) -> str:
        """Strip promotional bracket prefixes and site tags from novel title."""
        if not raw_title:
            return "Unknown Novel"
        cleaned = re.sub(r"^【[^】]+】\s*", "", raw_title)
        cleaned = re.sub(r"^\[[^\]]+\]\s*", "", cleaned)
        cleaned = re.split(r"\s*[-|–]\s*(?:Table of Contents|TOC|Index|目次|全話一覧|カクヨム|小説家になろう|Novel Updates|Syosetu)", cleaned, flags=re.I)[0]
        return cleaned.strip() or raw_title.strip()

    def _extract_page_summary(self, html: str, base_url: str) -> dict:
        """Condense HTML into metadata and candidate links to save LLM tokens."""
        soup = BeautifulSoup(html, "lxml")

        # Remove non-content tags
        for tag in soup(["script", "style", "svg", "noscript", "iframe"]):
            tag.decompose()

        page_title = soup.title.get_text(strip=True) if soup.title else ""
        meta_desc = ""
        desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        if desc_tag and desc_tag.get("content"):
            meta_desc = desc_tag["content"].strip()

        h1_texts = [h.get_text(strip=True) for h in soup.find_all("h1")][:3]

        # Collect candidate navigation links
        nav_links = []
        parsed_base = urlparse(base_url)
        base_path = parsed_base.path.rstrip("/")

        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = a["href"].strip()
            if not text or not href or href.startswith("javascript:") or href.startswith("#"):
                continue
            full_url = urljoin(base_url, href)
            nav_links.append({"text": text, "url": full_url})

        # Smart link prioritization: prioritize chapter/episode links
        def link_priority(link: dict) -> int:
            u = link["url"].lower()
            t = link["text"]
            if any(k in u for k in ["/episodes/", "/chapter", "/read/", "-chapter-", "_chapter_", "/c/"]):
                return 0
            if re.search(r"webnovel\.com/book/[^/]+/[^/]+", u) and not u.endswith("/catalog"):
                return 0
            if re.search(r"第\s*\d+\s*[話章节回]", t) or re.search(r"\b(?:chapter|ch|ep|episode)\s*\d+\b", t, re.I):
                return 0
            if base_path and len(base_path) > 1 and base_path in link["url"]:
                return 1
            if parsed_base.netloc and parsed_base.netloc in link["url"]:
                return 2
            return 3

        prioritized_links = sorted(nav_links, key=link_priority)

        return {
            "title": page_title,
            "description": meta_desc,
            "h1s": h1_texts,
            "total_links": len(nav_links),
            "sample_links": prioritized_links[:150],  # sample candidate links prioritizing chapters
        }

    def _extract_apollo_chapters(self, url: str, html: str) -> List[ChapterLink]:
        """Extract complete chapter list from Next.js Apollo state if present (e.g. Kakuyomu)."""
        soup = BeautifulSoup(html, "lxml")
        next_data_script = soup.find("script", id="__NEXT_DATA__")
        if not next_data_script or not next_data_script.string:
            return []
        try:
            data = json.loads(next_data_script.string)
            apollo = data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
            toc_chapter = apollo.get("TableOfContentsChapter:", {})
            episode_unions = toc_chapter.get("episodeUnions", [])
            work_match = re.search(r"/works/(\d+)", url)
            work_id = work_match.group(1) if work_match else ""
            work_obj = apollo.get(f"Work:{work_id}", {}) if work_id else {}
            if not episode_unions and "tableOfContentsV2" in work_obj:
                for toc_ref in work_obj["tableOfContentsV2"]:
                    ref_key = toc_ref.get("__ref")
                    if ref_key and ref_key in apollo:
                        episode_unions.extend(apollo[ref_key].get("episodeUnions", []))
            if episode_unions:
                parsed_url = urlparse(url)
                base_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
                apollo_chapters = []
                for ep_ref in episode_unions:
                    ref_key = ep_ref.get("__ref")
                    if not ref_key or ref_key not in apollo:
                        continue
                    ep_data = apollo[ref_key]
                    ep_id = ep_data.get("id")
                    ep_title = ep_data.get("title", "")
                    if ep_id and work_id:
                        ep_url = f"{base_origin}/works/{work_id}/episodes/{ep_id}"
                        apollo_chapters.append(ChapterLink(
                            index=len(apollo_chapters) + 1,
                            title=ep_title or f"Episode {len(apollo_chapters) + 1}",
                            url=ep_url
                        ))
                return apollo_chapters
        except Exception as e:
            logger.debug(f"Failed extracting Apollo TOC: {e}")
        return []

    async def classify(self, url: str, html: str) -> ClassificationResult:
        """Classify page using Gemini with fallback to deterministic heuristic parser."""
        recipe = None
        page_kind = None
        derived_toc_url = None

        # 1. Check Domain Memory fast-path bypass
        try:
            from src.agent.domain_memory import domain_memory
            from src.agent.toc.tools import ClaimInspector, TocAuditor
            recipe = domain_memory.get_recipe(url)
            if recipe:
                page_kind = recipe_page_kind(url, recipe.sample_toc_url, recipe.sample_chapter_url)
                derived_toc_url = toc_url_from_recipe(url, recipe.sample_toc_url, recipe.sample_chapter_url)
                claimed, title, author, desc = ClaimInspector.inspect(html, url)
                # Check if this page is a TOC by testing saved TOC recipe
                ok, chapters = domain_memory.test_toc_recipe(recipe, html, url)
                if page_kind != "CHAPTER" and ok and len(chapters) >= 1:
                    is_complete, conf, unexp, pag, _ = TocAuditor.audit(chapters, claimed, html)
                    if not pag and not unexp and (conf >= 0.8 or claimed is None):
                        logger.info(f"[DomainMemory] Bypass: Detected domain '{recipe.domain}' TOC with {len(chapters)} chapters.")
                        toc_state = {
                            "url": url,
                            "html": html,
                            "novel_title": title or recipe.domain,
                            "author": author,
                            "description": desc,
                            "claimed_chapter_count": claimed,
                            "extracted_chapters": chapters,
                            "extraction_strategy": "remembered_recipe",
                            "confidence_score": 1.0,
                            "is_complete": True,
                            "iteration": 0,
                            "issues": [],
                            "logs": [f"Bypassed LLM classification via remembered recipe for '{recipe.domain}'."],
                        }
                        domain_memory.record_usage(recipe.domain)
                        return ClassificationResult(
                            page_type="TOC",
                            novel_title=self._clean_novel_title(title or recipe.domain),
                            author=author,
                            description=desc,
                            chapter_links=chapters,
                            toc_state=toc_state,
                        )

                # Check if this page is a single chapter page
                soup = BeautifulSoup(html, "lxml")
                has_content = bool(soup.select_one(recipe.chapter_config.content_selector))
                if (has_content and page_kind != "TOC" and
                        (page_kind == "CHAPTER" or recipe.chapter_config.content_selector != "body")):
                    ch_verif = domain_memory.test_chapter_recipe(recipe, html)
                    if ch_verif.success:
                        logger.info(f"[DomainMemory] Bypass: Detected domain '{recipe.domain}' Chapter page ('{ch_verif.chapter_title}').")
                        return ClassificationResult(
                            page_type="CHAPTER",
                            novel_title=self._clean_novel_title(title or recipe.domain),
                            author=author,
                            description=desc,
                            chapter_title=ch_verif.chapter_title,
                            toc_url=derived_toc_url,
                        )
        except Exception as e:
            logger.debug(f"Domain memory classifier bypass check failed: {e}")

        summary = self._extract_page_summary(html, url)
        apollo_chapters = self._extract_apollo_chapters(url, html)

        if self.llm.is_available:
            try:
                prompt = f"""
Current Page URL: {url}
Page <title>: {summary['title']}
Meta Description: {summary['description']}
H1 Headings: {summary['h1s']}
Sample links found ({min(len(summary['sample_links']), 150)} of {summary['total_links']}):
{summary['sample_links']}

Analyze the page:
1. Determine if this page is a 'TOC' (Table of Contents / Novel Landing Page) or 'CHAPTER' (Single novel chapter).
2. Extract the clean novel title (strip promotional bracket prefixes like 【...】 if present) and author.
3. If TOC: Extract the chapter list with chapter number/index, clean chapter title, and full URL.
4. If CHAPTER: Look for TOC/Index link and Next Chapter link. Also identify current chapter title and number.
"""
                result = await self.llm.generate_json(
                    prompt_text=prompt,
                    schema=ClassificationResult,
                    system_instruction="You are an expert web scraping classifier specialized in novel platforms worldwide.",
                )
                if result.novel_title:
                    result.novel_title = self._clean_novel_title(result.novel_title)

                # Sanitize chapter links
                if result.chapter_links:
                    seen_urls = set()
                    cleaned_links = []
                    for c in result.chapter_links:
                        if c.url not in seen_urls:
                            seen_urls.add(c.url)
                            c.title = clean_chapter_title(c.title)
                            cleaned_links.append(c)
                    cleaned_links, _ = detect_and_fix_reverse_order(cleaned_links)
                    for idx, c in enumerate(cleaned_links, 1):
                        c.index = idx
                    result.chapter_links = cleaned_links

                # If Apollo state contains a more complete TOC, merge/augment it
                if apollo_chapters and len(apollo_chapters) > len(result.chapter_links):
                    result.page_type = "TOC"
                    result.chapter_links = apollo_chapters

            except Exception as e:
                logger.warning(f"LLM classification failed: {e}. Falling back to heuristic classifier.")
                result = self._heuristic_classify(url, html, summary)
        else:
            # Heuristic fallback
            result = self._heuristic_classify(url, html, summary)

        if page_kind == "TOC":
            result.page_type = "TOC"
            result.toc_url = None
        elif result.page_type == "CHAPTER" and recipe:
            result.toc_url = derived_toc_url

        # Autonomous TOC refinement via LangGraph agent
        if result.page_type == "TOC":
            try:
                toc_state = await self.toc_agent.extract_toc(url, html=html)
                if toc_state:
                    result.toc_state = toc_state
                    if toc_state.get("extracted_chapters"):
                        extracted = toc_state["extracted_chapters"]
                        if len(extracted) >= len(result.chapter_links):
                            result.chapter_links = extracted
                    if toc_state.get("novel_title"):
                        result.novel_title = toc_state["novel_title"]
                    if toc_state.get("author"):
                        result.author = toc_state["author"]
                    if toc_state.get("description"):
                        result.description = toc_state["description"]
            except Exception as e:
                logger.warning(f"TocAgent extraction warning: {e}. Keeping classified result.")

        if result.page_type == "TOC" and recipe:
            result.chapter_links = [
                chapter for chapter in result.chapter_links
                if belongs_to_recipe_novel(url, chapter.url, recipe.sample_toc_url, recipe.sample_chapter_url) is not False
            ]
            for index, chapter in enumerate(result.chapter_links, start=1):
                chapter.index = index
            if result.toc_state and result.toc_state.get("extracted_chapters") is not None:
                result.toc_state["extracted_chapters"] = result.chapter_links

        if result.page_type == "CHAPTER":
            from src.handlers.webnovel import is_webnovel_url, parse_webnovel_url
            if is_webnovel_url(url):
                book_id, book_slug, chapter_id = parse_webnovel_url(url)
                if book_id:
                    result.toc_url = f"https://www.webnovel.com/book/{book_id}/catalog"

        return result

    def _heuristic_classify(self, url: str, html: str, summary: dict) -> ClassificationResult:
        """Deterministic heuristic fallback when LLM is unavailable."""
        soup = BeautifulSoup(html, "lxml")
        page_title = summary["title"]

        # Look for chapter patterns in links
        chapter_links = []
        toc_candidates = []
        next_candidates = []
        seen_urls = set()

        # Decompose noisy sub-elements (views, dates, badges)
        for noise in soup.select("time, .date, .view, .views, .count, .num-views, .chapter-release-date, .post-on, .chapter-time, .release-date, .badge, .small, .fst-italic"):
            noise.decompose()

        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            href = a["href"].strip()
            if not text or not href or href.startswith("javascript:") or href.startswith("#"):
                continue
            full_url = urljoin(url, href)
            lower_text = text.lower()
            lower_url = full_url.lower()

            # Check for next chapter
            if "next" in lower_text or "下一章" in lower_text or "next chapter" in lower_text or "次へ" in lower_text:
                next_candidates.append(full_url)

            # Check for TOC / Index
            if lower_text in ["index", "toc", "table of contents", "chapters", "directory", "目录", "home", "目次"]:
                toc_candidates.append(full_url)

            # Chapter link detection
            is_chapter = False
            if re.search(r"\b(?:chapter|ch|episode|part)\b", lower_text, re.IGNORECASE):
                is_chapter = True
            elif re.search(r"第\s*\d+\s*[話章节回]", text) or re.search(r"第.+[話章节回]", text):
                is_chapter = True
            elif any(k in lower_url for k in ["/episodes/", "/chapter-", "/chapter/", "/c/"]):
                is_chapter = True
            elif re.search(r"webnovel\.com/book/[^/]+/[^/]+", lower_url) and not lower_url.endswith("/catalog"):
                is_chapter = True

            if is_chapter and full_url not in seen_urls:
                seen_urls.add(full_url)
                clean_title = clean_chapter_title(text)
                chapter_links.append(ChapterLink(
                    index=len(chapter_links) + 1,
                    title=clean_title or text,
                    url=full_url
                ))

        # Check Next.js Apollo state for complete TOC (e.g. Kakuyomu, etc.)
        next_data_script = soup.find("script", id="__NEXT_DATA__")
        if next_data_script and next_data_script.string:
            try:
                data = json.loads(next_data_script.string)
                apollo = data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})

                # Check for TableOfContentsChapter
                toc_chapter = apollo.get("TableOfContentsChapter:", {})
                episode_unions = toc_chapter.get("episodeUnions", [])

                # Check work object if not in default toc_chapter
                work_match = re.search(r"/works/(\d+)", url)
                work_id = work_match.group(1) if work_match else ""
                work_obj = apollo.get(f"Work:{work_id}", {}) if work_id else {}

                if not episode_unions and "tableOfContentsV2" in work_obj:
                    for toc_ref in work_obj["tableOfContentsV2"]:
                        ref_key = toc_ref.get("__ref")
                        if ref_key and ref_key in apollo:
                            episode_unions.extend(apollo[ref_key].get("episodeUnions", []))

                if episode_unions:
                    parsed_url = urlparse(url)
                    base_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
                    apollo_chapters = []
                    for ep_ref in episode_unions:
                        ref_key = ep_ref.get("__ref")
                        if not ref_key or ref_key not in apollo:
                            continue
                        ep_data = apollo[ref_key]
                        ep_id = ep_data.get("id")
                        ep_title = ep_data.get("title", "")
                        if ep_id and work_id:
                            ep_url = f"{base_origin}/works/{work_id}/episodes/{ep_id}"
                            apollo_chapters.append(ChapterLink(
                                index=len(apollo_chapters) + 1,
                                title=ep_title or f"Episode {len(apollo_chapters) + 1}",
                                url=ep_url
                            ))
                    if apollo_chapters:
                        chapter_links = apollo_chapters
            except Exception as e:
                logger.debug(f"Failed extracting Apollo TOC: {e}")

        # Check WebNovel embedded data
        webnovel_script = soup.find("script", id="__WEBNOVEL_DATA__")
        if webnovel_script and webnovel_script.string:
            try:
                wn_data = json.loads(webnovel_script.string)
                wn_chapters = []
                for item in wn_data.get("chapters", []):
                    wn_chapters.append(ChapterLink(
                        index=item.get("index", len(wn_chapters) + 1),
                        title=item.get("title") or f"Chapter {item.get('order', len(wn_chapters) + 1)}",
                        url=item.get("url"),
                    ))
                if wn_chapters:
                    chapter_links = wn_chapters
            except Exception as e:
                logger.debug(f"Failed extracting WebNovel TOC in heuristic classifier: {e}")

        # If more than 4 chapter links found, it's very likely a TOC page
        is_toc = len(chapter_links) >= 4

        # Deduce author
        author = None
        author_el = soup.select_one(".author, [class*='author'], [itemprop='author'], a[href*='/users/']")
        if author_el:
            author = author_el.get_text(strip=True)
            author = re.sub(r"^(?:author|作者|作者：|Author:)\s*", "", author, flags=re.I).strip()

        # Deduce novel title
        raw_title = summary["h1s"][0] if summary["h1s"] else page_title.split("-")[0].split("|")[0].strip()
        novel_title = self._clean_novel_title(raw_title)

        if is_toc:
            chapter_links, _ = detect_and_fix_reverse_order(chapter_links)
            for idx, ch in enumerate(chapter_links, 1):
                ch.index = idx
                ch.title = clean_chapter_title(ch.title)
            return ClassificationResult(
                page_type="TOC",
                novel_title=novel_title,
                author=author,
                description=summary["description"],
                chapter_links=chapter_links,
            )
        else:
            from src.handlers.webnovel import is_webnovel_url, parse_webnovel_url
            toc_url = toc_candidates[0] if toc_candidates else None
            if not toc_url and is_webnovel_url(url):
                book_id, book_slug, chapter_id = parse_webnovel_url(url)
                if book_id:
                    toc_url = f"https://www.webnovel.com/book/{book_id}/catalog"
            return ClassificationResult(
                page_type="CHAPTER",
                novel_title=novel_title,
                toc_url=toc_url,
                next_chapter_url=next_candidates[0] if next_candidates else None,
                chapter_title=summary["h1s"][0] if summary["h1s"] else page_title,
                chapter_number=1,
            )
