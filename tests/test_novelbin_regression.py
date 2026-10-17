"""Offline regression coverage for sparse, hydrated NovelBin pages."""
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from src.agent.ch.analyzer import ChapterAnalyzer
from src.agent.ch.code_generator import ChapterCodeGenerator
from src.agent.classifier import ChapterLink, PageClassifier
from src.agent.toc.tools import ClaimInspector, DomLinkExtractor
from src.scraper.runtime import belongs_to_observed_novel


TOC_URL = "https://novelbin.test/novel/pink-airhead"
BOOK_JSON = json.dumps({
    "@context": "https://schema.org",
    "@type": "Book",
    "name": "I Became the Academy's Pink Airhead",
    "url": TOC_URL,
    "numberOfPages": 3,
})
SPARSE_CATALOG = f"""
<html><head><title>NovelBin</title>
<script type="application/ld+json">{BOOK_JSON}</script></head>
<body><div id="root"><h1>NovelBin - Read Light Novels</h1>
<a href="/">Home</a><a href="/read/pink-airhead/chapter-1">Read Chapter 1</a>
<p>Total Chapters: 3 Chapters</p></div></body></html>
"""
RENDERED_CATALOG = f"""
<html><head><title>Pink Airhead</title>
<script type="application/ld+json">{BOOK_JSON}</script></head>
<body><h1>I Became the Academy's Pink Airhead</h1><p>Total Chapters: 3 Chapters</p>
<div class="chapter-list">
<a href="/read/pink-airhead/chapter-1">Chapter 0: Character Summaries</a>
<a href="/read/pink-airhead/chapter-2">Chapter 1: First Day</a>
<a href="/read/pink-airhead/chapter-3">Chapter 2: The Academy</a>
</div></body></html>
"""


class OfflineLLM:
    is_available = False


class FakeTocAgent:
    def __init__(self):
        self.html = None

    async def extract_toc(self, url, html=None):
        self.html = html
        chapters = DomLinkExtractor.extract(html or "", url)
        claimed, title, author, description = ClaimInspector.inspect(html or "", url)
        complete = bool(claimed and len(chapters) >= claimed)
        return {
            "url": url,
            "html": html,
            "novel_title": title,
            "author": author,
            "description": description,
            "claimed_chapter_count": claimed,
            "extracted_chapters": chapters,
            "is_complete": complete,
            "issues": [] if complete else ["Catalog remains incomplete"],
        }


class FakeBrowser:
    def __init__(self, rendered_html=None, render_error=None):
        self.rendered_html = rendered_html
        self.render_error = render_error
        self.calls = []

    async def fetch_html(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.render_error:
            raise self.render_error
        return self.rendered_html


@pytest.mark.asyncio
async def test_matching_book_metadata_renders_sparse_catalog_before_classification():
    browser = FakeBrowser(RENDERED_CATALOG)
    toc = FakeTocAgent()
    classifier = PageClassifier(llm_client=OfflineLLM(), obscura_client=browser, toc_agent=toc)

    result = await classifier.classify(TOC_URL, SPARSE_CATALOG)

    assert len(browser.calls) == 1
    assert browser.calls[0][1]["force_browser"] is True
    assert result.page_type == "TOC"
    assert result.novel_title == "I Became the Academy's Pink Airhead"
    assert [chapter.url for chapter in result.chapter_links] == [
        f"https://novelbin.test/read/pink-airhead/chapter-{index}" for index in range(1, 4)
    ]
    assert result.toc_state["is_complete"] is True
    assert toc.html == RENDERED_CATALOG


@pytest.mark.asyncio
async def test_sparse_catalog_render_failure_is_reported_and_stays_a_toc():
    browser = FakeBrowser(render_error=RuntimeError("render failed"))
    classifier = PageClassifier(
        llm_client=OfflineLLM(), obscura_client=browser, toc_agent=FakeTocAgent()
    )

    result = await classifier.classify(TOC_URL, SPARSE_CATALOG)

    assert result.page_type == "TOC"
    assert result.toc_state["is_complete"] is False
    assert any("render failed" in issue for issue in result.toc_state["issues"])


def test_home_link_is_not_a_toc_candidate():
    html = '<html><h1>Novel</h1><a href="/">Home</a><a href="/chapter-1">Chapter 1</a></html>'
    classifier = PageClassifier(llm_client=OfflineLLM())
    summary = classifier._extract_page_summary(html, "https://fixture.test/novel/one")

    result = classifier._heuristic_classify("https://fixture.test/novel/one", html, summary)

    assert result.page_type == "CHAPTER"
    assert result.toc_url is None


def test_novel_and_reader_routes_share_only_the_matching_slug():
    source = "https://novelbin.test/novel/pink-airhead"

    assert belongs_to_observed_novel(
        source, "https://novelbin.test/read/pink-airhead/chapter-2"
    )
    assert not belongs_to_observed_novel(
        source, "https://novelbin.test/read/other-novel/chapter-2"
    )
    assert not belongs_to_observed_novel(source, "https://novelbin.test/genre/action")
    assert not belongs_to_observed_novel(source, "https://novelbin.test/")


def test_catalog_links_exclude_other_novels_and_global_pages():
    html = """<div>
    <a href="/read/pink-airhead/chapter-1">Chapter 1</a>
    <a href="/read/pink-airhead/chapter-2">Chapter 2</a>
    <a href="/read/other-novel/chapter-1">Chapter 1</a>
    <a href="/genre/action">Chapter 1</a>
    </div>"""

    chapters = DomLinkExtractor.extract(html, "https://novelbin.test/novel/pink-airhead")

    assert [chapter.url for chapter in chapters] == [
        "https://novelbin.test/read/pink-airhead/chapter-1",
        "https://novelbin.test/read/pink-airhead/chapter-2",
    ]


@pytest.mark.asyncio
async def test_discovery_reports_scope_filtered_catalog_coverage(monkeypatch):
    from src import api_bridge

    chapters = [
        ChapterLink(index=1, title="Chapter 1", url="https://novelbin.test/read/pink-airhead/chapter-1"),
        ChapterLink(index=2, title="Chapter 1", url="https://novelbin.test/read/other-novel/chapter-1"),
        ChapterLink(index=3, title="Action", url="https://novelbin.test/genre/action"),
    ]

    class Browser:
        async def fetch_html(self, url, **kwargs):
            return RENDERED_CATALOG

    class Toc:
        def __init__(self, **kwargs):
            pass

    class Classifier:
        def __init__(self, **kwargs):
            pass

        async def classify(self, url, html):
            return SimpleNamespace(
                page_type="TOC",
                toc_state={
                    "novel_title": "I Became the Academy's Pink Airhead",
                    "claimed_chapter_count": 3,
                    "extracted_chapters": chapters,
                    "is_complete": True,
                    "issues": [],
                },
                chapter_links=chapters,
                novel_title="I Became the Academy's Pink Airhead",
                author=None,
                description=None,
            )

    monkeypatch.setattr(api_bridge, "TocAgent", Toc)
    monkeypatch.setattr(api_bridge, "PageClassifier", Classifier)
    result = await api_bridge.discover(TOC_URL, Browser(), OfflineLLM())

    assert result["total_chapters"] == 1
    assert result["is_complete"] is False
    assert any("extracted 1 of 3" in issue for issue in result["issues"])


def test_reader_selectors_extract_mixed_text_without_footer():
    html = """<html><body><div id="root">
    <h1 class="reader-header-chapter">Chapter 7: Mixed Text</h1>
    <div class="reader-content"><p>The opening <em>stays inline</em>.</p>
    <div>A direct-text paragraph.</div><p>The actual ending.</p></div>
    <footer>Copyright NovelBin</footer></div></body></html>"""
    analyzer = ChapterAnalyzer(OfflineLLM())

    plan = analyzer._heuristic_analyze(html)
    extracted = ChapterCodeGenerator(plan).extract(html)
    content = BeautifulSoup(extracted.content_markdown, "lxml").get_text(" ", strip=True)

    assert plan.title_selector == ".reader-header-chapter"
    assert plan.content_selector == ".reader-content"
    assert "opening stays inline" in content
    assert "direct-text paragraph" in content
    assert extracted.content_markdown.endswith("The actual ending.")
    assert "Copyright" not in content


@pytest.mark.asyncio
async def test_fetch_html_waits_for_populated_content_selector(monkeypatch):
    from src.core.obscura_client import ObscuraClient

    class Page:
        wait = None

        def on(self, *args):
            pass

        async def route(self, *args):
            pass

        async def goto(self, *args, **kwargs):
            raise TimeoutError("networkidle did not settle")

        async def wait_for_function(self, expression, **kwargs):
            self.wait = (expression, kwargs)

        async def content(self):
            return '<div class="reader-content"><p>Ready</p></div>' + " " * 400

    page = Page()
    client = ObscuraClient()

    @asynccontextmanager
    async def browser(**kwargs):
        yield page

    monkeypatch.setattr(client, "get_playwright_page", browser)
    try:
        html = await client.fetch_html(
            "https://fixture.test/read/book/chapter-1",
            force_browser=True,
            content_selector=".reader-content",
        )
    finally:
        await client.close()

    assert page.wait is not None
    assert page.wait[1]["arg"] == ".reader-content"
    assert "textContent" in page.wait[0]
    assert "Ready" in html


@pytest.mark.asyncio
async def test_fetch_html_rejects_unpopulated_reader_skeleton(monkeypatch):
    from src.core.obscura_client import ObscuraClient

    class Page:
        def on(self, *args):
            pass

        async def route(self, *args):
            pass

        async def goto(self, *args, **kwargs):
            pass

        async def wait_for_function(self, expression, **kwargs):
            raise TimeoutError("selector did not populate")

        async def content(self):
            return '<main class="reader-content"><div class="skeleton"> </div></main>' + " " * 400

        async def evaluate(self, expression):
            return await self.content()

    client = ObscuraClient()

    @asynccontextmanager
    async def browser(**kwargs):
        yield Page()

    monkeypatch.setattr(client, "get_playwright_page", browser)
    try:
        with pytest.raises(TimeoutError, match="populated content selector"):
            await client.fetch_html(
                "https://fixture.test/read/book/chapter-1",
                force_browser=True,
                content_selector=".reader-content",
            )
    finally:
        await client.close()
