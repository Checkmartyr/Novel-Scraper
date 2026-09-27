"""Autonomous TOC extraction tools for LangGraph agent."""

import json
import logging
import re
from typing import List, Optional, Tuple, Set, Dict, Any
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

from src.agent.classifier import ChapterLink
from src.core.obscura_client import ObscuraClient

logger = logging.getLogger("agent.toc.tools")


class ClaimInspector:
    """Inspects page text, meta tags, and data attributes to discover claimed chapter counts and metadata."""

    @staticmethod
    def inspect(html: str, url: str) -> Tuple[Optional[int], str, Optional[str], Optional[str]]:
        """Return (claimed_count, novel_title, author, description)."""
        soup = BeautifulSoup(html, "lxml")
        claimed_count: Optional[int] = None
        
        # 1. Title & Meta
        h1_el = soup.find("h1")
        h1_title = h1_el.get_text(strip=True) if h1_el else ""
        raw_title = h1_title or (soup.title.get_text(strip=True) if soup.title else "")
        cleaned_title = re.sub(r"^[【\[].*?[】\]]\s*", "", raw_title)
        cleaned_title = re.split(r"\s*[-|–]\s*(?:Table of Contents|TOC|Index|目次|全話一覧|カクヨム|小説家になろう|Novel Updates|Syosetu)", cleaned_title, flags=re.I)[0].strip()
        novel_title = cleaned_title or raw_title or "Untitled Novel"

        # Description
        desc_el = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        description = desc_el["content"].strip() if desc_el and desc_el.get("content") else None

        # Author
        author = None
        author_el = soup.select_one(".author, [class*='author'], [itemprop='author'], a[href*='/users/'], .novel_writername, .p-novel__author, a[href*='/mypage/'], a[href*='author=']")
        if author_el:
            author = author_el.get_text(strip=True)
            author = re.sub(r"^(?:作者[：:]|著者[：:]|author[:\s])\s*", "", author, flags=re.I).strip()

        # Check for author in title parentheses, e.g. "タイトル（作者名）"
        author_in_title = re.search(r"[（\(]([^）\)]+)[）\)]$", cleaned_title)
        if author_in_title:
            if not author:
                author = author_in_title.group(1).strip()
            cleaned_title = cleaned_title[:author_in_title.start()].strip()

        if author:
            author = re.sub(r"^(?:作者[：:]|著者[：:]|author[:\s])\s*", "", author, flags=re.I).strip()

        novel_title = cleaned_title or raw_title or "Untitled Novel"

        # Check for Nekopost embedded data
        nekopost_script = soup.find("script", id="__NEKOPOST_DATA__")
        if nekopost_script and nekopost_script.string:
            try:
                neko_data = json.loads(nekopost_script.string)
                if neko_data.get("novel_title"):
                    novel_title = neko_data["novel_title"]
                if neko_data.get("author"):
                    author = neko_data["author"]
                if neko_data.get("description"):
                    description = neko_data["description"]
                if neko_data.get("total_chapters"):
                    claimed_count = int(neko_data["total_chapters"])
            except Exception:
                pass

        # Check for Dek-D embedded data
        dekd_script = soup.find("script", id="__DEKD_DATA__")
        if dekd_script and dekd_script.string:
            try:
                dekd_data = json.loads(dekd_script.string)
                if dekd_data.get("novel_title"):
                    novel_title = dekd_data["novel_title"]
                if dekd_data.get("author"):
                    author = dekd_data["author"]
                if dekd_data.get("description"):
                    description = dekd_data["description"]
                if dekd_data.get("total_chapters"):
                    claimed_count = int(dekd_data["total_chapters"])
            except Exception:
                pass

        # Check for WebNovel embedded data
        webnovel_script = soup.find("script", id="__WEBNOVEL_DATA__")
        if webnovel_script and webnovel_script.string:
            try:
                wn_data = json.loads(webnovel_script.string)
                if wn_data.get("novel_title"):
                    novel_title = wn_data["novel_title"]
                if wn_data.get("author"):
                    author = wn_data["author"]
                if wn_data.get("description"):
                    description = wn_data["description"]
                if wn_data.get("total_chapters"):
                    claimed_count = int(wn_data["total_chapters"])
            except Exception:
                pass

        # 2. Extract Claimed Count from Embedded JSON (highest precision)
        # Next.js / JSON-LD / Redux / WebNovel g_data
        if claimed_count is None:
            json_matches = re.findall(r'"(?:publicEpisodeCount|totalEpisodes|episodeCount|chapterCount|totalChapterNum|chapterNum)":\s*(\d+)', html, re.I)
            if json_matches:
                try:
                    claimed_count = int(json_matches[0])
                except ValueError:
                    pass

        # 3. Extract Claimed Count from Header Cards & Page Text Badges
        if claimed_count is None:
            # Header cards like "Last Chapter Chapter 665" or "Latest Chapter: 665"
            last_ch_match = re.search(r"(?:Last|Latest)\s*Chapter\s*(?:Chapter)?\s*[:\s]*(\d+)", html, re.I)
            if last_ch_match:
                claimed_count = int(last_ch_match.group(1))

        if claimed_count is None:
            # Patterns like 全111話 or 全 111 章
            badge_match = re.search(r"全\s*(\d+)\s*[話章节回]", html)
            if badge_match:
                claimed_count = int(badge_match.group(1))

        if claimed_count is None:
            # Patterns like "111 話" or "111 Chapters"
            count_match = re.search(r"\b(\d{1,5})\s*(?:話|chapters?|episodes?)\b", html, re.I)
            if count_match:
                claimed_count = int(count_match.group(1))

        if claimed_count is None:
            # Thai pattern: e.g. "62 ตอน" or <span class="stat_number">62</span> ... <p class="iconname">ตอน</p>
            thai_stat_match = re.search(r'class="stat_number">\s*(\d{1,5})\s*</span>.*?<p class="iconname">\s*ตอน', html, re.S)
            if thai_stat_match:
                claimed_count = int(thai_stat_match.group(1))
            else:
                thai_match = re.search(r"\b(\d{1,5})\s*ตอน\b", html)
                if thai_match:
                    claimed_count = int(thai_match.group(1))

        # 4. Check Range Badges on accordion buttons (e.g. "91〜111" implies 111!)
        if claimed_count is None:
            range_candidates = []
            for b in soup.find_all(["button", "a", "span"]):
                text = b.get_text(strip=True)
                m = re.search(r"\b\d+\s*[〜~–-]\s*(\d+)\b", text)
                if m:
                    try:
                        val = int(m.group(1))
                        if 0 < val < 50000:
                            range_candidates.append(val)
                    except ValueError:
                        pass
            if range_candidates:
                claimed_count = max(range_candidates)

        return claimed_count, novel_title, author, description


class EmbeddedStateExtractor:
    """Extracts complete TOC directly from SSR hydration payloads (Next.js Apollo, Redux, Nuxt, JSON-LD)."""

    @staticmethod
    def extract(html: str, url: str) -> List[ChapterLink]:
        """Extract chapters from embedded JSON frameworks."""
        soup = BeautifulSoup(html, "lxml")
        chapters: List[ChapterLink] = []

        # A. Next.js Apollo Cache (__NEXT_DATA__)
        next_data_script = soup.find("script", id="__NEXT_DATA__")
        if next_data_script and next_data_script.string:
            try:
                data = json.loads(next_data_script.string)
                apollo = data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
                
                # Check for TableOfContentsChapter
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
                    for ep_ref in episode_unions:
                        ref_key = ep_ref.get("__ref")
                        if not ref_key or ref_key not in apollo:
                            continue
                        ep_data = apollo[ref_key]
                        ep_id = ep_data.get("id")
                        ep_title = ep_data.get("title", "")
                        if ep_id and work_id:
                            ep_url = f"{base_origin}/works/{work_id}/episodes/{ep_id}"
                            chapters.append(ChapterLink(
                                index=len(chapters) + 1,
                                title=ep_title or f"Episode {len(chapters) + 1}",
                                url=ep_url
                            ))
                    if chapters:
                        logger.info(f"Extracted {len(chapters)} chapters from Next.js Apollo state.")
                        return chapters
            except Exception as e:
                logger.debug(f"Apollo state extraction error: {e}")

        # B. JSON-LD Schema (ItemList / hasPart)
        for s in soup.find_all("script", type="application/ld+json"):
            if not s.string:
                continue
            try:
                ld = json.loads(s.string)
                items = []
                if isinstance(ld, dict):
                    if ld.get("@type") == "ItemList":
                        items = ld.get("itemListElement", [])
                    elif "hasPart" in ld and isinstance(ld["hasPart"], list):
                        items = ld["hasPart"]
                elif isinstance(ld, list):
                    for obj in ld:
                        if isinstance(obj, dict) and obj.get("@type") == "ItemList":
                            items = obj.get("itemListElement", [])
                            break

                for it in items:
                    item_url = it.get("url") or (it.get("item", {}).get("@id") if isinstance(it.get("item"), dict) else None)
                    item_name = it.get("name") or (it.get("item", {}).get("name") if isinstance(it.get("item"), dict) else None)
                    if item_url:
                        full_u = urljoin(url, item_url)
                        chapters.append(ChapterLink(
                            index=len(chapters) + 1,
                            title=item_name or f"Chapter {len(chapters) + 1}",
                            url=full_u
                        ))
                if len(chapters) >= 4:
                    logger.info(f"Extracted {len(chapters)} chapters from JSON-LD schema.")
                    return chapters
            except Exception:
                continue

        # C. Nekopost embedded data (__NEKOPOST_DATA__)
        nekopost_script = soup.find("script", id="__NEKOPOST_DATA__")
        if nekopost_script and nekopost_script.string:
            try:
                neko_data = json.loads(nekopost_script.string)
                for item in neko_data.get("chapters", []):
                    chapters.append(ChapterLink(
                        index=item.get("index", len(chapters) + 1),
                        title=item.get("title") or f"Chapter {item.get('chapter_no')}",
                        url=item.get("url"),
                    ))
                if chapters:
                    logger.info(f"Extracted {len(chapters)} chapters from Nekopost embedded data.")
                    return chapters
            except Exception as e:
                logger.debug(f"Nekopost data extraction error: {e}")

        # D. Dek-D embedded data (__DEKD_DATA__)
        dekd_script = soup.find("script", id="__DEKD_DATA__")
        if dekd_script and dekd_script.string:
            try:
                dekd_data = json.loads(dekd_script.string)
                for item in dekd_data.get("chapters", []):
                    chapters.append(ChapterLink(
                        index=item.get("index", len(chapters) + 1),
                        title=item.get("title") or f"Chapter {item.get('order')}",
                        url=item.get("url"),
                    ))
                if chapters:
                    logger.info(f"Extracted {len(chapters)} chapters from Dek-D embedded data.")
                    return chapters
            except Exception as e:
                logger.debug(f"Dek-D data extraction error: {e}")

        # E. WebNovel embedded data (__WEBNOVEL_DATA__)
        webnovel_script = soup.find("script", id="__WEBNOVEL_DATA__")
        if webnovel_script and webnovel_script.string:
            try:
                wn_data = json.loads(webnovel_script.string)
                for item in wn_data.get("chapters", []):
                    chapters.append(ChapterLink(
                        index=item.get("index", len(chapters) + 1),
                        title=item.get("title") or f"Chapter {item.get('order', len(chapters) + 1)}",
                        url=item.get("url"),
                    ))
                if chapters:
                    logger.info(f"Extracted {len(chapters)} chapters from WebNovel embedded data.")
                    return chapters
            except Exception as e:
                logger.debug(f"WebNovel data extraction error: {e}")

        return chapters


class DomLinkExtractor:
    """Extracts chapter links directly from current DOM structure."""

    @staticmethod
    def extract(html: str, url: str) -> List[ChapterLink]:
        soup = BeautifulSoup(html, "lxml")
        chapters: List[ChapterLink] = []
        seen_urls: Set[str] = set()

        # Modern Kakuyomu WorkTocSection
        toc_items = soup.select('a[class*="WorkTocSection_link"]')
        if toc_items:
            for a in toc_items:
                href = a.get("href", "").strip()
                if not href or "/episodes/" not in href:
                    continue
                full_url = urljoin(url, href)
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                
                title_el = a.select_one('[class*="WorkTocSection_title"]') or a
                title = title_el.get_text(strip=True)
                title = re.sub(r"\s*[（\(\[]\s*\d+\s*/\s*\d+\s*[）\)\]]", "", title).strip()
                chapters.append(ChapterLink(
                    index=len(chapters) + 1,
                    title=title or f"Chapter {len(chapters) + 1}",
                    url=full_url
                ))
            if chapters:
                return chapters

        # Syosetu (小説家になろう) episode links
        syosetu_items = soup.select(".p-eplist__subtitle, dd.subtitle a, a.p-eplist__subtitle")
        if syosetu_items:
            for a in syosetu_items:
                href = a.get("href", "").strip()
                if not href or href.startswith("javascript:") or href.startswith("#"):
                    continue
                full_url = urljoin(url, href)
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                
                title = a.get_text(strip=True)
                title = re.sub(r"\s*[（\(\[]\s*\d+\s*/\s*\d+\s*[）\)\]]", "", title).strip()
                chapters.append(ChapterLink(
                    index=len(chapters) + 1,
                    title=title or f"Chapter {len(chapters) + 1}",
                    url=full_url
                ))
            if chapters:
                return chapters

        # WebNovel catalog items (.volume-item li.g_col a, ol.content-list li.g_col a, li.g_col a)
        webnovel_items = soup.select("div.volume-item li.g_col a, ol.content-list li.g_col a, li.g_col a")
        if webnovel_items:
            for a in webnovel_items:
                href = a.get("href", "").strip()
                if not href or href.startswith("javascript:") or href.startswith("#") or "/catalog" in href:
                    continue
                full_url = urljoin(url, href)
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                strong = a.find("strong")
                title = a.get("title") or (strong.get_text(strip=True) if strong else a.get_text(strip=True))
                # Strip relative time indicators
                title = re.sub(
                    r"\d+\s+(?:years?|months?|weeks?|days?|hours?|mins?|minutes?|secs?|seconds?)\s+ago",
                    "",
                    title,
                    flags=re.I,
                ).strip()
                chapters.append(ChapterLink(
                    index=len(chapters) + 1,
                    title=title or f"Chapter {len(chapters) + 1}",
                    url=full_url
                ))
            if chapters:
                return chapters

        # EmpireNovel & similar webnovel chapter cards (a.chapter_link, a.chapter-link, .chapter a)
        card_items = soup.select("a.chapter_link, a.chapter-link, .chapter a")
        if card_items:
            for a in card_items:
                href = a.get("href", "").strip()
                if not href or href.startswith("javascript:") or href.startswith("#"):
                    continue
                txt = a.get_text(strip=True)
                if "First Chapter" in txt or "Last Chapter" in txt:
                    continue

                full_url = urljoin(url, href)
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                # Strip small/italic dates e.g. <div class="small fst-italic">Jul 27, 2026</div>
                a_copy = BeautifulSoup(str(a), "lxml")
                for sub in a_copy.select(".small, .fst-italic, time, .date"):
                    sub.decompose()

                title = a_copy.get_text(strip=True)
                title = re.sub(r"[\s\xa0]+", " ", title).strip()
                chapters.append(ChapterLink(
                    index=len(chapters) + 1,
                    title=title or f"Chapter {len(chapters) + 1}",
                    url=full_url
                ))
            if chapters:
                return chapters

        # Generic Semantic TOC links
        action_btn_re = re.compile(
            r"^(?:read\s*(?:first|latest|now)|first\s*chapter|last\s*chapter|continue\s*reading|bookmark|share|follow|1話目から読む|最初から読む)\b",
            re.I,
        )

        url_to_chapter: Dict[str, Dict[str, Any]] = {}
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = a["href"].strip()
            if not text or not href or href.startswith("javascript:") or href.startswith("#"):
                continue
            if action_btn_re.search(text):
                continue

            full_url = urljoin(url, href)
            lower_text = text.lower()
            lower_url = full_url.lower()

            is_chapter = False
            if any(k in lower_url for k in ["/episodes/", "/chapter-", "/chapter/", "/c/", "/read/", "viewlongc.php", "chapter="]):
                is_chapter = True
            elif re.search(r"第\s*\d+\s*[話章节回]", text) or re.search(r"\b(?:chapter|ch|episode)\s*\d+\b", lower_text):
                is_chapter = True
            else:
                # Check numeric chapter path pattern (e.g. /n2273dh/1/)
                parsed_u = urlparse(full_url)
                path_parts = [p for p in parsed_u.path.strip("/").split("/") if p]
                if len(path_parts) >= 2 and path_parts[-1].isdigit():
                    is_chapter = True

            if is_chapter:
                a_copy = BeautifulSoup(str(a), "lxml")
                for sub in a_copy.select(".small, .fst-italic, time, .date"):
                    sub.decompose()
                clean_text = a_copy.get_text(strip=True)
                clean_title = re.sub(r"\s*[（\(\[]\s*\d+\s*/\s*\d+\s*[）\)\]]", "", clean_text).strip()
                clean_title = re.sub(r"[\s\xa0]+", " ", clean_title).strip()
                final_title = clean_title or text

                # Parse chapter number
                ch_num = None
                m_num = re.search(r"\b(?:chapter|ch|episode|ตอนที่|ตอน|第)\s*(\d+(?:\.\d+)?)\b", final_title, re.I)
                if m_num:
                    try:
                        ch_num = float(m_num.group(1))
                    except ValueError:
                        pass
                if ch_num is None:
                    m_url = re.search(r"/(?:chapter|ch|episode)-(\d+(?:\.\d+)?)\b", full_url, re.I)
                    if not m_url:
                        m_url = re.search(r"[?&]chapter=(\d+(?:\.\d+)?)", full_url, re.I)
                    if m_url:
                        try:
                            ch_num = float(m_url.group(1))
                        except ValueError:
                            pass

                if full_url in url_to_chapter:
                    existing = url_to_chapter[full_url]
                    if len(final_title) > len(existing["title"]):
                        existing["title"] = final_title
                    if ch_num is not None and existing["ch_num"] is None:
                        existing["ch_num"] = ch_num
                else:
                    url_to_chapter[full_url] = {
                        "url": full_url,
                        "title": final_title,
                        "ch_num": ch_num,
                        "original_index": len(url_to_chapter),
                    }

        if url_to_chapter:
            extracted_items = list(url_to_chapter.values())
            num_count = sum(1 for it in extracted_items if it["ch_num"] is not None)
            if num_count >= len(extracted_items) * 0.8 and len(extracted_items) > 1:
                extracted_items.sort(
                    key=lambda x: (x["ch_num"] if x["ch_num"] is not None else float("inf"), x["original_index"])
                )

            for idx, it in enumerate(extracted_items, start=1):
                chapters.append(ChapterLink(
                    index=idx,
                    title=it["title"],
                    url=it["url"],
                    chapter_number=it["ch_num"],
                ))

        return chapters


class InteractiveDomExpander:
    """Uses Playwright via Obscura to interactively expand accordions, click 'load more', and scroll."""

    @staticmethod
    async def expand(url: str, obscura: ObscuraClient) -> str:
        """Open page with Playwright, interactively expand all collapsed sections, and return enriched HTML."""
        logger.info(f"Interactively expanding DOM for {url} via Playwright...")
        async with obscura.get_playwright_page(stealth=True) as page:
            await page.goto(url, wait_until="networkidle", timeout=30000)

            # 1. Expand all collapsed accordions
            expand_js = """
            async () => {
                // A. Click accordion headers with ranges or collapsed triggers
                const buttons = Array.from(document.querySelectorAll("button, [role='button'], .WorkTocAccordion_trigger"));
                for (const b of buttons) {
                    const text = b.innerText || "";
                    // Range buttons like 1〜30, 31〜60, or Volume headers
                    if (text.match(/\\d+〜\\d+/) || b.classList.contains("WorkTocAccordion_trigger")) {
                        const target = b.closest("button") || b;
                        target.click();
                    }
                }
                await new Promise(r => setTimeout(r, 800));

                // B. Repeatedly click 'Show More' / 'Load More' buttons
                for (let round = 0; round < 10; round++) {
                    const moreBtns = Array.from(document.querySelectorAll("button")).filter(b => {
                        const t = (b.innerText || "").trim();
                        return t.includes("つづきを表示") || t.includes("もっと見る") || t.includes("Load more") || t.includes("展開");
                    });
                    if (moreBtns.length === 0) break;
                    let clicked = false;
                    for (const mb of moreBtns) {
                        mb.click();
                        clicked = true;
                    }
                    if (!clicked) break;
                    await new Promise(r => setTimeout(r, 600));
                }

                // C. Smooth scroll down to bottom and back up to trigger lazy rendering
                window.scrollTo({ top: document.body.scrollHeight / 2, behavior: 'smooth' });
                await new Promise(r => setTimeout(r, 500));
                window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
                await new Promise(r => setTimeout(r, 800));
            }
            """
            try:
                await page.evaluate(expand_js)
                await page.wait_for_timeout(1500)
            except Exception as e:
                logger.warning(f"Interactive expansion JS error: {e}")

            return await page.content()


class PaginatedTocCrawler:
    """Discovers and crawls multi-page TOC pagination links (e.g. ?p=2..27)."""

    @staticmethod
    def detect_pagination_urls(html: str, base_url: str) -> List[str]:
        soup = BeautifulSoup(html, "lxml")
        cand_urls = []
        seen = {base_url}

        # 1. Detect if a maximum page number is linked (e.g. Syosetu .c-pager__item--last, ?p=N, ?page=N)
        max_page = 1
        page_param = "p"
        base_clean = base_url.split("?")[0]

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            m = re.search(r"[?&](p|page)=(\d+)", href, re.I)
            if m:
                p_name = m.group(1)
                p_val = int(m.group(2))
                if p_val > max_page:
                    max_page = p_val
                    page_param = p_name

        # If a sequence of pages is detected, generate full range ?p=2..max_page
        if max_page > 1:
            limit = min(max_page, 100)  # Safety ceiling of 100 pages (~10,000 chapters)
            return [f"{base_clean}?{page_param}={i}" for i in range(2, limit + 1)]

        # 2. Fallback: discover standard discrete pagination links
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            text = a.get_text(strip=True)
            if not href or href.startswith("javascript:") or href.startswith("#"):
                continue
            
            is_page = False
            if re.search(r"[?&](?:page|p)=\d+", href, re.I):
                is_page = True
            elif re.search(r"/page/\d+", href, re.I):
                is_page = True
            elif text in ["2", "3", "4", "5", "6", "7", "8", "9", "Next", "次へ", "下一页"]:
                if any(p in href.lower() for p in ["page", "p=", "index"]):
                    is_page = True

            if is_page:
                full_u = urljoin(base_url, href)
                if full_u not in seen:
                    seen.add(full_u)
                    cand_urls.append(full_u)

        return cand_urls[:50]


class TocAuditor:
    """Critic and auditor that assesses extraction completeness against claims and indicators."""

    @staticmethod
    def audit(
        chapters: List[ChapterLink],
        claimed_count: Optional[int],
        html: str,
        check_pagination: bool = True,
    ) -> Tuple[bool, float, bool, bool, List[str]]:
        """Evaluate extraction completeness.
        
        Returns:
            is_complete (bool),
            confidence_score (float, 0.0 - 1.0),
            has_unexpanded_sections (bool),
            has_pagination (bool),
            issues (List[str])
        """
        issues: List[str] = []
        count = len(chapters)
        
        # Check for unexpanded accordions or load more
        has_unexpanded_sections = False
        if any(k in html for k in ["つづきを表示", "もっと見る", "WorkTocAccordion", "data-accordion"]):
            has_unexpanded_sections = True
            
        # Check for multi-page pagination
        has_pagination = False
        if check_pagination and re.search(r"[?&](?:page|p)=\d+", html, re.I):
            has_pagination = True

        if count == 0:
            return False, 0.0, has_unexpanded_sections, has_pagination, ["Zero chapters extracted from page."]

        # Compare with claimed count
        if claimed_count and claimed_count > 0:
            ratio = count / claimed_count
            if count >= claimed_count:
                # Complete or exceeded
                confidence = 1.0
                is_complete = True
            elif ratio >= 0.95:
                # Within 95% margin
                confidence = 0.95
                is_complete = True
            else:
                confidence = max(0.1, round(ratio, 2))
                is_complete = False
                issues.append(f"Chapter count discrepancy: Extracted {count} chapters, but page indicates {claimed_count} chapters.")
        else:
            # No claimed count available; evaluate based on indicators
            if has_pagination:
                confidence = 0.6
                is_complete = False
                issues.append("Page contains unvisited pagination links.")
            elif has_unexpanded_sections:
                confidence = 0.6
                is_complete = False
                issues.append("Page contains unexpanded accordion sections or 'load more' buttons.")
            elif count >= 4:
                confidence = 0.9
                is_complete = True
            else:
                confidence = 0.5
                is_complete = False
                issues.append(f"Only {count} chapters found and page structure may be incomplete.")

        return is_complete, confidence, has_unexpanded_sections, has_pagination, issues


class TocSynthesizer:
    """Synthesizes custom chapter list container and link selectors when heuristics fall short."""

    @staticmethod
    def synthesize(
        html: str,
        url: str,
        claimed_count: Optional[int] = None,
    ) -> Tuple[List[ChapterLink], Optional[str], Optional[str]]:
        """Cluster links by container and score candidates to recover novel chapter links."""
        soup = BeautifulSoup(html, "lxml")

        # Action button and nav link filter
        action_btn_re = re.compile(
            r"^(?:read\s*(?:first|latest|now)|first\s*chapter|last\s*chapter|continue\s*reading|bookmark|share|follow|1話目から読む|最初から読む|login|sign\s*up|home|catalog|search)\b",
            re.I,
        )

        clusters: Dict[str, List[Tuple[str, str]]] = {}
        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if not href or href.startswith("javascript:") or href.startswith("#"):
                continue
            text = a.get_text(" ", strip=True)
            text_clean = re.sub(r"[\s\xa0]+", " ", text).strip()
            if not text_clean or len(text_clean) > 200 or action_btn_re.search(text_clean):
                continue

            full_url = urljoin(url, href)
            parent = a.parent
            parent_sel = ""
            if parent and parent.name not in ["html", "body"]:
                classes = parent.get("class", [])
                valid_classes = [c for c in classes if re.match(r"^[a-zA-Z_-][a-zA-Z0-9_-]*$", c)]
                if valid_classes:
                    parent_sel = f"{parent.name}.{'.'.join(valid_classes[:2])}"
                elif parent.get("id"):
                    parent_sel = f"{parent.name}#{parent['id']}"
                else:
                    parent_sel = parent.name

            a_classes = [c for c in a.get("class", []) if re.match(r"^[a-zA-Z_-][a-zA-Z0-9_-]*$", c)]
            a_sel = f"a.{'.'.join(a_classes[:2])}" if a_classes else "a"
            combined_key = f"{parent_sel} {a_sel}".strip()

            clusters.setdefault(combined_key, []).append((text_clean, full_url))

        best_cluster_key = None
        best_score = -1.0
        best_items: List[Tuple[str, str]] = []

        for key, items in clusters.items():
            if len(items) < 2:
                continue

            has_num = sum(1 for t, u in items if re.search(r"(?:chapter|ch[\.-]?|ep[\.-]?|第|\b)\d+", f"{t} {u}", re.I))
            num_ratio = has_num / len(items)

            count_score = 1.0
            if claimed_count and claimed_count > 0:
                count_score = 1.0 - min(abs(len(items) - claimed_count) / claimed_count, 1.0)

            score = (len(items) * 0.4) + (num_ratio * 40.0) + (count_score * 30.0)
            if score > best_score:
                best_score = score
                best_cluster_key = key
                best_items = items

        chapters: List[ChapterLink] = []
        if best_items and (best_score >= 10.0 or len(best_items) >= 3):
            seen_urls = set()
            for text, u in best_items:
                if u not in seen_urls:
                    seen_urls.add(u)
                    chapters.append(ChapterLink(
                        index=len(chapters) + 1,
                        title=text,
                        url=u
                    ))

        container_sel = best_cluster_key.split()[0] if best_cluster_key and " " in best_cluster_key else best_cluster_key
        link_sel = best_cluster_key.split()[-1] if best_cluster_key and " " in best_cluster_key else "a[href]"
        return chapters, container_sel, link_sel
