import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.agent.analyzer import DOMStructurePlan
from src.agent.classifier import ChapterLink
from src.agent.code_generator import ExtractedChapter
from src.agent.domain_memory import DomainMemoryManager
from src.agent.toc.state import TocState
from src.agent.toc.graph import TocGraphWorkflow
from src.agent.toc.tools import TocSynthesizer
from src.scraper.batch_runner import BatchScraperRunner


def test_update_chapter_plan(tmp_path):
    manager = DomainMemoryManager(recipes_dir=tmp_path)
    initial_plan = DOMStructurePlan(
        title_selector="h1.orig-title",
        content_selector="div.orig-content",
    )
    recipe = manager.save_recipe(
        domain_or_url="https://adaptivenovel.org/book/1",
        sample_toc_url="https://adaptivenovel.org/book/1",
        sample_chapter_url="https://adaptivenovel.org/book/1/ch1",
        toc_strategy="dom_heuristic",
        chapter_plan=initial_plan,
    )
    assert recipe.chapter_config.title_selector == "h1.orig-title"

    # Now self-healer finds a better selector
    updated_plan = DOMStructurePlan(
        title_selector="h1.healed-title",
        content_selector="div.healed-content",
        remove_selectors=[".healed-ad"],
    )
    updated_recipe = manager.update_chapter_plan(
        domain_or_url="https://adaptivenovel.org/book/1/ch2",
        plan=updated_plan,
        sample_url="https://adaptivenovel.org/book/1/ch2",
    )
    assert updated_recipe.chapter_config.title_selector == "h1.healed-title"
    assert updated_recipe.chapter_config.content_selector == "div.healed-content"
    assert updated_recipe.chapter_config.remove_selectors == [".healed-ad"]

    # Verify persisted script contains updated selectors
    script_path = tmp_path / "adaptivenovel.org.py"
    assert script_path.exists()
    script_text = script_path.read_text(encoding="utf-8")
    assert "healed-title" in script_text
    assert "healed-content" in script_text
    assert "--batch" in script_text


def test_update_toc_plan(tmp_path):
    manager = DomainMemoryManager(recipes_dir=tmp_path)
    recipe = manager.update_toc_plan(
        domain_or_url="https://clusternovel.net/series/99",
        toc_strategy="self_healed_dom",
        container_selector="div.toc-list",
        link_selector="a.toc-item",
        sample_url="https://clusternovel.net/series/99",
    )
    assert recipe.toc_config.strategy == "self_healed_dom"
    assert recipe.toc_config.container_selector == "div.toc-list"
    assert recipe.toc_config.link_selector == "a.toc-item"

    script_path = tmp_path / "clusternovel.net.py"
    assert script_path.exists()
    script_text = script_path.read_text(encoding="utf-8")
    assert "div.toc-list" in script_text
    assert "a.toc-item" in script_text
    assert "--batch" in script_text


def test_standalone_script_has_batch_runner(tmp_path):
    manager = DomainMemoryManager(recipes_dir=tmp_path)
    plan = DOMStructurePlan(title_selector="h1", content_selector="div.body")
    recipe = manager.save_recipe(
        domain_or_url="https://batchtest.com",
        sample_toc_url="https://batchtest.com/novel",
        sample_chapter_url="https://batchtest.com/novel/1",
        toc_strategy="dom_heuristic",
        chapter_plan=plan,
    )
    script_path = tmp_path / "batchtest.com.py"
    content = script_path.read_text(encoding="utf-8")
    assert "--batch" in content
    assert "args.batch" in content
    assert "extract_toc" in content
    assert "extract_chapter" in content


def test_toc_synthesizer_clustering():
    html = """
    <html><body>
      <div class="nav-bar">
        <a href="/home">Home</a>
        <a href="/catalog">Catalog</a>
      </div>
      <div class="chapter-cluster">
        <ul>
          <li class="c-row"><a class="link-item" href="/novel/1/1">Chapter 1: The Start</a></li>
          <li class="c-row"><a class="link-item" href="/novel/1/2">Chapter 2: The Journey</a></li>
          <li class="c-row"><a class="link-item" href="/novel/1/3">Chapter 3: The Climax</a></li>
          <li class="c-row"><a class="link-item" href="/novel/1/4">Chapter 4: The End</a></li>
        </ul>
      </div>
      <div class="footer"><a href="/about">About Us</a></div>
    </body></html>
    """
    chapters, container_sel, link_sel = TocSynthesizer.synthesize(
        html=html,
        url="https://cluster.test/novel/1",
        claimed_count=4,
    )
    assert len(chapters) == 4
    assert chapters[0].title == "Chapter 1: The Start"
    assert chapters[3].title == "Chapter 4: The End"
    assert "li.c-row" in container_sel or "link-item" in link_sel


@pytest.mark.asyncio
async def test_toc_graph_self_heal_routing():
    workflow = TocGraphWorkflow()
    # State with 0 chapters extracted, incomplete, and heal_attempted=False
    state: TocState = {
        "url": "https://testnovel.org/toc",
        "html": "<html><body><div></div></body></html>",
        "novel_title": "Test Novel",
        "author": None,
        "description": None,
        "claimed_chapter_count": 5,
        "extracted_chapters": [],
        "extraction_strategy": "initial",
        "confidence_score": 0.0,
        "has_unexpanded_sections": False,
        "has_pagination": False,
        "pagination_urls": [],
        "is_complete": False,
        "iteration": 1,
        "max_iterations": 3,
        "issues": ["No chapters found"],
        "logs": [],
        "heal_attempted": False,
    }

    # Route after audit should route to self_heal_toc
    route = workflow._route_after_audit(state)
    assert route == "self_heal_toc"

    # Now if heal_attempted is True, it should finalize or expand
    state["heal_attempted"] = True
    route2 = workflow._route_after_audit(state)
    assert route2 in ["finalize", "interactive_expand"]


@pytest.mark.asyncio
async def test_batch_runner_auto_sync_on_heal(tmp_path):
    plan = DOMStructurePlan(
        title_selector="h1.old-title",
        content_selector="div.old-body",
    )
    runner = BatchScraperRunner(
        novel_title="Test Sync Novel",
        initial_plan=plan,
        concurrency=1,
    )

    mock_healer = MagicMock()
    mock_healed_plan = DOMStructurePlan(
        title_selector="h1.new-title",
        content_selector="div.new-body",
    )
    mock_extracted = ExtractedChapter(
        title="Chapter 1 Healed",
        content_markdown="A" * 600,
        word_count=100,
        char_count=600,
        success=True,
    )
    mock_healer.attempt_heal = AsyncMock(return_value=(True, mock_healed_plan, mock_extracted))
    runner.healer = mock_healer

    # Mock obscura to return valid HTML
    mock_obscura = MagicMock()
    mock_obscura.fetch_html = AsyncMock(return_value="<html><body><h1>Old</h1><p>Too short</p></body></html>")
    runner.obscura = mock_obscura

    # Mock storage
    runner.storage = MagicMock()
    runner.storage.save_chapter = AsyncMock(return_value=Path("/tmp/saved.md"))

    with patch("src.agent.domain_memory.domain_memory.update_chapter_plan") as mock_update:
        link = ChapterLink(index=1, title="Chapter 1", url="https://synctest.org/ch1")
        sem = MagicMock()
        sem.__aenter__ = AsyncMock()
        sem.__aexit__ = AsyncMock()

        success = await runner._scrape_single_chapter(link, sem)
        assert success is True
        assert runner.plan.title_selector == "h1.new-title"
        mock_update.assert_called_once_with("https://synctest.org/ch1", mock_healed_plan, sample_url="https://synctest.org/ch1")
