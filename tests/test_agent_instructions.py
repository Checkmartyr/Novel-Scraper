"""Offline checks for agent instructions, grounding, and useful review work."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.ch.analyzer import ChapterAnalyzer, DOMStructurePlan
from src.agent.ch.code_generator import ChapterCodeGenerator
from src.agent.ch.observer import ExtractionObserver, ExtractionReview
from src.agent.ch.review_loop import ReviewLoopOrchestrator
from src.agent.ch.self_healer import SelfHealer
from src.agent.classifier import ChapterLink, ClassificationResult, PageClassifier
from src.config import MIN_QUALITY_SCORE


HTML = """<html><body><h1>Chapter 7: Returning Home</h1>
<div id="chapter-content"><p>The traveller reached home after a long journey.
The old gate was open and the garden was full of flowers.
She remembered the voices of her family as she crossed the courtyard.
Inside, a small lamp was still burning beside the window.</p>
<p>She whispered <em>at last</em>, and closed the door behind her.</p>
<p>The light stayed on until dawn.</p></div><footer>Copyright</footer></body></html>"""
URL = "https://fixture.test/novel/home/chapter-7"


def plan(**changes):
    return DOMStructurePlan(title_selector="h1", content_selector="#chapter-content", **changes)


class Model:
    is_available = True
    last_interaction_id = None

    def __init__(self, *results):
        self.results = iter(results)
        self.calls = []

    async def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.results)


@pytest.mark.asyncio
async def test_analysis_and_repair_receive_active_skills_and_current_dom():
    model = Model(plan(), plan(remove_selectors=["footer"]))
    analyzer = ChapterAnalyzer(model)
    current = await analyzer.analyze(HTML, URL)
    review = ExtractionReview(is_accurate=False, quality_score=0.7,
                             issues=["Footer was included"], recommended_fixes=["Remove footer"])
    updated = await analyzer.refine(HTML, URL, current, review)

    assert len(model.calls) == 2
    assert "Role: chapter_analyzer" in model.calls[0]["system_instruction"]
    assert "untrusted evidence" in model.calls[0]["system_instruction"]
    assert "Role: chapter_refiner" in model.calls[1]["system_instruction"]
    assert "Role: observer" not in model.calls[1]["system_instruction"]
    assert "div#chapter-content" in model.calls[1]["prompt_text"]
    assert URL in model.calls[1]["prompt_text"]
    assert "Footer was included" in model.calls[1]["prompt_text"]
    assert updated.remove_selectors == ["footer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_plan", [
    DOMStructurePlan(title_selector="h1", content_selector="#invented"),
    plan(remove_selectors=["body"]),
    plan(remove_selectors=["#chapter-content"]),
    plan(remove_selectors=["h1"]),
])
async def test_model_selector_errors_fall_back_to_a_usable_parser(bad_plan):
    model = Model(bad_plan)
    selected = await ChapterAnalyzer(model).analyze(HTML, URL)
    extracted = ChapterCodeGenerator(selected).extract(HTML)

    assert extracted.success
    assert extracted.title == "Chapter 7: Returning Home"
    assert extracted.content_markdown.endswith("The light stayed on until dawn.")
    assert "#chapter-content" not in selected.remove_selectors


@pytest.mark.asyncio
async def test_invalid_refinement_cannot_remove_the_valid_story_container():
    model = Model(plan(remove_selectors=["#chapter-content"]))
    current = plan()
    review = ExtractionReview(is_accurate=False, quality_score=0.5,
                             recommended_fixes=["Select #chapter-content to include the ending"])

    updated = await ChapterAnalyzer(model).refine(HTML, URL, current, review)

    assert updated == current
    assert ChapterCodeGenerator(updated).extract(HTML).success


@pytest.mark.asyncio
async def test_classifier_discards_invented_links_and_model_coverage_claims():
    source = "https://fixture.test/novel/home"
    html = '<h1>Homecoming</h1><a href="/novel/home/chapter-7">Chapter 7</a>'
    model = Model(ClassificationResult(
        page_type="TOC", novel_title="Homecoming",
        chapter_links=[
            ChapterLink(index=1, title="Chapter 7", url="/novel/home/chapter-7"),
            ChapterLink(index=2, title="Chapter 8", url="/novel/home/chapter-8"),
        ],
        next_chapter_url="/novel/home/chapter-8",
        toc_state={"is_complete": True},
    ))
    toc = SimpleNamespace(extract_toc=AsyncMock(return_value=None))

    result = await PageClassifier(llm_client=model, toc_agent=toc).classify(source, html)

    assert [link.url for link in result.chapter_links] == [URL]
    assert result.next_chapter_url is None
    assert result.toc_state is None
    assert "Role: classifier" in model.calls[0]["system_instruction"]


@pytest.mark.asyncio
async def test_observer_receives_middle_ending_and_configured_approval_rules():
    current = plan()
    verification = ChapterCodeGenerator(current).test_and_verify(HTML)
    model = Model(ExtractionReview(is_accurate=True, quality_score=1))

    await ExtractionObserver(model).review(URL, HTML, current, verification)

    request = model.calls[0]
    assert "Role: observer" in request["system_instruction"]
    assert f"quality_score >= {MIN_QUALITY_SCORE}" in request["system_instruction"]
    assert "Extracted Text Preview (Middle)" in request["prompt_text"]
    assert "The light stayed on until dawn." in request["prompt_text"]


@pytest.mark.asyncio
async def test_repeated_rejected_plan_stops_before_another_parser_review():
    current = plan(remove_selectors=["footer", "nav"])
    repeated = plan(remove_selectors=["nav", "footer"])
    analyzer = SimpleNamespace(
        analyze=AsyncMock(return_value=current), refine=AsyncMock(return_value=repeated),
    )
    observer = SimpleNamespace(review=AsyncMock(return_value=ExtractionReview(
        is_accurate=False, quality_score=0.6, issues=["Coverage remains unresolved"],
    )))

    result = await ReviewLoopOrchestrator(
        analyzer=analyzer, observer=observer, max_iterations=6,
        llm_client=SimpleNamespace(is_available=True),
    ).run(HTML, URL)

    assert result.approved is False
    assert result.iterations == 1
    assert len(result.review_history) == 1
    assert result.plan.content_selector == current.content_selector
    analyzer.analyze.assert_awaited_once()
    analyzer.refine.assert_not_awaited()
    observer.review.assert_awaited_once()


@pytest.mark.asyncio
async def test_healer_respects_the_configured_threshold(monkeypatch):
    monkeypatch.setattr("src.agent.ch.self_healer.MIN_QUALITY_SCORE", 0.95)
    current = plan()
    analyzer = SimpleNamespace(
        analyze=AsyncMock(return_value=current), refine=AsyncMock(return_value=current),
    )
    observer = SimpleNamespace(review=AsyncMock(return_value=ExtractionReview(
        is_accurate=True, quality_score=0.9,
    )))

    recovered, retained, extracted = await SelfHealer(
        analyzer=analyzer, observer=observer,
    ).attempt_heal(URL, HTML, current)

    assert recovered is False
    assert retained == current
    assert extracted is None
