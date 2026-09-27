import logging
from typing import Optional, Tuple
from src.agent.analyzer import ChapterAnalyzer, DOMStructurePlan
from src.agent.code_generator import ChapterCodeGenerator, ExtractedChapter
from src.agent.observer import ExtractionObserver
from src.agent.llm_client import LLMClient

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
        logger.warning(f"Initiating self-healing protocol for {url} (previous_interaction_id={previous_interaction_id})...")
        
        try:
            # 1. Re-analyze DOM structure for the failed page with previous interaction context
            new_plan = await self.analyzer.analyze(html, url, previous_interaction_id=previous_interaction_id)
            
            # 2. Test extraction using new plan
            new_generator = ChapterCodeGenerator(new_plan)
            verification = new_generator.test_and_verify(html)
            result = new_generator.extract(html)
            
            if not (verification.success and result.success):
                # Fallback recovery: try candidate parent containers or prominent novel containers
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "lxml")
                candidate_fallbacks = ["div.p-novel__body", ".p-novel__body", "#novel_honbun", "#chapter-content", "article", "main"]
                # Also check parent of the current content selector
                try:
                    curr_el = soup.select_one(new_plan.content_selector)
                    if curr_el and curr_el.parent and curr_el.parent.name in ["div", "article", "section", "main"]:
                        parent_classes = curr_el.parent.get("class", [])
                        if parent_classes:
                            candidate_fallbacks.insert(0, f"{curr_el.parent.name}.{'.'.join(parent_classes)}")
                        elif curr_el.parent.get("id"):
                            candidate_fallbacks.insert(0, f"#{curr_el.parent['id']}")
                except Exception:
                    pass

                for cand in candidate_fallbacks:
                    try:
                        test_plan = new_plan.model_copy(deep=True)
                        test_plan.content_selector = cand
                        test_gen = ChapterCodeGenerator(test_plan)
                        test_res = test_gen.extract(html)
                        if test_res.success and test_res.char_count > 200:
                            new_plan = test_plan
                            new_generator = test_gen
                            verification = new_generator.test_and_verify(html)
                            result = test_res
                            logger.info(f"Self-healer recovered using candidate container '{cand}' ({result.char_count} chars)!")
                            break
                    except Exception:
                        continue

            if verification.success and result.success:
                # 3. Quality Control: Observer review
                review = await self.observer.review(
                    url=url,
                    html=html,
                    plan=new_plan,
                    verification=verification,
                    previous_interaction_id=previous_interaction_id,
                )
                
                if review.is_accurate:
                    logger.info(f"Self-healing succeeded & observer-verified (score={review.quality_score:.2f}) for {url}!")
                    return True, new_plan, result
                else:
                    logger.warning(
                        f"Self-healing plan critique rejected by observer (score={review.quality_score:.2f}): "
                        f"{review.issues}. Attempting observer-guided refine..."
                    )
                    refined_plan = await self.analyzer.refine(
                        html=html,
                        url=url,
                        current_plan=new_plan,
                        review=review,
                        previous_interaction_id=previous_interaction_id,
                    )
                    refined_gen = ChapterCodeGenerator(refined_plan)
                    refined_res = refined_gen.extract(html)
                    if refined_res.success:
                        logger.info(f"Self-healing refinement succeeded for {url}!")
                        return True, refined_plan, refined_res
                    else:
                        logger.error(f"Self-healing refinement failed: {refined_res.error}")
                        return False, current_plan, None
            else:
                logger.error(f"Self-healing failed: new plan produced invalid output ({result.error})")
                return False, current_plan, None
        except Exception as e:
            logger.error(f"Self-healing encountered exception: {e}")
            return False, current_plan, None

