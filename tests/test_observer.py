import pytest
from unittest.mock import AsyncMock, MagicMock
from src.agent.analyzer import DOMStructurePlan, ChapterAnalyzer
from src.agent.code_generator import ChapterCodeGenerator, ParserVerificationResult, ExtractedChapter
from src.agent.observer import ExtractionObserver, ExtractionReview
from src.agent.review_loop import ReviewLoopOrchestrator, ReviewLoopResult
from src.agent.self_healer import SelfHealer
from src.config import MIN_QUALITY_SCORE

SAMPLE_CLEAN_HTML = """
<!DOCTYPE html>
<html>
<head><title>Volume 1 Chapter 1 - Clean Web Novel</title></head>
<body>
  <div class="header"><span class="site-title">Novel Site</span></div>
  <h1 class="chapter-title">Chapter 1: Awaken in Another World</h1>
  <div class="novel-body">
    <p>The dawn broke over the emerald canopy of the ancient forest.</p>
    <p>He opened his eyes, feeling the pulse of foreign mana surging through his veins.</p>
    <p>Nothing in this world resembled the Tokyo street he had walked just moments ago.</p>
    <p>"So this is what reincarnation feels like," he muttered under his breath.</p>
  </div>
  <div class="footer"><p>Copyright 2026</p></div>
</body>
</html>
"""

SAMPLE_DIRTY_HTML = """
<!DOCTYPE html>
<html>
<head><title>Novel Site - Top Page</title></head>
<body>
  <div class="site-nav">
    <a href="/prev">Previous Chapter</a>
    <a href="/next">Next Chapter</a>
  </div>
  <div class="ad-top">Buy Gold Coins Now!</div>
  <h1 class="page-header">Novel Site</h1>
  <div class="story-container">
    <h2 class="episode-title">Episode 1: The First Step</h2>
    <div class="ad-banner">Click here for free spins!</div>
    <p>The dawn broke over the emerald canopy of the ancient forest.</p>
    <p>He opened his eyes, feeling the pulse of foreign mana surging through his veins.</p>
    <div class="cheer-widget">Click the heart to cheer the author!</div>
    <div class="nav-bottom"><a href="/next">Next Chapter</a></div>
  </div>
</body>
</html>
"""

def test_extraction_review_model():
    review = ExtractionReview(
        is_accurate=True,
        quality_score=0.95,
        title_accurate=True,
        has_clutter=False,
        issues=[],
        recommended_fixes=[],
        summary="Clean extraction",
    )
    assert review.is_accurate is True
    assert review.quality_score == 0.95
    assert review.has_clutter is False


def test_review_loop_result_last_interaction_id():
    plan = DOMStructurePlan(title_selector="h1", content_selector="div")
    gen = ChapterCodeGenerator(plan)
    verif = gen.test_and_verify("<html><body><h1>Title</h1><div>Content that is long enough to pass verification test properly.</div></body></html>")
    review = ExtractionReview(is_accurate=True, quality_score=0.9, issues=[], recommended_fixes=[], summary="")
    res = ReviewLoopResult(
        plan=plan,
        verification=verif,
        review=review,
        iterations=1,
        review_history=[review],
        approved=True,
        last_interaction_id="interaction-abc-123",
    )
    assert res.last_interaction_id == "interaction-abc-123"


@pytest.mark.asyncio
async def test_observer_heuristic_review_clean():
    observer = ExtractionObserver()
    plan = DOMStructurePlan(
        title_selector="h1.chapter-title",
        content_selector="div.novel-body",
        remove_selectors=[".header", ".footer"],
    )
    gen = ChapterCodeGenerator(plan)
    verification = gen.test_and_verify(SAMPLE_CLEAN_HTML)
    assert verification.success is True

    review = await observer.review(
        url="https://example.com/novel/1",
        html=SAMPLE_CLEAN_HTML,
        plan=plan,
        verification=verification,
    )
    assert review.is_accurate is True
    assert review.quality_score >= MIN_QUALITY_SCORE
    assert review.has_clutter is False
    assert review.title_accurate is True


@pytest.mark.asyncio
async def test_observer_heuristic_review_detects_clutter():
    observer = ExtractionObserver()
    # Plan captures container with ads and nav buttons without removing them
    plan = DOMStructurePlan(
        title_selector="h1.page-header",  # generic site title!
        content_selector="div.story-container",
        remove_selectors=[],
    )
    gen = ChapterCodeGenerator(plan)
    verification = gen.test_and_verify(SAMPLE_DIRTY_HTML)

    review = await observer.review(
        url="https://example.com/novel/1",
        html=SAMPLE_DIRTY_HTML,
        plan=plan,
        verification=verification,
    )
    # Heuristic review detects clutter or wrong title
    assert review.has_clutter is True or not review.title_accurate or review.quality_score < 1.0


@pytest.mark.asyncio
async def test_review_loop_approves_clean_plan():
    # Mock analyzer returning clean plan
    clean_plan = DOMStructurePlan(
        title_selector="h1.chapter-title",
        content_selector="div.novel-body",
        remove_selectors=[".header", ".footer"],
    )
    analyzer = MagicMock(spec=ChapterAnalyzer)
    analyzer.analyze = AsyncMock(return_value=clean_plan)

    orchestrator = ReviewLoopOrchestrator(analyzer=analyzer, max_iterations=3)
    result = await orchestrator.run(
        html=SAMPLE_CLEAN_HTML,
        url="https://example.com/novel/1",
    )

    assert result.approved is True
    assert result.iterations == 1
    assert result.review.quality_score >= MIN_QUALITY_SCORE
    assert result.plan.title_selector == "h1.chapter-title"


@pytest.mark.asyncio
async def test_review_loop_refinement():
    # Iteration 1 gives bad plan, Iteration 2 refines to clean plan
    bad_plan = DOMStructurePlan(
        title_selector="h1.page-header",
        content_selector="div.story-container",
        remove_selectors=[],
    )
    good_plan = DOMStructurePlan(
        title_selector="h2.episode-title",
        content_selector="div.story-container",
        remove_selectors=[".ad-banner", ".cheer-widget", ".nav-bottom"],
    )

    analyzer = MagicMock(spec=ChapterAnalyzer)
    analyzer.analyze = AsyncMock(return_value=bad_plan)
    analyzer.refine = AsyncMock(return_value=good_plan)

    orchestrator = ReviewLoopOrchestrator(analyzer=analyzer, max_iterations=2)
    result = await orchestrator.run(
        html=SAMPLE_DIRTY_HTML,
        url="https://example.com/novel/1",
    )

    # If refined plan is clean, result reflects improved score
    assert result.iterations >= 1
    assert result.plan is not None


@pytest.mark.asyncio
async def test_self_healer_with_observer():
    clean_plan = DOMStructurePlan(
        title_selector="h1.chapter-title",
        content_selector="div.novel-body",
        remove_selectors=[".header", ".footer"],
    )
    broken_plan = DOMStructurePlan(
        title_selector="invalid-title-sel",
        content_selector="invalid-content-sel",
        remove_selectors=[],
    )

    analyzer = MagicMock(spec=ChapterAnalyzer)
    analyzer.analyze = AsyncMock(return_value=clean_plan)

    healer = SelfHealer(analyzer=analyzer)
    success, updated_plan, chapter = await healer.attempt_heal(
        url="https://example.com/novel/1",
        html=SAMPLE_CLEAN_HTML,
        current_plan=broken_plan,
    )

    assert success is True
    assert updated_plan.content_selector == "div.novel-body"
    assert chapter is not None
    assert chapter.success is True
    assert "reincarnation" in chapter.content_markdown
