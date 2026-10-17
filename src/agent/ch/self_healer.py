import logging
from typing import Optional, Tuple
from src.agent.ch.analyzer import ChapterAnalyzer, DOMStructurePlan
from src.agent.ch.code_generator import ChapterCodeGenerator, ExtractedChapter
from src.agent.ch.observer import ExtractionObserver
from src.agent.llm import LLMClient
from src.config import MIN_QUALITY_SCORE

logger = logging.getLogger("agent.self_healer")

class SelfHealer:
    """Self-healing engine triggered when a chapter layout changes or extraction fails."""
    
    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        observer: Optional[ExtractionObserver] = None,
        analyzer: Optional[ChapterAnalyzer] = None,
    ):
        self.llm = llm_client or LLMClient()
        self.analyzer = analyzer or ChapterAnalyzer(self.llm)
        self.observer = observer or ExtractionObserver(self.llm)

    async def attempt_heal(
        self,
        url: str,
        html: str,
        current_plan: DOMStructurePlan,
        previous_interaction_id: Optional[str] = None,
    ) -> Tuple[bool, DOMStructurePlan, Optional[ExtractedChapter]]:
        """Re-analyze broken chapter DOM, patch selectors, and verify recovery using Observer review."""
        from src.agent.ch.review_loop import ReviewLoopOrchestrator
        from src.scraper.telemetry import stage_enabled
        if not stage_enabled("repair"):
            return False, current_plan, None
        try:
            reviewed = await ReviewLoopOrchestrator(analyzer=self.analyzer, observer=self.observer,
                llm_client=self.llm).run(html, url)
            if (reviewed.approved and reviewed.verification.success and not reviewed.verification.coverage_issues
                    and reviewed.review.quality_score >= MIN_QUALITY_SCORE):
                extracted = ChapterCodeGenerator(reviewed.plan).extract(html)
                if extracted.success and not extracted.is_paywalled:
                    return True, reviewed.plan, extracted
        except Exception as error:
            logger.warning("Chapter repair failed: %s", error)
        return False, current_plan, None
