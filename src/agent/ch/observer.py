import re
from typing import List, Optional
from pydantic import BaseModel, Field

from src.agent.llm import LLMClient
from src.agent.instructions import agent_system_instruction
from src.agent.ch.analyzer import DOMStructurePlan
from src.agent.ch.code_generator import ParserVerificationResult
from src.config import MIN_QUALITY_SCORE
from src.core.page_analysis import PageAnalysis

class ExtractionReview(BaseModel):
    """Structured evaluation returned by the Quality Control Observer Agent."""
    is_accurate: bool = Field(description="True if the extracted text is clean story prose without clutter, and title is correct")
    quality_score: float = Field(ge=0, le=1, description="Quality rating from 0.0 (completely broken) to 1.0 (perfect)")
    title_accurate: bool = Field(default=True, description="Whether extracted chapter title is clean and accurate")
    has_clutter: bool = Field(default=False, description="Whether ads, nav, footer, or social widgets leaked into body")
    issues: List[str] = Field(default_factory=list, description="Specific defects found (e.g. 'contains nav buttons', 'wrong title')")
    recommended_fixes: List[str] = Field(default_factory=list, description="Concrete selector recommendations for title, content, or remove_selectors")
    summary: str = Field(default="", description="Brief assessment of the extraction quality")

class ExtractionObserver:
    """Observer / Critic Agent that inspects extracted sample chapters and verifies accuracy."""
    
    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm = llm_client or LLMClient()

    async def review(
        self,
        url: str,
        html: str,
        plan: DOMStructurePlan,
        verification: ParserVerificationResult,
        previous_interaction_id: Optional[str] = None,
        *, page_analysis=None,
    ) -> ExtractionReview:
        """Critique extracted sample chapter using Gemini Interactions API or heuristic fallback."""
        if not verification.success:
            return ExtractionReview(
                is_accurate=False,
                quality_score=0.0,
                title_accurate=False,
                has_clutter=False,
                issues=[f"Parser execution failed: {verification.error}"],
                recommended_fixes=["Re-evaluate content_selector to target a valid DOM element."],
                summary="Extraction failed completely."
            )

        if verification.coverage_issues:
            return ExtractionReview(is_accurate=False, quality_score=0.4,
                title_accurate=True, has_clutter=False, issues=verification.coverage_issues,
                recommended_fixes=[f"Preserve all prose and the ending from observed source region {verification.source_selector}"],
                summary="Independent source coverage failed")

        if self.llm.is_available:
            try:
                # Prepare text snippets
                preview = verification.preview_markdown
                head_snippet = preview[:1000]
                tail_snippet = preview[-1000:] if len(preview) > 1000 else ""
                middle_start = preview.find("[Middle]")
                middle_snippet = preview[middle_start:middle_start + 1000] if middle_start >= 0 else ""

                prompt = f"""
Source URL: {url}
Extracted Title: "{verification.chapter_title}"
Selectors Evaluated:
- Title Selector: {plan.title_selector}
- Content Selector: {plan.content_selector}
- Remove Selectors: {plan.remove_selectors}
Word Count: {verification.word_count}
Character Count: {verification.char_count}
Paragraph Count: {verification.paragraph_count}
Source page excerpt: {verification.source_excerpt}
Source container paragraph count: {verification.source_paragraph_count}
Source container character count: {verification.source_char_count}
Source container beginning: {verification.source_head}
Source container middle: {verification.source_middle}
Source container ending: {verification.source_tail}

Extracted Text Preview (Beginning):
\"\"\"
{head_snippet}
\"\"\"

Extracted Text Preview (Middle):
\"\"\"
{middle_snippet}
\"\"\"

Extracted Text Preview (Ending):
\"\"\"
{tail_snippet}
\"\"\"

"""
                review = await self.llm.generate_json(
                    prompt_text=prompt,
                    schema=ExtractionReview,
                    system_instruction=agent_system_instruction("observer"),
                    previous_interaction_id=previous_interaction_id,
                )
                return review
            except Exception:
                pass  # Fall back to heuristic

        return self._heuristic_review(html, plan, verification, page_analysis=page_analysis)

    def _heuristic_review(
        self,
        html: str,
        plan: DOMStructurePlan,
        verification: ParserVerificationResult,
        *, page_analysis=None,
    ) -> ExtractionReview:
        """Deterministic heuristic quality audit when LLM is unavailable."""
        issues: List[str] = []
        fixes: List[str] = []
        title_accurate = True
        has_clutter = False
        score = 1.0
        if verification.coverage_issues:
            issues.extend(verification.coverage_issues)
            fixes.append(f"Recover complete source region {verification.source_selector} without removing story prose")
            score -= 0.6

        title = verification.chapter_title.strip()
        preview = verification.preview_markdown
        soup = (page_analysis or PageAnalysis(html)).soup
        alternative_titles = [tag.get_text(" ", strip=True) for tag in soup.select("h1, h2, [class*=chapter-title], [class*=episode-title]")]
        if not re.search(r"chapter|episode|第.+[話章]|ตอน", title, re.I) and any(re.search(r"chapter|episode|第.+[話章]|ตอน", heading, re.I) for heading in alternative_titles if heading != title):
            title_accurate = False
            score -= 0.3
            issues.append("Selected title is a site heading instead of the observed chapter heading")

        # 1. Title checks
        if not title or title.lower() in ("chapter", "untitled", "title", "index", "home"):
            issues.append(f"Extracted title '{title}' is generic or empty.")
            fixes.append("Adjust title_selector to target a specific heading (e.g. h1, h2, or [class*='title']).")
            title_accurate = False
            score -= 0.3

        # 2. Clutter checks (common keywords in text)
        clutter_patterns = [
            (r"\b(?:next\s*chapter|previous\s*chapter|read\s*next|bookmark|table\s*of\s*contents)\b", "navigation links"),
            (r"\b(?:share\s*on|tweet|facebook|reddit|discord|patreon)\b", "social sharing widgets"),
            (r"(?:応援する|応援コメント|ハートをクリック|ログインが必要です)", "cheer/footer interactive widgets"),
            (r"(?:copyright|all\s*rights\s*reserved|dmca)", "copyright footer notice"),
        ]

        for pat, label in clutter_patterns:
            if re.search(pat, preview, re.IGNORECASE):
                issues.append(f"Extracted content contains residual {label}.")
                fixes.append(f"Add selectors targeting {label} to remove_selectors.")
                has_clutter = True
                score -= 0.2

        # 3. Word count check
        if verification.source_paragraph_count > verification.paragraph_count + 2 and verification.source_char_count > verification.char_count * 2.5:
            issues.append("Content selector misses adjacent source paragraphs")
            fixes.append("Select the complete story container rather than its first paragraphs")
            score -= 0.5
        if verification.char_count == 0 and verification.word_count == 0:
            issues.append(f"Word count ({verification.word_count}) is unusually low for a novel chapter.")
            fixes.append("Check if content_selector is too narrow or missing paragraphs.")
            score -= 0.3

        score = max(0.0, min(1.0, round(score, 2)))
        is_accurate = score >= MIN_QUALITY_SCORE and not has_clutter and title_accurate

        summary = (
            "Extraction verified clean and accurate."
            if is_accurate
            else f"Extraction needs refinement (Score: {score}). Issues: {', '.join(issues)}"
        )

        return ExtractionReview(
            is_accurate=is_accurate,
            quality_score=score,
            title_accurate=title_accurate,
            has_clutter=has_clutter,
            issues=issues,
            recommended_fixes=fixes,
            summary=summary,
        )
