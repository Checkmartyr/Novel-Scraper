import re
from typing import List, Optional, Any
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from src.agent.llm import LLMClient
from src.agent.instructions import agent_system_instruction

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

    def candidate_plans(self, html, previous_signature=None):
        from src.agent.ch.candidates import rank_candidates
        from src.scraper.runtime import ScraperLimits
        return [DOMStructurePlan(title_selector=candidate.title_selector, content_selector=candidate.selector,
                                 remove_selectors=["nav", "footer", "aside", ".comments", "#comments", ".ad-banner", ".nav-bottom", ".cheer-widget"])
                for candidate in rank_candidates(html, ScraperLimits.from_env().candidate_limit, previous_signature)]

    @staticmethod
    def _validate_plan(plan: DOMStructurePlan, html: str) -> DOMStructurePlan:
        """Require observed selectors and reject removals that erase story or title."""
        soup = BeautifulSoup(html, "lxml")
        checked = plan.model_copy(deep=True)
        checked.title_selector = sanitize_css_selector(plan.title_selector)
        checked.content_selector = sanitize_css_selector(plan.content_selector)
        titles = soup.select(checked.title_selector) if checked.title_selector else []
        containers = soup.select(checked.content_selector) if checked.content_selector else []
        if not titles or not containers:
            raise ValueError("Proposed title or content selector is absent from the source DOM")
        protected = titles + containers
        checked.remove_selectors = []
        for selector in plan.remove_selectors:
            selector = sanitize_css_selector(selector)
            if not selector:
                continue
            removed = soup.select(selector)
            if any(element is target or any(element is parent for parent in target.parents)
                   for element in removed for target in protected):
                raise ValueError("Proposed removal would erase the title or story container")
            if removed and selector not in checked.remove_selectors:
                checked.remove_selectors.append(selector)
        return checked

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
                
        from src.agent.ch.candidates import rank_candidates
        candidates = rank_candidates(html)
        if candidates:
            candidate_blocks = [f"[{candidate.selector}] Score: {candidate.score:.2f} | Text Length: {len(candidate.text)} | Beginning: {candidate.text[:180]!r} | Ending: {candidate.text[-180:]!r}"
                                for candidate in candidates]
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
"""
                plan = await self.llm.generate_json(
                    prompt_text=prompt,
                    schema=DOMStructurePlan,
                    system_instruction=agent_system_instruction("chapter_analyzer"),
                    previous_interaction_id=previous_interaction_id,
                )
                return self._validate_plan(plan, html)
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
Source URL: {url}
Current DOM Structure & Candidates:
{self._prepare_dom_skeleton(html)}

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

"""
                plan = await self.llm.generate_json(
                    prompt_text=prompt,
                    schema=DOMStructurePlan,
                    system_instruction=agent_system_instruction("chapter_refiner"),
                    previous_interaction_id=previous_interaction_id,
                )
                return self._validate_plan(plan, html)
            except Exception:
                pass

        # Heuristic refinement: merge recommended fixes if any
        new_plan = current_plan.model_copy(deep=True)
        for fix in getattr(review, "recommended_fixes", []):
            extracted_sel = re.findall(r"[\.#][a-zA-Z0-9_-]+", fix)
            for s in extracted_sel:
                if s not in new_plan.remove_selectors:
                    new_plan.remove_selectors.append(s)
        try:
            return self._validate_plan(new_plan, html)
        except ValueError:
            return current_plan.model_copy(deep=True)

    def _heuristic_analyze(self, html: str) -> DOMStructurePlan:
        """Deterministic heuristic analysis when LLM is unavailable."""
        soup = BeautifulSoup(html, "lxml")
        
        # 1. Title selector heuristic
        title_selector = "h1"
        for sel in [".reader-header-chapter", "h1.p-novel__title.p-novel__title--rensai", "h1.p-novel__title", ".p-novel__title", ".widget-episodeTitle", ".chapter-title", "h2.widget-episode-title", "h1", "h2", "title"]:
            if soup.select_one(sel):
                title_selector = sel
                break
                
        # 2. Content container heuristic (look for common novel content IDs and classes)
        common_content_selectors = [
            ".reader-content",
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
            text = el.get_text(strip=True) if el else ""
            if el and text and not el.select_one(".skeleton, [aria-busy=true]"):
                content_selector = sel
                break
                
        if not content_selector:
            from src.agent.ch.candidates import rank_candidates
            candidates = rank_candidates(html)
            content_selector = candidates[0].selector if candidates else "body"
            if candidates:
                title_selector = candidates[0].title_selector
                
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
