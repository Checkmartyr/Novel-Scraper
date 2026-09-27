import re
from typing import List, Optional, Any
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from src.agent.llm_client import LLMClient

class DOMStructurePlan(BaseModel):
    title_selector: str = Field(description="CSS selector for chapter title heading")
    content_selector: str = Field(description="CSS selector for main novel body text container")
    remove_selectors: List[str] = Field(default_factory=list, description="CSS selectors of elements to remove (ads, nav, scripts)")
    clean_paragraphs: bool = Field(default=True, description="Whether to normalize paragraphs")

import soupsieve

def sanitize_css_selector(selector: str, default: str = "") -> str:
    """Sanitize CSS selector to avoid syntax errors from decimal classes or malformed syntax."""
    if not selector:
        return default
    cleaned = re.sub(r"\.\d+", "", selector).strip()
    try:
        soupsieve.compile(cleaned)
        return cleaned
    except Exception:
        m = re.match(r"^[a-zA-Z0-9_-]+", selector)
        if m:
            return m.group(0)
        return default

class ChapterAnalyzer:
    """Analyzes chapter HTML to locate title and story body selectors."""
    
    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm = llm_client or LLMClient()

    def _prepare_dom_skeleton(self, html: str) -> str:
        """Create a simplified DOM skeleton showing tag names, IDs, classes, and text lengths."""
        soup = BeautifulSoup(html, "lxml")
        
        # Strip script, style, comments
        for tag in soup(["script", "style", "svg", "noscript", "iframe"]):
            tag.decompose()

        page_title = soup.title.get_text(strip=True) if soup.title else ""
            
        # Title candidates: headings (h1-h3) and elements with title/chapter/episode/header keywords
        title_candidates = []
        for elem in soup.find_all(["h1", "h2", "h3", "p", "div", "span"]):
            text = elem.get_text(strip=True)
            text_len = len(text)
            if not text or text_len > 150:
                continue
            classes = elem.get("class", [])
            valid_classes = [c for c in classes if re.match(r"^[a-zA-Z_-][a-zA-Z0-9_-]*$", c)]
            class_str = ".".join(valid_classes[:2])
            elem_id = elem.get("id", "")
            lower_meta = f"{elem.name} {class_str} {elem_id}".lower()
            if elem.name in ["h1", "h2", "h3"] or any(k in lower_meta for k in ["title", "chapter", "episode", "header", "heading"]):
                selector_hint = elem.name
                if elem_id and re.match(r"^[a-zA-Z_-][a-zA-Z0-9_-]*$", elem_id):
                    selector_hint += f"#{elem_id}"
                if class_str:
                    selector_hint += f".{class_str}"
                title_candidates.append(f"[{selector_hint}] \"{text}\"")

        candidate_blocks = []
        for elem in soup.find_all(["div", "article", "section", "main", "p"]):
            text = elem.get_text(strip=True)
            text_len = len(text)
            if text_len > 200:
                classes = elem.get("class", [])
                valid_classes = [c for c in classes if re.match(r"^[a-zA-Z_-][a-zA-Z0-9_-]*$", c)]
                class_str = ".".join(valid_classes[:2])
                elem_id = elem.get("id", "")
                tag_name = elem.name
                selector_hint = f"{tag_name}"
                if elem_id and re.match(r"^[a-zA-Z_-][a-zA-Z0-9_-]*$", elem_id):
                    selector_hint += f"#{elem_id}"
                if class_str:
                    selector_hint += f".{class_str}"
                
                # Check how many elements match this selector
                match_count = 1
                try:
                    match_count = len(soup.select(selector_hint))
                except Exception:
                    pass
                count_info = f" (matches {match_count} elements)" if match_count > 1 else ""
                
                # Sample text preview
                sample_snippet = text[:120].replace("\n", " ")
                candidate_blocks.append(f"[{selector_hint}]{count_info} Text Length: {text_len} | Sample: \"{sample_snippet}...\"")
                
        skeleton = (
            f"Page <title>: {page_title}\n\n"
            f"Title & Heading Candidates:\n" + "\n".join(title_candidates[:20]) + "\n\n"
            f"Large Text Containers:\n" + "\n".join(candidate_blocks[:15])
        )
        return skeleton

    async def analyze(self, html: str, url: str, previous_interaction_id: Optional[str] = None) -> DOMStructurePlan:
        """Analyze chapter HTML to discover optimal selectors using LangChain & Interactions API."""
        skeleton = self._prepare_dom_skeleton(html)
        
        if self.llm.is_available:
            try:
                prompt = f"""
Analyze the chapter webpage DOM:
URL: {url}

DOM Structure & Candidates:
{skeleton}

Task:
1. Identify the most specific CSS selector for the Chapter Title (check Title & Heading Candidates above).
2. Identify the most specific CSS selector for the Main Story Content container.
   IMPORTANT: Prefer the outermost enclosing container (e.g. 'div.p-novel__body', '#chapter-content', or 'article') that encloses the entire chapter text including any author prefaces, chapter body, and afterwords, rather than an inner sub-block that only matches the preface or a single subsection.
3. Identify CSS selectors for clutter elements to REMOVE (ads, nav buttons, share widgets, watermarks).
"""
                plan = await self.llm.generate_json(
                    prompt_text=prompt,
                    schema=DOMStructurePlan,
                    system_instruction="You are an expert web scraping engineer analyzing novel chapter HTML.",
                    previous_interaction_id=previous_interaction_id,
                )
                plan.title_selector = sanitize_css_selector(plan.title_selector, default="h1")
                plan.content_selector = sanitize_css_selector(plan.content_selector, default="body")
                plan.remove_selectors = [sanitize_css_selector(s) for s in plan.remove_selectors if sanitize_css_selector(s)]
                return plan
            except Exception:
                pass  # Fallback to heuristic
                
        return self._heuristic_analyze(html)

    async def refine(
        self,
        html: str,
        url: str,
        current_plan: DOMStructurePlan,
        review: Any,
        previous_interaction_id: Optional[str] = None,
    ) -> DOMStructurePlan:
        """Refine extraction plan based on feedback from the Quality Control Observer."""
        if self.llm.is_available:
            try:
                issues_list = "\n".join([f"- {issue}" for issue in getattr(review, "issues", [])]) or "None"
                fixes_list = "\n".join([f"- {fix}" for fix in getattr(review, "recommended_fixes", [])]) or "None"
                
                prompt = f"""
The previous DOM extraction plan produced defects according to the Quality Control Observer:
Current Plan:
- Title Selector: {current_plan.title_selector}
- Content Selector: {current_plan.content_selector}
- Remove Selectors: {current_plan.remove_selectors}

Observer Evaluation:
- Quality Score: {getattr(review, 'quality_score', 0.0)}
- Title Accurate: {getattr(review, 'title_accurate', True)}
- Has Clutter: {getattr(review, 'has_clutter', False)}
- Issues Detected:
{issues_list}
- Recommended Fixes:
{fixes_list}

Task:
Synthesize an improved, refined DOMStructurePlan that completely fixes all reported issues:
1. If Title was inaccurate or generic: update title_selector to pinpoint the exact chapter heading.
2. If Clutter (ads, nav, comments, cheer buttons) was present: identify and add their CSS selectors to remove_selectors.
3. Ensure content_selector cleanly captures only the story paragraphs.
"""
                return await self.llm.generate_json(
                    prompt_text=prompt,
                    schema=DOMStructurePlan,
                    system_instruction="You are an expert web scraping engineer repairing and optimizing extraction selectors based on quality audit feedback.",
                    previous_interaction_id=previous_interaction_id,
                )
            except Exception:
                pass

        # Heuristic refinement: merge recommended fixes if any
        new_plan = current_plan.model_copy(deep=True)
        for fix in getattr(review, "recommended_fixes", []):
            extracted_sel = re.findall(r"[\.#][a-zA-Z0-9_-]+", fix)
            for s in extracted_sel:
                if s not in new_plan.remove_selectors:
                    new_plan.remove_selectors.append(s)
        return new_plan

    def _heuristic_analyze(self, html: str) -> DOMStructurePlan:
        """Deterministic heuristic analysis when LLM is unavailable."""
        soup = BeautifulSoup(html, "lxml")
        
        # 1. Title selector heuristic
        title_selector = "h1"
        for sel in ["h1.p-novel__title.p-novel__title--rensai", "h1.p-novel__title", ".p-novel__title", ".widget-episodeTitle", ".chapter-title", "h2.widget-episode-title", "h1", "h2", "title"]:
            if soup.select_one(sel):
                title_selector = sel
                break
                
        # 2. Content container heuristic (look for common novel content IDs and classes)
        common_content_selectors = [
            "div.p-novel__body",
            ".p-novel__body",
            ".widget-episodeBody",
            ".js-episode-body",
            "#novel_honbun",
            "#chapter-content",
            ".cha-words",
            ".cha-content",
            "#content",
            ".chapter-content",
            ".entry-content",
            ".content",
            ".text-content",
            "#chapter_content",
            ".read-content",
            "article",
            "main",
        ]
        
        content_selector = None
        for sel in common_content_selectors:
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 100:
                content_selector = sel
                break
                
        if not content_selector:
            # Pick the element with highest text length
            candidates = soup.find_all(["div", "article", "section", "main"])
            candidates.sort(key=lambda e: len(e.get_text(strip=True)), reverse=True)
            if candidates:
                best = candidates[0]
                if best.get("id"):
                    content_selector = f"#{best['id']}"
                elif best.get("class"):
                    content_selector = f"{best.name}.{'.'.join(best['class'])}"
                else:
                    content_selector = best.name
            else:
                content_selector = "body"
                
        remove_selectors = [
            "script",
            "style",
            ".ad",
            ".ads",
            ".advertisement",
            ".share",
            ".social",
            "nav",
            ".nav",
            ".prev-next",
            ".comments",
            ".m-thou",
            ".user-links-wrap",
            ".report-wrap",
            ".j_reportWrap",
            ".cha-bts",
            ".cha-btn-box",
        ]
        
        return DOMStructurePlan(
            title_selector=title_selector,
            content_selector=content_selector,
            remove_selectors=remove_selectors,
            clean_paragraphs=True,
        )
