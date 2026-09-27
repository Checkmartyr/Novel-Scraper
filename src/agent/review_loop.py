import logging
from typing import Optional, Callable, List, Dict, Any
from src.agent.analyzer import ChapterAnalyzer, DOMStructurePlan
from src.agent.code_generator import ChapterCodeGenerator, ParserVerificationResult
from src.agent.observer import ExtractionObserver, ExtractionReview
from src.agent.llm_client import LLMClient
from src.config import MAX_REVIEW_ITERATIONS, MIN_QUALITY_SCORE

logger = logging.getLogger("agent.review_loop")

class ReviewLoopResult:
    """Encapsulates the final outcome of the Observer review & refinement loop."""
    def __init__(
        self,
        plan: DOMStructurePlan,
        verification: ParserVerificationResult,
        review: ExtractionReview,
        iterations: int,
        review_history: List[ExtractionReview],
        approved: bool,
        last_interaction_id: Optional[str] = None,
    ):
        self.plan = plan
        self.verification = verification
        self.review = review
        self.iterations = iterations
        self.review_history = review_history
        self.approved = approved
        self.last_interaction_id = last_interaction_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "iterations": self.iterations,
            "quality_score": self.review.quality_score,
            "is_accurate": self.review.is_accurate,
            "title_accurate": self.review.title_accurate,
            "has_clutter": self.review.has_clutter,
            "summary": self.review.summary,
            "issues": self.review.issues,
            "recommended_fixes": self.review.recommended_fixes,
        }

class ReviewLoopOrchestrator:
    """Orchestrates the Generator <-> Observer Review & Refinement loop."""

    def __init__(
        self,
        analyzer: Optional[ChapterAnalyzer] = None,
        observer: Optional[ExtractionObserver] = None,
        llm_client: Optional[LLMClient] = None,
        max_iterations: int = MAX_REVIEW_ITERATIONS,
    ):
        self.llm = llm_client or LLMClient()
        self.analyzer = analyzer or ChapterAnalyzer(self.llm)
        self.observer = observer or ExtractionObserver(self.llm)
        self.max_iterations = max_iterations

    async def run(
        self,
        html: str,
        url: str,
        on_progress: Optional[Callable[[int, int, DOMStructurePlan, ParserVerificationResult, ExtractionReview], None]] = None,
    ) -> ReviewLoopResult:
        """Run iterative extraction review and refinement loop until Observer approves."""
        # Check Domain Memory for cached chapter extraction plan
        try:
            from src.agent.domain_memory import domain_memory
            recipe = domain_memory.get_recipe(url)
            if recipe and recipe.chapter_config:
                saved_plan = recipe.chapter_config.to_plan()
                verification = domain_memory.test_chapter_recipe(recipe, html)
                if verification.success:
                    logger.info(f"[DomainMemory] Successfully verified saved chapter recipe for '{recipe.domain}' ({verification.word_count} words).")
                    saved_review = ExtractionReview(
                        quality_score=recipe.chapter_config.quality_score or 1.0,
                        is_accurate=True,
                        title_accurate=True,
                        has_clutter=False,
                        summary=f"Extraction verified with saved recipe for '{recipe.domain}'.",
                        issues=[],
                        recommended_fixes=[],
                    )
                    if on_progress:
                        try:
                            on_progress(1, 1, saved_plan, verification, saved_review)
                        except Exception:
                            pass
                    domain_memory.record_usage(recipe.domain)
                    return ReviewLoopResult(
                        plan=saved_plan,
                        verification=verification,
                        review=saved_review,
                        iterations=0,
                        review_history=[saved_review],
                        approved=True,
                    )
                else:
                    logger.warning(f"[DomainMemory] Saved chapter recipe for '{recipe.domain}' failed verification: {verification.error}. Refining via Observer.")
        except Exception as e:
            logger.debug(f"Domain memory chapter check exception: {e}")

        review_history: List[ExtractionReview] = []
        best_plan: Optional[DOMStructurePlan] = None
        best_verification: Optional[ParserVerificationResult] = None
        best_review: Optional[ExtractionReview] = None
        best_score = -1.0
        last_interaction_id: Optional[str] = None
        current_plan: Optional[DOMStructurePlan] = None

        for iteration in range(1, self.max_iterations + 1):
            logger.info(f"[Review Loop] Starting iteration {iteration}/{self.max_iterations} for {url}...")

            # 1. Generate or refine plan
            if iteration == 1 or current_plan is None:
                current_plan = await self.analyzer.analyze(html, url, previous_interaction_id=last_interaction_id)
            else:
                current_plan = await self.analyzer.refine(
                    html=html,
                    url=url,
                    current_plan=current_plan,
                    review=review_history[-1],
                    previous_interaction_id=last_interaction_id,
                )
            analyzer_llm = getattr(self.analyzer, "llm", None)
            if analyzer_llm and getattr(analyzer_llm, "last_interaction_id", None):
                last_interaction_id = analyzer_llm.last_interaction_id

            # 2. Synthesize & test parser
            generator = ChapterCodeGenerator(current_plan)
            verification = generator.test_and_verify(html)

            # 3. Observer reviews extraction
            review = await self.observer.review(
                url=url,
                html=html,
                plan=current_plan,
                verification=verification,
                previous_interaction_id=last_interaction_id,
            )
            observer_llm = getattr(self.observer, "llm", None)
            if observer_llm and getattr(observer_llm, "last_interaction_id", None):
                last_interaction_id = observer_llm.last_interaction_id
            review_history.append(review)

            logger.info(
                f"[Review Loop] Iteration {iteration} result: score={review.quality_score}, "
                f"accurate={review.is_accurate}, title_ok={review.title_accurate}, clutter={review.has_clutter}"
            )

            if on_progress:
                on_progress(iteration, self.max_iterations, current_plan, verification, review)

            # Track best result
            if review.quality_score > best_score:
                best_score = review.quality_score
                best_plan = current_plan
                best_verification = verification
                best_review = review

            # 4. Check approval condition
            if review.is_accurate and review.quality_score >= MIN_QUALITY_SCORE:
                logger.info(f"[Review Loop] Observer APPROVED extraction plan at iteration {iteration} (score={review.quality_score})!")
                return ReviewLoopResult(
                    plan=current_plan,
                    verification=verification,
                    review=review,
                    iterations=iteration,
                    review_history=review_history,
                    approved=True,
                    last_interaction_id=last_interaction_id,
                )

        logger.warning(
            f"[Review Loop] Max iterations ({self.max_iterations}) reached. Returning best plan (score={best_score})."
        )
        return ReviewLoopResult(
            plan=best_plan or current_plan,
            verification=best_verification or verification,
            review=best_review or review,
            iterations=self.max_iterations,
            review_history=review_history,
            approved=False,
            last_interaction_id=last_interaction_id,
        )
