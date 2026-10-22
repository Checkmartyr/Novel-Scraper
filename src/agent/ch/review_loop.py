import logging
from bs4 import BeautifulSoup
from typing import Optional, Callable, List, Dict, Any
from src.agent.ch.analyzer import ChapterAnalyzer, DOMStructurePlan
from src.agent.ch.code_generator import ChapterCodeGenerator, ParserVerificationResult
from src.agent.ch.observer import ExtractionObserver, ExtractionReview
from src.agent.llm import LLMClient
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
        from src.scraper.telemetry import stage, mark_validated
        # Check Domain Memory for cached chapter extraction plan
        try:
            from src.agent.domain_memory import domain_memory
            recipe = domain_memory.get_chapter_recipe(url, html)
            if recipe and recipe.chapter_config:
                saved_plan = recipe.chapter_config.to_plan()
                verification = domain_memory.test_chapter_recipe(recipe, html)
                deterministic_review = self.observer._heuristic_review(html, saved_plan, verification)
                selector_present = bool(BeautifulSoup(html, "lxml").select(saved_plan.content_selector))
                if selector_present and verification.success and deterministic_review.is_accurate and recipe.chapter_config.quality_score >= MIN_QUALITY_SCORE:
                    with stage("recipe") as measurement:
                        mark_validated(measurement, ["chapter:" + url])
                        measurement.update(outcome="approved", reason="Saved recipe independently verified")
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

        from src.scraper.telemetry import stage, stage_enabled
        from src.scraper.runtime import ScraperLimits
        from src.agent.domain_memory import domain_memory
        remembered = domain_memory.get_recipe(url)
        signature = getattr(getattr(remembered, "chapter_config", None), "structural_signature", None)
        deterministic = ChapterAnalyzer(self.llm)
        history, seen_plans, seen_outputs = [], set(), set()
        best = None
        last_interaction_id = None

        async def evaluate(plan, phase):
            nonlocal best
            key = (plan.title_selector, plan.content_selector, tuple(sorted(set(plan.remove_selectors))), plan.clean_paragraphs)
            if key in seen_plans:
                return None
            seen_plans.add(key)
            with stage(phase) as measurement:
                verification = ChapterCodeGenerator(plan).test_and_verify(html)
                output_key = (verification.chapter_title, verification.preview_markdown, tuple(verification.coverage_issues))
                if output_key in seen_outputs:
                    measurement.update(outcome="skipped", reason="Equivalent extraction already audited")
                    return None
                seen_outputs.add(output_key)
                if hasattr(self.observer, "_heuristic_review"):
                    review = self.observer._heuristic_review(html, plan, verification)
                    if verification.coverage_issues:
                        review = ExtractionReview(is_accurate=False, quality_score=0.4, issues=verification.coverage_issues)
                    elif phase in {"model_selection", "contextual_repair"} and verification.success and not review.is_accurate:
                        review = await self.observer.review(url, html, plan, verification,
                            previous_interaction_id=last_interaction_id)
                else:
                    review = await self.observer.review(url, html, plan, verification)
                approved = bool(verification.success and not verification.coverage_issues and not verification.is_paywalled
                    and review.is_accurate and review.title_accurate and not review.has_clutter
                    and review.quality_score >= MIN_QUALITY_SCORE)
                history.append(review)
                measurement.update(outcome="approved" if approved else "rejected",
                    reason="Verified chapter boundaries" if approved else "; ".join(review.issues))
                if approved:
                    mark_validated(measurement, ["chapter:" + url])
                result = ReviewLoopResult(plan, verification, review, len(history), list(history), approved, last_interaction_id)
                if best is None or (review.quality_score > best.review.quality_score):
                    best = result
                if on_progress:
                    on_progress(len(history), self.max_iterations, plan, verification, review)
                return result

        if stage_enabled("deterministic"):
            plans = [deterministic._heuristic_analyze(html), *deterministic.candidate_plans(html)]
            for plan in plans[:ScraperLimits.from_env().candidate_limit]:
                result = await evaluate(plan, "deterministic_candidates")
                if result and (result.approved or result.verification.is_paywalled):
                    return result

        if stage_enabled("model") and self.llm.is_available:
            current_plan = None
            for _ in range(self.max_iterations):
                if current_plan is None:
                    proposed = await self.analyzer.analyze(html, url, previous_interaction_id=last_interaction_id)
                else:
                    proposed = await self.analyzer.refine(html=html, url=url, current_plan=current_plan,
                        review=history[-1], previous_interaction_id=last_interaction_id)
                last_interaction_id = getattr(getattr(self.analyzer, "llm", None), "last_interaction_id", None)
                result = await evaluate(proposed, "model_selection")
                if result is None:
                    break
                current_plan = proposed
                if result.approved or result.verification.is_paywalled:
                    return result

        if signature and stage_enabled("repair"):
            for plan in deterministic.candidate_plans(html, signature)[:2]:
                result = await evaluate(plan, "contextual_repair")
                if result and result.approved:
                    return result

        if best is None:
            plan = deterministic._heuristic_analyze(html)
            verification = ChapterCodeGenerator(plan).test_and_verify(html)
            review = ExtractionReview(is_accurate=False, quality_score=0,
                issues=["Recovery ceiling or exhausted candidates prevented chapter approval"])
            best = ReviewLoopResult(plan, verification, review, 0, [], False)
        best.iterations, best.review_history = len(history), history
        return best
