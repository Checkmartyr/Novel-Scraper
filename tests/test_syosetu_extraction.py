import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.agent.analyzer import ChapterAnalyzer, DOMStructurePlan
from src.agent.code_generator import ChapterCodeGenerator
from src.agent.self_healer import SelfHealer
from src.agent.classifier import ChapterLink
from src.scraper.batch_runner import BatchScraperRunner

SYOSETU_SAMPLE_HTML = """<!DOCTYPE html>
<html lang="ja">
<head><title>打撃系鬼っ娘が征く配信道！ - 鬼と棍棒</title></head>
<body>
  <article class="p-novel">
    <div class="c-announce">Announcement header</div>
    <h1 class="p-novel__title p-novel__title--rensai">鬼と棍棒</h1>
    <div class="p-novel__body">
      <div class="js-novel-text p-novel__text p-novel__text--preface">
        <p id="Lp1">リンネ視点です。</p>
      </div>
      <div class="js-novel-text p-novel__text">
        <p id="L1">５匹の狼が、一人の少女を襲っている。</p>
        <p id="L2">正面から２匹が両腕を狙って噛み付かんと飛びかかる。</p>
        <p id="L3">その間に１匹は失敗に備えて背後へと回り、残り２匹は警戒するように周囲を抑える。</p>
        <p id="L4">少女は大きく右にステップすると、空中にいるせいで無防備な狼の背骨を砕くように右手の棍棒を叩きつける。</p>
        <p id="L5">まっすぐに飛んでくる棍棒は逃げる狼の脚に当たり、ポリゴン片へと還った。</p>
      </div>
      <div class="js-novel-text p-novel__text p-novel__text--afterword">
        <p id="La1">お読みいただきありがとうございました！次回もお楽しみに！</p>
      </div>
    </div>
    <div class="l-foot-contents">Footer clutter</div>
  </article>
</body>
</html>"""


def test_multi_container_extraction_parent_selector():
    """Verify that using parent container div.p-novel__body captures preface, body, and afterword."""
    plan = DOMStructurePlan(
        title_selector="h1.p-novel__title",
        content_selector="div.p-novel__body",
        remove_selectors=[".c-announce", ".l-foot-contents"],
        clean_paragraphs=True,
    )
    generator = ChapterCodeGenerator(plan)
    res = generator.extract(SYOSETU_SAMPLE_HTML)

    assert res.success is True
    assert res.title == "鬼と棍棒"
    assert "リンネ視点です。" in res.content_markdown
    assert "５匹の狼が、一人の少女を襲っている。" in res.content_markdown
    assert "お読みいただきありがとうございました！" in res.content_markdown


def test_multi_container_extraction_sub_selector():
    """Verify that when selector matches multiple elements (e.g. .p-novel__text), all sections are retained."""
    plan = DOMStructurePlan(
        title_selector="h1.p-novel__title",
        content_selector="div.p-novel__text",
        remove_selectors=[],
        clean_paragraphs=True,
    )
    generator = ChapterCodeGenerator(plan)
    res = generator.extract(SYOSETU_SAMPLE_HTML)

    assert res.success is True
    assert "リンネ視点です。" in res.content_markdown
    assert "５匹の狼が、一人の少女を襲っている。" in res.content_markdown
    assert "お読みいただきありがとうございました！" in res.content_markdown


def test_fallback_when_selector_matches_only_short_preface():
    """Verify that if content_selector only targets the preface (< 150 chars), fallback finds div.p-novel__body."""
    plan = DOMStructurePlan(
        title_selector="h1.p-novel__title",
        content_selector=".p-novel__text--preface",
        remove_selectors=[],
        clean_paragraphs=True,
    )
    generator = ChapterCodeGenerator(plan)
    res = generator.extract(SYOSETU_SAMPLE_HTML)

    assert res.success is True
    # Fallback should have kicked in to grab the full body
    assert "５匹の狼が、一人の少女を襲っている。" in res.content_markdown


def test_analyzer_heuristic_detects_syosetu_elements():
    """Verify heuristic analyzer correctly selects Syosetu title and content container."""
    analyzer = ChapterAnalyzer()
    plan = analyzer._heuristic_analyze(SYOSETU_SAMPLE_HTML)

    assert "p-novel__title" in plan.title_selector or plan.title_selector == "h1"
    assert plan.content_selector in ["div.p-novel__body", ".p-novel__body", "article", "article.p-novel"]


@pytest.mark.asyncio
async def test_self_healer_recovers_using_candidate_containers():
    """Verify SelfHealer fallback container recovery when initial extraction yields short text."""
    healer = SelfHealer()
    # Mock LLM to return a broken plan that only selects .p-novel__text--preface
    mock_llm = MagicMock()
    mock_llm.is_available = True
    broken_plan = DOMStructurePlan(
        title_selector="h1",
        content_selector=".p-novel__text--preface",
        remove_selectors=[],
        clean_paragraphs=True,
    )
    mock_llm.generate_json = AsyncMock(return_value=broken_plan)
    healer.llm = mock_llm
    healer.analyzer.llm = mock_llm

    # Mock observer to approve recovery
    mock_review = MagicMock()
    mock_review.is_accurate = True
    mock_review.quality_score = 1.0
    healer.observer.review = AsyncMock(return_value=mock_review)

    healed, healed_plan, healed_res = await healer.attempt_heal(
        url="https://ncode.syosetu.com/n9517fc/5/",
        html=SYOSETU_SAMPLE_HTML,
        current_plan=broken_plan,
    )

    assert healed is True
    assert healed_res is not None
    assert healed_res.success is True
    assert "５匹の狼が、一人の少女を襲っている。" in healed_res.content_markdown


@pytest.mark.asyncio
async def test_batch_runner_assigns_healed_result(tmp_path):
    """Verify BatchScraperRunner properly assigns healed_result and returns success."""
    broken_plan = DOMStructurePlan(
        title_selector="h1",
        content_selector=".non-existent-selector",
        remove_selectors=[],
        clean_paragraphs=True,
    )

    runner = BatchScraperRunner(
        novel_title="Test Novel",
        initial_plan=broken_plan,
        concurrency=1,
    )

    # Mock obscura to return SYOSETU_SAMPLE_HTML
    mock_obscura = MagicMock()
    mock_obscura.fetch_html = AsyncMock(return_value=SYOSETU_SAMPLE_HTML)
    runner.obscura = mock_obscura

    # Mock storage
    mock_storage = MagicMock()
    mock_storage.save_chapter = AsyncMock(return_value=tmp_path / "0005.md")
    runner.storage = mock_storage

    # Mock healer to succeed
    recovered_plan = DOMStructurePlan(
        title_selector="h1",
        content_selector="div.p-novel__body",
        remove_selectors=[],
        clean_paragraphs=True,
    )
    generator = ChapterCodeGenerator(recovered_plan)
    expected_healed_result = generator.extract(SYOSETU_SAMPLE_HTML)

    runner.healer.attempt_heal = AsyncMock(
        return_value=(True, recovered_plan, expected_healed_result)
    )

    semaphore = asyncio.Semaphore(1)
    link = ChapterLink(index=5, title="鬼と棍棒", url="https://ncode.syosetu.com/n9517fc/5/")
    success = await runner._scrape_single_chapter(link, semaphore)

    assert success is True
    # Verify save_chapter was called with the healed result containing the full text
    assert mock_storage.save_chapter.call_count == 1
    call_kwargs = mock_storage.save_chapter.call_args[1]
    saved_chapter = call_kwargs["chapter"]
    assert saved_chapter.success is True
    assert "５匹の狼が、一人の少女を襲っている。" in saved_chapter.content_markdown
