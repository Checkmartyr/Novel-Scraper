import pytest
import json
from pathlib import Path
from bs4 import BeautifulSoup

from src.agent.analyzer import DOMStructurePlan
from src.agent.classifier import ChapterLink
from src.agent.domain_memory import (
    DomainMemoryManager,
    DomainRecipe,
    TocRecipeConfig,
    ChapterRecipeConfig,
)
from src.agent.review_loop import ReviewLoopOrchestrator
from src.agent.toc.agent import TocAgent


def test_domain_normalization():
    assert DomainMemoryManager.normalize_domain("https://freewebnovel.com/novel/test") == "freewebnovel.com"
    assert DomainMemoryManager.normalize_domain("https://www.empirenovel.com/novel/123/") == "empirenovel.com"
    assert DomainMemoryManager.normalize_domain("http://ncode.syosetu.com/n2273dh/") == "ncode.syosetu.com"
    assert DomainMemoryManager.normalize_domain("http://localhost:8000/test") == "localhost"
    assert DomainMemoryManager.normalize_domain("kakuyomu.jp") == "kakuyomu.jp"
    assert DomainMemoryManager.normalize_domain("") == ""


def test_save_and_load_recipe(tmp_path):
    manager = DomainMemoryManager(recipes_dir=tmp_path)
    plan = DOMStructurePlan(
        title_selector="h1.title",
        content_selector="div.entry-content",
        remove_selectors=[".ads", ".share"],
    )

    recipe = manager.save_recipe(
        domain_or_url="https://testnovel.com/novel/1",
        sample_toc_url="https://testnovel.com/novel/1",
        sample_chapter_url="https://testnovel.com/novel/1/chapter-1",
        toc_strategy="dom_heuristic",
        chapter_plan=plan,
        toc_link_selector="a.ch-link",
        quality_score=0.95,
    )

    assert recipe.domain == "testnovel.com"
    assert (tmp_path / "testnovel.com.json").exists()
    assert (tmp_path / "testnovel.com.py").exists()

    # Load back
    loaded = manager.get_recipe("https://testnovel.com/other-novel")
    assert loaded is not None
    assert loaded.domain == "testnovel.com"
    assert loaded.chapter_config.title_selector == "h1.title"
    assert loaded.chapter_config.content_selector == "div.entry-content"
    assert loaded.chapter_config.quality_score == 0.95


def test_generated_python_script_execution(tmp_path):
    manager = DomainMemoryManager(recipes_dir=tmp_path)
    plan = DOMStructurePlan(
        title_selector="h1.chapter-title",
        content_selector="#article",
        remove_selectors=[".ad-banner"],
    )

    manager.save_recipe(
        domain_or_url="https://mysite.com",
        sample_toc_url="https://mysite.com/toc",
        sample_chapter_url="https://mysite.com/c1",
        toc_strategy="dom_heuristic",
        chapter_plan=plan,
        toc_link_selector="a.ch",
    )

    script_path = tmp_path / "mysite.com.py"
    assert script_path.exists()
    code_text = script_path.read_text(encoding="utf-8")

    # Verify python syntax and execute functions in isolated namespace
    namespace = {}
    exec(code_text, namespace)

    assert "extract_toc" in namespace
    assert "extract_chapter" in namespace

    # Test extract_toc in generated script
    toc_html = """
    <html><body>
        <a class="ch" href="/c1">Chapter 1: The Beginning</a>
        <a class="ch" href="/c2">Chapter 2: The Journey</a>
    </body></html>
    """
    toc_res = namespace["extract_toc"](toc_html, "https://mysite.com/toc")
    assert len(toc_res) == 2
    assert toc_res[0]["title"] == "Chapter 1: The Beginning"
    assert toc_res[0]["url"] == "https://mysite.com/c1"

    # Test extract_chapter in generated script
    chapter_html = """
    <html><body>
        <h1 class="chapter-title">Chapter 1: The Beginning</h1>
        <div id="article">
            <div class="ad-banner">Buy our stuff</div>
            <p>Once upon a time in a fantasy world far away.</p>
            <p>Our hero woke up with extraordinary abilities.</p>
        </div>
    </body></html>
    """
    ch_res = namespace["extract_chapter"](chapter_html)
    assert ch_res["title"] == "Chapter 1: The Beginning"
    assert "Once upon a time" in ch_res["content"]
    assert "Buy our stuff" not in ch_res["content"]
    assert ch_res["word_count"] > 10


def test_toc_fast_path(tmp_path):
    manager = DomainMemoryManager(recipes_dir=tmp_path)
    plan = DOMStructurePlan(title_selector="h1", content_selector="#content")
    manager.save_recipe(
        domain_or_url="https://booksite.com/series/1",
        sample_toc_url="https://booksite.com/series/1",
        sample_chapter_url="https://booksite.com/series/1/c1",
        toc_strategy="dom_heuristic",
        chapter_plan=plan,
        toc_link_selector="ul.chapter-list a",
    )

    recipe = manager.get_recipe("booksite.com")
    html = """
    <html><body>
        <ul class="chapter-list">
            <li><a href="/series/1/c1">Chapter 1</a></li>
            <li><a href="/series/1/c2">Chapter 2</a></li>
            <li><a href="/series/1/c3">Chapter 3</a></li>
        </ul>
    </body></html>
    """
    ok, chapters = manager.test_toc_recipe(recipe, html, "https://booksite.com/series/1")
    assert ok is True
    assert len(chapters) == 3
    assert chapters[0].url == "https://booksite.com/series/1/c1"
    assert chapters[2].title == "Chapter 3"


def test_chapter_fast_path(tmp_path):
    manager = DomainMemoryManager(recipes_dir=tmp_path)
    plan = DOMStructurePlan(
        title_selector="h2.heading",
        content_selector="div#chapter-content",
        remove_selectors=[".nav"],
    )
    manager.save_recipe(
        domain_or_url="https://readnovel.org",
        sample_toc_url="",
        sample_chapter_url="",
        toc_strategy="dom_heuristic",
        chapter_plan=plan,
    )

    recipe = manager.get_recipe("readnovel.org")
    html = """
    <html><body>
        <h2 class="heading">Episode 10: Victory</h2>
        <div id="chapter-content">
            <div class="nav">Next Previous</div>
            <p>The dragon roared across the misty mountains as the party held their ground.</p>
            <p>With a decisive swing of his enchanted sword, the spell broke and the land was saved.</p>
        </div>
    </body></html>
    """
    verif = manager.test_chapter_recipe(recipe, html)
    assert verif.success is True
    assert verif.chapter_title == "Episode 10: Victory"
    assert verif.word_count > 20


@pytest.mark.asyncio
async def test_review_loop_fast_path_integration(tmp_path, monkeypatch):
    # Setup recipe in tmp_path and patch global domain_memory
    manager = DomainMemoryManager(recipes_dir=tmp_path)
    plan = DOMStructurePlan(
        title_selector="h1.tit",
        content_selector="div.txt",
    )
    manager.save_recipe(
        domain_or_url="https://instantnovel.org",
        sample_toc_url="",
        sample_chapter_url="",
        toc_strategy="dom_heuristic",
        chapter_plan=plan,
        quality_score=0.95,
    )

    import src.agent.domain_memory as dm_module
    monkeypatch.setattr(dm_module, "domain_memory", manager)

    chapter_html = """
    <html><body>
        <h1 class="tit">Chapter 5: Reincarnation</h1>
        <div class="txt">
            <p>I woke up in a lavish royal bed with silk sheets draped over my shoulders.</p>
            <p>Looking out the stained glass window, floating castles drifted in the clear blue sky.</p>
        </div>
    </body></html>
    """

    orchestrator = ReviewLoopOrchestrator()
    result = await orchestrator.run(chapter_html, "https://instantnovel.org/novel/5")

    # Fast path should hit with 0 iterations and approval!
    assert result.approved is True
    assert result.iterations == 0
    assert result.verification.success is True
    assert result.verification.chapter_title == "Chapter 5: Reincarnation"
    assert result.review.quality_score == 0.95


@pytest.mark.asyncio
async def test_toc_agent_fast_path_integration(tmp_path, monkeypatch):
    manager = DomainMemoryManager(recipes_dir=tmp_path)
    plan = DOMStructurePlan(title_selector="h1", content_selector="#art")
    manager.save_recipe(
        domain_or_url="https://fasttoc.com",
        sample_toc_url="",
        sample_chapter_url="",
        toc_strategy="dom_heuristic",
        chapter_plan=plan,
        toc_link_selector="div.list a",
    )

    import src.agent.domain_memory as dm_module
    monkeypatch.setattr(dm_module, "domain_memory", manager)

    toc_html = """
    <html><head><title>The Grand Journey - FastToc</title></head>
    <body>
        <h1>The Grand Journey</h1>
        <div class="list">
            <a href="/c1">Chapter 1: Dawn</a>
            <a href="/c2">Chapter 2: Noon</a>
            <a href="/c3">Chapter 3: Dusk</a>
        </div>
    </body></html>
    """

    agent = TocAgent()
    state = await agent.extract_toc("https://fasttoc.com/novel/grand-journey", html=toc_html)

    assert state["extraction_strategy"] == "remembered_recipe"
    assert state["confidence_score"] == 1.0
    assert len(state["extracted_chapters"]) == 3
    assert state["extracted_chapters"][0].title == "Chapter 1: Dawn"


def test_recipe_stats_and_delete(tmp_path):
    manager = DomainMemoryManager(recipes_dir=tmp_path)
    plan = DOMStructurePlan(title_selector="h1", content_selector="p")
    manager.save_recipe(
        domain_or_url="https://deleteme.com",
        sample_toc_url="",
        sample_chapter_url="",
        toc_strategy="dom_heuristic",
        chapter_plan=plan,
    )

    assert manager.get_recipe("deleteme.com") is not None
    manager.record_usage("deleteme.com")
    recipe = manager.get_recipe("deleteme.com")
    assert recipe.times_used == 1
    assert recipe.last_used_at is not None

    recipes = manager.list_recipes()
    assert any(r["domain"] == "deleteme.com" for r in recipes)

    deleted = manager.delete_recipe("deleteme.com")
    assert deleted is True
    assert manager.get_recipe("deleteme.com") is None
    assert not (tmp_path / "deleteme.com.json").exists()
    assert not (tmp_path / "deleteme.com.py").exists()


@pytest.mark.asyncio
async def test_recipe_self_healing_fallback(tmp_path, monkeypatch):
    manager = DomainMemoryManager(recipes_dir=tmp_path)
    # Saved plan targets #obsolete_content
    plan = DOMStructurePlan(
        title_selector="h1.old-title",
        content_selector="#obsolete_content",
    )
    manager.save_recipe(
        domain_or_url="https://redesignedsite.org",
        sample_toc_url="",
        sample_chapter_url="",
        toc_strategy="dom_heuristic",
        chapter_plan=plan,
        quality_score=0.9,
    )

    import src.agent.domain_memory as dm_module
    monkeypatch.setattr(dm_module, "domain_memory", manager)

    # Redesigned page has new layout
    redesigned_html = """
    <html><body>
        <h1 class="new-title">Redesigned Chapter</h1>
        <article class="modern-body">
            <p>The entire layout was redesigned by web developers!</p>
            <p>The old selector #obsolete_content is completely gone.</p>
        </article>
    </body></html>
    """

    recipe = manager.get_recipe("redesignedsite.org")
    verif = manager.test_chapter_recipe(recipe, redesigned_html)
    # Verification should fail cleanly
    assert verif.success is False
    assert "Content container '#obsolete_content' not found" in (verif.error or "")


@pytest.mark.asyncio
async def test_page_classifier_domain_bypass(tmp_path, monkeypatch):
    from src.agent.classifier import PageClassifier

    manager = DomainMemoryManager(recipes_dir=tmp_path)
    plan = DOMStructurePlan(
        title_selector="h1.reader-title",
        content_selector="div#chapter-body",
    )
    manager.save_recipe(
        domain_or_url="https://bypasstest.org",
        sample_toc_url="https://bypasstest.org/novel/test-book",
        sample_chapter_url="https://bypasstest.org/novel/test-book/ch-1",
        toc_strategy="dom_heuristic",
        chapter_plan=plan,
        toc_link_selector="div.catalog a",
        quality_score=1.0,
    )

    import src.agent.domain_memory as dm_module
    monkeypatch.setattr(dm_module, "domain_memory", manager)

    classifier = PageClassifier()

    # 1. Test TOC page classification bypass
    toc_html = """
    <html><head><title>Test Book - BypassTest</title></head>
    <body>
        <h1>Test Book</h1>
        <div class="catalog">
            <a href="/novel/test-book/ch-1">Chapter 1: The Call</a>
            <a href="/novel/test-book/ch-2">Chapter 2: The Journey</a>
        </div>
    </body></html>
    """
    toc_res = await classifier.classify("https://bypasstest.org/novel/test-book", toc_html)
    assert toc_res.page_type == "TOC"
    assert len(toc_res.chapter_links) == 2
    assert toc_res.chapter_links[0].title == "Chapter 1: The Call"
    assert toc_res.toc_state is not None
    assert toc_res.toc_state["extraction_strategy"] == "remembered_recipe"

    # 2. Test Chapter page classification bypass
    ch_html = """
    <html><body>
        <h1 class="reader-title">Chapter 1: The Call</h1>
        <div id="chapter-body">
            <p>The mysterious call resonated through the ancient temple ruins.</p>
            <p>He gripped his staff firmly, preparing for the trials ahead.</p>
        </div>
    </body></html>
    """
    ch_res = await classifier.classify("https://bypasstest.org/novel/test-book/ch-1", ch_html)
    assert ch_res.page_type == "CHAPTER"
    assert ch_res.chapter_title == "Chapter 1: The Call"
    assert ch_res.toc_url == "https://bypasstest.org/novel/test-book"
