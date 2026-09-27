import re
from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from src.agent.ch.analyzer import DOMStructurePlan
from src.utils.title_cleaner import clean_chapter_title, clean_chapter_content

DEFAULT_NOISE_SELECTORS = [
    ".disclaimer", ".notice", ".alert", ".premium-notice", ".vip-notice",
    ".membership", ".chapter-warning", ".post-views", ".entry-meta",
    ".sharedaddy", ".jp-relatedposts"
]

class ExtractedChapter(BaseModel):
    title: str
    content_markdown: str
    word_count: int
    char_count: int
    success: bool
    error: Optional[str] = None
    is_paywalled: bool = False

class ParserVerificationResult(BaseModel):
    success: bool
    chapter_title: str
    word_count: int
    preview_markdown: str
    generated_code: str
    error: Optional[str] = None

class ChapterCodeGenerator:
    """Generates and executes deterministic Python extraction code from DOM plan."""
    
    def __init__(self, plan: DOMStructurePlan):
        self.plan = plan

    def generate_code_string(self) -> str:
        """Generate standalone Python scraper function code."""
        removes_repr = repr(self.plan.remove_selectors)
        code = f'''from bs4 import BeautifulSoup
import re

def extract_chapter(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    
    # Remove clutter elements
    remove_selectors = {removes_repr}
    for sel in remove_selectors:
        for tag in soup.select(sel):
            tag.decompose()
            
    # Extract Title
    title = ""
    title_el = soup.select_one("{self.plan.title_selector}")
    if title_el:
        title = title_el.get_text(strip=True)
        
    # Extract Content
    content_elements = soup.select("{self.plan.content_selector}")
    top_elements = [
        el for el in content_elements
        if not any(other is not el and other in el.parents for other in content_elements)
    ]
    if not top_elements:
        return {{"title": title, "content": "", "word_count": 0, "error": "Content container not found"}}
        
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
            text = container.get_text("\\n", strip=True)
            for line in text.split("\\n"):
                line = line.strip()
                if line:
                    paragraphs.append(line)
                
    content_md = "\\n\\n".join(paragraphs)
    words = len(re.findall(r"\\w+", content_md))
    
    return {{
        "title": title,
        "content": content_md,
        "word_count": words,
        "char_count": len(content_md),
        "error": None
    }}
'''
        return code

    def extract(self, html: str) -> ExtractedChapter:
        """Execute extraction directly on HTML using the configured plan."""
        soup = BeautifulSoup(html, "lxml")
        
        # Remove clutter and aggregator noise
        all_remove = set(self.plan.remove_selectors or []) | set(DEFAULT_NOISE_SELECTORS)
        for sel in all_remove:
            try:
                for tag in soup.select(sel):
                    tag.decompose()
            except Exception:
                pass
                
        # Title extraction
        title = "Chapter"
        if self.plan.title_selector:
            try:
                title_el = soup.select_one(self.plan.title_selector)
                if title_el:
                    title = title_el.get_text(strip=True)
            except Exception:
                for fb in ["h1", "h2", ".chapter-title", ".title"]:
                    try:
                        title_el = soup.select_one(fb)
                        if title_el:
                            title = title_el.get_text(strip=True)
                            break
                    except Exception:
                        pass
        title = clean_chapter_title(title)
                
        # Content containers: select all matches and filter to top-level containers
        content_elements = []
        try:
            matched = soup.select(self.plan.content_selector)
            content_elements = [
                el for el in matched
                if not any(other is not el and other in el.parents for other in matched)
            ]
        except Exception:
            pass

        if not content_elements:
            for fb in ["div.p-novel__body", ".cha-words", ".chapter-content", "#chapter-content", "#novel_honbun", "#content", "article", "main"]:
                try:
                    el = soup.select_one(fb)
                    if el and len(el.get_text(strip=True)) > 200:
                        content_elements = [el]
                        break
                except Exception:
                    pass

        if not content_elements:
            return ExtractedChapter(
                title=title,
                content_markdown="",
                word_count=0,
                char_count=0,
                success=False,
                error=f"Content container '{self.plan.content_selector}' not found in page."
            )
            
        paragraphs: List[str] = []
        for container in content_elements:
            p_tags = container.find_all("p")
            if p_tags:
                for p in p_tags:
                    text = p.get_text(strip=True)
                    if text:
                        paragraphs.append(text)
            else:
                raw_text = container.get_text("\n", strip=True)
                for line in raw_text.split("\n"):
                    line = line.strip()
                    if line:
                        paragraphs.append(line)

        # Filter aggregator disclaimers and detect paywalls
        clean_paras, is_paywalled = clean_chapter_content(paragraphs)
        content_md = "\n\n".join(clean_paras)
        word_count = len(re.findall(r"\w+", content_md))
        char_count = len(content_md)

        # Fallback check: if extracted text is suspiciously short (< 150 chars), check if a known richer container exists on the page
        if char_count < 150 and not is_paywalled:
            for fb in ["div.p-novel__body", "#novel_honbun", ".entry-content", "article"]:
                try:
                    fb_el = soup.select_one(fb)
                    if fb_el:
                        fb_p_tags = fb_el.find_all("p")
                        fb_paras = []
                        if fb_p_tags:
                            for p in fb_p_tags:
                                t = p.get_text(strip=True)
                                if t:
                                    fb_paras.append(t)
                        else:
                            fb_paras = [l.strip() for l in fb_el.get_text("\n", strip=True).split("\n") if l.strip()]
                        fb_cleaned, fb_paywall = clean_chapter_content(fb_paras)
                        fb_md = "\n\n".join(fb_cleaned)
                        if len(fb_md) > char_count + 200:
                            content_md = fb_md
                            word_count = len(re.findall(r"\w+", content_md))
                            char_count = len(content_md)
                            is_paywalled = fb_paywall
                            break
                except Exception:
                    pass
        
        success = char_count > 50
        if is_paywalled and char_count <= 100:
            success = False
            error = "Chapter appears to be paywalled or premium teaser."
        else:
            error = None if success else "Extracted text content too short (< 50 chars)."
        
        return ExtractedChapter(
            title=title,
            content_markdown=content_md,
            word_count=word_count,
            char_count=char_count,
            success=success,
            error=error,
            is_paywalled=is_paywalled
        )

    def test_and_verify(self, sample_html: str) -> ParserVerificationResult:
        """Test extraction against sample chapter and produce preview."""
        result = self.extract(sample_html)
        code_str = self.generate_code_string()
        
        preview = ""
        if result.success:
            # Build preview snippet (e.g. title + first 5 paragraphs)
            paras = result.content_markdown.split("\n\n")
            preview_body = "\n\n".join(paras[:5])
            if len(paras) > 5:
                preview_body += f"\n\n*... [{len(paras) - 5} more paragraphs] ...*"
            preview = f"# {result.title}\n\n{preview_body}"
            
        return ParserVerificationResult(
            success=result.success,
            chapter_title=result.title,
            word_count=result.word_count,
            preview_markdown=preview or "Extraction yielded empty content.",
            generated_code=code_str,
            error=result.error
        )
