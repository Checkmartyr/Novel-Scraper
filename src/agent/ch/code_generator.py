import re
from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from src.agent.ch.analyzer import DOMStructurePlan
from src.utils.title_cleaner import clean_chapter_title, clean_chapter_content

DEFAULT_NOISE_SELECTORS = [
    "script", "style", "noscript", "rt", "rp",
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
    source_excerpt: str = ""
    paragraph_count: int = 0
    char_count: int = 0
    source_head: str = ""
    source_middle: str = ""
    source_tail: str = ""
    source_char_count: int = 0
    source_paragraph_count: int = 0
    is_paywalled: bool = False


def extract_paragraphs(container):
    """Preserve mixed direct prose, inline markup, paragraph blocks, and line breaks."""
    paragraphs, current = [], []
    def flush():
        text = "".join(current).strip()
        if text:
            paragraphs.append(text)
        current.clear()
    def walk(node):
        for child in node.children:
            name = getattr(child, "name", None)
            if name is None:
                current.append(str(child))
            elif name == "br":
                flush()
            elif name in {"p", "div", "section", "article", "main", "blockquote", "li"}:
                flush()
                walk(child)
                flush()
            elif name not in {"script", "style", "rt", "rp", "h1", "h2"}:
                current.append(child.get_text("", strip=False))
    walk(container)
    flush()
    return paragraphs

class ChapterCodeGenerator:
    """Generates and executes deterministic Python extraction code from DOM plan."""
    
    def __init__(self, plan: DOMStructurePlan):
        self.plan = plan

    def generate_code_string(self) -> str:
        """Generate standalone Python scraper function code."""
        removes_repr = repr(sorted(set(self.plan.remove_selectors) | set(DEFAULT_NOISE_SELECTORS)))
        code = f'''from bs4 import BeautifulSoup
import re

def extract_paragraphs(container):
    paragraphs, current = [], []
    def flush():
        text = "".join(current).strip()
        if text:
            paragraphs.append(text)
        current.clear()
    def walk(node):
        for child in node.children:
            name = getattr(child, "name", None)
            if name is None:
                current.append(str(child))
            elif name == "br":
                flush()
            elif name in {{"p", "div", "section", "article", "main", "blockquote", "li"}}:
                flush()
                walk(child)
                flush()
            elif name not in {{"script", "style", "rt", "rp", "h1", "h2"}}:
                current.append(child.get_text("", strip=False))
    walk(container)
    flush()
    return paragraphs

def extract_chapter(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    
    # Remove clutter elements
    remove_selectors = {removes_repr}
    for sel in remove_selectors:
        for tag in soup.select(sel):
            tag.decompose()
            
    # Extract Title
    title = ""
    title_el = soup.select_one({self.plan.title_selector!r})
    if title_el:
        title = title_el.get_text(strip=True)
        
    # Extract Content
    content_elements = soup.select({self.plan.content_selector!r})
    top_elements = [
        el for el in content_elements
        if not any(other is not el and other in el.parents for other in content_elements)
    ]
    if not top_elements:
        return {{"title": title, "content": "", "word_count": 0, "error": "Content container not found"}}
        
    # Process paragraphs into Markdown
    paragraphs = []
    for container in top_elements:
        paragraphs.extend(extract_paragraphs(container))
                
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
        locked = any(not node.get("hidden") and node.get("aria-hidden") != "true"
                     and "display:none" not in node.get("style", "").replace(" ", "")
                     and "hidden" not in node.get("class", [])
                     for node in soup.select(".chapter-locked, .paywall-lock, .unlock-chapter"))
        
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
            paragraphs.extend(extract_paragraphs(container))

        # Filter aggregator disclaimers and detect paywalls
        clean_paras, is_paywalled = clean_chapter_content(paragraphs)
        is_paywalled = is_paywalled or locked
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
        
        success = char_count > 0
        if is_paywalled and char_count <= 100:
            success = False
            error = "Chapter appears to be paywalled or premium teaser."
        else:
            error = None if success else "Extracted text content is empty."
        if not success and any(container.select_one("canvas, img") for container in content_elements):
            error = "Unsupported image/canvas chapter rendering; OCR is outside this version"
        
        return ExtractedChapter(
            title=title,
            content_markdown=content_md,
            word_count=word_count,
            char_count=char_count,
            success=success,
            error=error,
            is_paywalled=is_paywalled
        )

    def test_and_verify(self, sample_html: str, result: Optional[ExtractedChapter] = None) -> ParserVerificationResult:
        """Test extraction against sample chapter and produce preview."""
        result = result or self.extract(sample_html)
        code_str = self.generate_code_string()
        source = BeautifulSoup(sample_html, "lxml")
        for selector in set(self.plan.remove_selectors) | set(DEFAULT_NOISE_SELECTORS):
            try:
                for element in source.select(selector):
                    element.decompose()
            except Exception:
                continue
        try:
            containers = source.select(self.plan.content_selector)
        except Exception:
            containers = []
        context = []
        for container in containers:
            parent = container.parent
            if parent and parent.name in {"div", "article", "section", "main"} and len(parent.find_all("p")) > len(container.find_all("p")) + 2:
                container = parent
            if container not in context:
                context.append(container)
        source_paragraphs = [paragraph for container in context for paragraph in extract_paragraphs(container)]
        source_text = "\n\n".join(source_paragraphs)
        midpoint = max(0, len(source_text)//2 - 500)
        
        preview = ""
        if result.success:
            # Build preview snippet (e.g. title + first 5 paragraphs)
            paras = result.content_markdown.split("\n\n")
            middle = max(0, len(paras) // 2 - 1)
            preview_body = "\n\n".join([
                "[Beginning]", *paras[:3], "[Middle]", *paras[middle:middle + 3],
                "[Ending]", *paras[-3:],
            ])
            if len(paras) > 5:
                preview_body += f"\n\n*... [{len(paras) - 5} more paragraphs] ...*"
            preview = f"# {result.title}\n\n{preview_body}"
            
        return ParserVerificationResult(
            success=result.success and not result.is_paywalled,
            chapter_title=result.title,
            word_count=result.word_count,
            preview_markdown=preview or "Extraction yielded empty content.",
            generated_code=code_str,
            error="Chapter is locked or contains a premium teaser" if result.is_paywalled else result.error,
            source_excerpt=BeautifulSoup(sample_html, "lxml").get_text(" ", strip=True)[:1200],
            paragraph_count=len(result.content_markdown.split("\n\n")),
            char_count=result.char_count,
            source_head=source_text[:1000],
            source_middle=source_text[midpoint:midpoint + 1000],
            source_tail=source_text[-1000:],
            source_char_count=len(source_text),
            source_paragraph_count=len(source_paragraphs),
            is_paywalled=result.is_paywalled,
        )
