"""Fixture-driven acceptance for adaptive discovery and durable raw extraction."""
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from src.agent.classifier import ChapterLink
from src.agent.ch.analyzer import DOMStructurePlan
from src.agent.ch.code_generator import ChapterCodeGenerator
from src.agent.toc.agent import TocAgent
from src.agent.toc.graph import TocGraphWorkflow
from src.agent.toc.tools import TocAuditor, PaginatedTocCrawler, DomLinkExtractor
from src.agent.toc.discovery import expand_catalog, network_chapters
from src.scraper.batch_runner import BatchScraperRunner
from src.scraper.storage import NovelStorage
from src.scraper.runtime import ScraperLimits, ChapterCheckpoint, DestinationLock, atomic_write, fingerprint, belongs_to_observed_novel
from src.api_bridge import select_chapters, extract_novel_chapters


def chapter_html(index=1):
    return f'<html><h1>Chapter {index}</h1><div id="chapter-content">' + ''.join(f'<p>Paragraph {i} in chapter {index} preserves the story and its words.</p>' for i in range(10)) + '</div></html>'


class FixtureFetcher:
    limits = ScraperLimits()
    metrics = {}
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
    async def fetch_html(self, url, **kwargs):
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value
    async def close(self):
        pass


def links(count):
    return [ChapterLink(index=i, title=f"Chapter {i}", url=f"https://fixture.test/book/{i}") for i in range(1, count+1)]


@pytest.mark.asyncio
async def test_budget_finalization_never_invents_completeness():
    workflow = TocGraphWorkflow(obscura_client=FixtureFetcher({}))
    state = {"extracted_chapters": [], "iteration": 8, "max_iterations": 8, "has_unexpanded_sections": True, "logs": []}
    assert workflow._route_after_audit(state) == "finalize"
    assert (await workflow._node_finalize(state))["is_complete"] is False
    assert TocAuditor.audit(links(95), 100, "")[0] is False


def test_advertised_count_cannot_hide_unresolved_catalog_controls():
    assert not TocAuditor.audit(links(5), 5, '<button>Load more</button>')[0]
    assert not TocAuditor.audit(links(5), 5, '<a href="?cursor=next">Next</a>')[0]
    assert TocAuditor.audit(links(5), 5, '<button disabled>Load more</button>')[0]
    assert TocAuditor.audit(links(5), 5, '<button hidden>Load more</button>')[0]


def test_selection_does_not_expand_to_all_and_keeps_indices():
    assert [item.index for item in select_chapters(links(8), [3, 7])] == [3, 7]
    for kwargs in ({"chapter_indices": []}, {"chapter_indices": [9]}, {"start_chapter": 90}):
        with pytest.raises(ValueError):
            select_chapters(links(8), **kwargs)


def test_preview_uses_actual_ending_and_short_chapters_are_valid():
    generator = ChapterCodeGenerator(DOMStructurePlan(title_selector="h1", content_selector="#chapter-content"))
    assert "Paragraph 9" in generator.test_and_verify(chapter_html()).preview_markdown
    assert generator.extract('<h1>Chapter 1</h1><div id="chapter-content"><p>The end.</p></div>').success


def test_adjacent_source_paragraphs_expose_a_narrow_selector():
    from src.agent.ch.observer import ExtractionObserver
    plan = DOMStructurePlan(title_selector="h1", content_selector="#chapter-content p:first-of-type")
    html = chapter_html()
    verification = ChapterCodeGenerator(plan).test_and_verify(html)
    assert "Paragraph 9" in verification.source_tail
    assert not ExtractionObserver()._heuristic_review(html, plan, verification).is_accurate


def test_paywall_preview_is_never_an_approved_parser():
    plan = DOMStructurePlan(title_selector="h1", content_selector="#chapter-content")
    html = '<h1>Chapter 1</h1><div id="chapter-content"><p>' + 'Preview prose. ' * 30 + '</p><p>購入して続きを読む</p></div>'
    verification = ChapterCodeGenerator(plan).test_and_verify(html)
    assert verification.is_paywalled and not verification.success


@pytest.mark.asyncio
async def test_storage_ownership_atomicity_and_title_changes(tmp_path, monkeypatch):
    storage = NovelStorage(base_output_dir=tmp_path, direct_output=True, overwrite=True)
    result = ChapterCodeGenerator(DOMStructurePlan(title_selector="h1", content_selector="#chapter-content")).extract(chapter_html())
    original = await storage.save_chapter("Novel", 1, "Old", result, "https://fixture.test/book/1")
    renamed = await storage.save_chapter("Novel", 1, "New", result, "https://fixture.test/book/1")
    assert original == renamed
    with pytest.raises(FileExistsError):
        await storage.save_chapter("Novel", 1, "Other", result, "https://fixture.test/other/1")
    before = original.read_bytes()
    monkeypatch.setattr("src.scraper.runtime.os.replace", lambda *args: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        atomic_write(original, "unfinished")
    assert original.read_bytes() == before
    assert not list(tmp_path.glob(".scraper-*"))


@pytest.mark.asyncio
async def test_each_worker_outcome_is_accounted_for(tmp_path):
    chapters = links(3)
    fetcher = FixtureFetcher({chapters[0].url: chapter_html(), chapters[1].url: RuntimeError("network failure"), chapters[2].url: PermissionError("expired session")})
    checkpoint = ChapterCheckpoint(tmp_path / "state", "job")
    progress = []
    runner = BatchScraperRunner("Novel", DOMStructurePlan(title_selector="h1", content_selector="#chapter-content"),
        obscura_client=fetcher, storage=NovelStorage(base_output_dir=tmp_path / "raw", direct_output=True), checkpoint=checkpoint,
        on_progress=lambda *event: progress.append(event))
    result = await runner.run(chapters, "https://fixture.test/book")
    assert (result["saved"], result["failed"], result["blocked"]) == (1, 1, 1)
    assert len(result["outcomes"]) == len(checkpoint.records) == 3
    assert progress[-1][-1] == 100
    assert result["metrics"]["peak_active_workers"] <= 3
    assert len(list((tmp_path / "raw").glob("*.md"))) == 1


@pytest.mark.asyncio
async def test_cancel_and_resume_hash_verified_chapters(tmp_path):
    chapters = links(5)
    checkpoint = ChapterCheckpoint(tmp_path / "state", "job")
    checkpoint.cancel_path.parent.mkdir(parents=True)
    fetcher = FixtureFetcher({chapter.url: chapter_html(chapter.index) for chapter in chapters})
    storage = NovelStorage(base_output_dir=tmp_path / "raw", direct_output=True, overwrite=False)
    def cancel_after_first(*args):
        checkpoint.cancel_path.touch()
    plan = DOMStructurePlan(title_selector="h1", content_selector="#chapter-content")
    first = await BatchScraperRunner("Novel", plan, obscura_client=fetcher, storage=storage, concurrency=1,
        checkpoint=checkpoint, on_progress=cancel_after_first).run(chapters, "https://fixture.test/book")
    assert first["saved"] == 1 and first["cancelled"] == 4
    checkpoint.cancel_path.unlink()
    second = await BatchScraperRunner("Novel", plan, obscura_client=fetcher, storage=storage, checkpoint=checkpoint).run(chapters, "https://fixture.test/book")
    assert second["saved"] == 4 and second["skipped"] == 1
    assert fetcher.calls.count(chapters[0].url) == 1
    record = checkpoint.records[chapters[0].url]
    (storage.base_output_dir / record["file"]).write_text("corrupted", encoding="utf-8")
    assert checkpoint.verified(chapters[0].url, storage.base_output_dir) is None


@pytest.mark.asyncio
async def test_recursive_catalog_frontier_and_query_preservation():
    base = "https://fixture.test/book?lang=ja"
    def toc(indices, next_page=None):
        return '<h1>Novel</h1><span>6 Chapters</span><div class="chapter-list">' + ''.join(f'<a href="/book/{i}">Chapter {i}</a>' for i in indices) + '</div>' + (f'<a href="?lang=ja&page={next_page}">Next</a>' if next_page else '')
    fetcher = FixtureFetcher({base: toc([1, 2], 2), "https://fixture.test/book?lang=ja&page=2": toc([3, 4], 3), "https://fixture.test/book?lang=ja&page=3": toc([5, 6])})
    state = await TocAgent(obscura_client=fetcher).extract_toc(base)
    assert state["is_complete"]
    assert len(state["extracted_chapters"]) == 6
    assert len(set(fetcher.calls)) == 3
    assert all("lang=ja" in url for url in PaginatedTocCrawler.detect_pagination_urls(toc([1], 3), base))


def test_api_cursor_uses_observed_endpoint_and_rejects_external_links():
    chapters, frontier = network_chapters([{"url": "https://fixture.test/api/catalog?book=7&cursor=first", "data": {
        "chapters": [{"title": "Chapter 1", "url": "/book/1"}, {"title": "Other", "url": "https://other.test/1"}], "nextCursor": "second"}}], "https://fixture.test/book")
    assert len(chapters) == 1
    assert frontier == ["https://fixture.test/api/catalog?book=7&cursor=second"]


@pytest.mark.asyncio
async def test_virtualized_catalog_accumulates_intermediate_views():
    class Page:
        position = 0
        def on(self, *args): pass
        async def goto(self, *args, **kwargs): pass
        async def content(self):
            start = self.position * 3 + 1
            return '<div class="chapter-list">' + ''.join(f'<a href="/book/{i}">Chapter {i}</a>' for i in range(start, start+3)) + '</div>'
        async def evaluate(self, expression):
            if "scrollBy" in expression: self.position = min(2, self.position + 1)
            return self.position
        async def wait_for_timeout(self, *args): pass
    class Browser:
        limits = ScraperLimits()
        async def pace(self, *args): pass
        @asynccontextmanager
        async def get_playwright_page(self, **kwargs): yield Page()
    html, chapters, frontier = await expand_catalog("https://fixture.test/book", Browser(), DomLinkExtractor.extract)
    assert len(chapters) == 9
    assert len(DomLinkExtractor.extract(html, "https://fixture.test/book")) == 3


@pytest.mark.asyncio
async def test_partial_subpage_is_not_saved(tmp_path):
    url = "https://fixture.test/book/1"
    html = chapter_html().replace('</html>', '<a href="?page=2">Next page</a></html>')
    fetcher = FixtureFetcher({url: html, url + "?page=2": '<h1>Chapter 1</h1>'})
    runner = BatchScraperRunner("Novel", DOMStructurePlan(title_selector="h1", content_selector="#chapter-content"),
        obscura_client=fetcher, storage=NovelStorage(base_output_dir=tmp_path, direct_output=True))
    result = await runner.run([ChapterLink(index=1, title="Chapter 1", url=url)], url)
    assert result["failed"] == 1
    assert not list(tmp_path.glob("*.md"))


@pytest.mark.parametrize("candidate,expected", [
    ("https://fixture.test/chapter.php?id=123&chapter=1", True),
    ("https://fixture.test/chapter.php?id=456&chapter=1", False),
    ("https://fixture.test/chapter.php?chapter=1", False),
    ("https://other.test/chapter.php?id=123&chapter=1", False),
])
def test_root_php_catalog_identity(candidate, expected):
    assert belongs_to_observed_novel("https://fixture.test/index.php?id=123", candidate) is expected


def test_unfamiliar_novel_identity_and_mixed_prose():
    source = "https://writer.dek-d.com/Kumari/writer/view.php?id=2655851"
    assert belongs_to_observed_novel(source, source.replace("view.php", "viewlongc.php") + "&chapter=1")
    assert not belongs_to_observed_novel(source, source.replace("view.php", "viewlongc.php").replace("2655851", "9999999") + "&chapter=1")
    assert not belongs_to_observed_novel("https://fixture.test/series/one", "https://fixture.test/series/two/chapter/1")
    generator = ChapterCodeGenerator(DOMStructurePlan(title_selector="h1", content_selector="#chapter-content"))
    html = '<h1>Chapter 1</h1><div id="chapter-content">Opening text.<p>A <em>quiet</em> dawn.</p>Final sentence.</div>'
    result = generator.extract(html)
    assert "Opening text." in result.content_markdown
    assert "A quiet dawn." in result.content_markdown
    assert "Final sentence." in result.content_markdown
    exported = {}
    exec(generator.generate_code_string(), exported)
    assert exported["extract_chapter"](html)["content"] == result.content_markdown


def test_standalone_worker_import_has_no_initialization_cycle():
    import subprocess
    import sys
    from pathlib import Path
    result = subprocess.run([sys.executable, "-m", "src.api_bridge", "--help"],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "extract" in result.stdout


def test_native_destination_ownership_releases_cleanly(tmp_path):
    first = DestinationLock(tmp_path / "state", tmp_path / "raw")
    second = DestinationLock(tmp_path / "state", tmp_path / "raw")
    first.acquire()
    try:
        with pytest.raises(BlockingIOError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


@pytest.mark.asyncio
async def test_http_pool_auth_cookie_scope_and_challenge_block(monkeypatch, tmp_path):
    from src.core.obscura_client import ObscuraClient
    state = tmp_path / "session.json"
    state.write_text(json.dumps({"cookies": [{"name": "auth", "value": "authorized", "domain": "fixture.test", "path": "/book"}]}), encoding="utf-8")
    monkeypatch.setenv("NOVEL_SCRAPER_STORAGE_STATE", str(state))
    monkeypatch.setenv("MIN_DELAY_SECONDS", "0")
    async def respond(request):
        if request.url.path == "/book":
            assert "auth=authorized" in request.headers["cookie"]
            return httpx.Response(200, text=chapter_html())
        assert "cookie" not in request.headers
        return httpx.Response(200, text='<title>Just a moment...</title><div id="challenge-form"></div>')
    original_client = httpx.AsyncClient
    monkeypatch.setattr("src.core.obscura_client.httpx.AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), trust_env=False, **kwargs))
    client = ObscuraClient()
    class ChallengePage:
        def on(self, *args): pass
        async def route(self, *args): pass
        async def goto(self, *args, **kwargs): pass
        async def content(self): return '<title>Just a moment...</title><div id="challenge-form"></div>'
    @asynccontextmanager
    async def browser(**kwargs): yield ChallengePage()
    monkeypatch.setattr(client, "get_playwright_page", browser)
    pooled = await client.http_client()
    assert await client.http_client() is pooled
    assert "Paragraph 9" in await client.fetch_html("https://fixture.test/book")
    with pytest.raises(PermissionError):
        await client.fetch_html("https://fixture.test/other")
    assert client._browser is None
    await client.close()


@pytest.mark.asyncio
async def test_rejected_samples_never_publish_or_promote(tmp_path, monkeypatch):
    from src import api_bridge
    from src.agent.domain_memory import domain_memory
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"url": "https://fixture.test/book", "novel_title": "Novel", "chapters": [item.model_dump() for item in links(2)], "is_complete": True}), encoding="utf-8")
    fetcher = FixtureFetcher({item.url: chapter_html(item.index) for item in links(2)})
    monkeypatch.setattr(api_bridge, "ObscuraClient", lambda: fetcher)
    class Rejected:
        def __init__(self, **kwargs): pass
        async def run(self, *args):
            from types import SimpleNamespace
            return SimpleNamespace(approved=False, verification=SimpleNamespace(success=False))
    monkeypatch.setattr(api_bridge, "ReviewLoopOrchestrator", Rejected)
    result = await extract_novel_chapters("https://fixture.test/book", tmp_path / "raw", state_dir=tmp_path / "state", snapshot_file=snapshot)
    assert result["status"] == "partial"
    assert result["failed"] == 2
    assert result["files"] == []
    assert domain_memory.list_recipes() == []


@pytest.mark.asyncio
async def test_cold_and_warm_extraction_benchmark(tmp_path, monkeypatch):
    from src import api_bridge
    from src.agent.ch.observer import ExtractionReview
    import time
    catalog = links(30)
    catalog_html = '<h1>Novel</h1><span>30 Chapters</span><div class="chapter-list">' + ''.join(f'<a href="{item.url}">{item.title}</a>' for item in catalog) + '</div>'
    class Model:
        is_available = True
        last_interaction_id = None
        calls = 0
        async def generate_json(self, schema, **kwargs):
            self.calls += 1
            if schema is DOMStructurePlan:
                return DOMStructurePlan(title_selector="h1", content_selector="#chapter-content")
            return ExtractionReview(is_accurate=True, quality_score=1, title_accurate=True, has_clutter=False)
    model = Model()
    monkeypatch.setattr(api_bridge, "LLMClient", lambda: model)
    monkeypatch.setattr(api_bridge, "ObscuraClient", lambda: FixtureFetcher({"https://fixture.test/book": catalog_html, **{item.url: chapter_html(item.index) for item in catalog}}))
    measurements = []
    for run in ("cold", "warm"):
        before = model.calls
        start = time.perf_counter()
        result = await extract_novel_chapters("https://fixture.test/book", tmp_path / run, state_dir=tmp_path / "state", job_id=run)
        elapsed = time.perf_counter() - start
        assert result["status"] == "completed" and result["saved"] == len(catalog), result
        measurements.append({"run": run, "chapters": len(catalog), "seconds": round(elapsed, 3),
                             "chapters_per_second": round(len(catalog)/elapsed, 2), "model_calls": model.calls-before,
                             "peak_workers": result["metrics"]["peak_active_workers"]})
    assert measurements[0]["model_calls"] > 0
    assert measurements[1]["model_calls"] == 0
    print("OFFLINE_BENCHMARK=" + json.dumps(measurements))


@pytest.mark.asyncio
async def test_chapter_input_remembers_the_actual_catalog_url(tmp_path, monkeypatch):
    from src import api_bridge
    from src.agent.classifier import ClassificationResult
    from src.agent.ch.observer import ExtractionReview
    from src.agent.domain_memory import domain_memory
    catalog_url = "https://fixture.test/series/novel"
    catalog = [ChapterLink(index=i, title=f"Chapter {i}", url=f"{catalog_url}/chapter/{i}") for i in range(1, 4)]
    pages = {catalog_url: '<h1>Novel</h1><div class="chapter-list">' + ''.join(f'<a href="{item.url}">{item.title}</a>' for item in catalog) + '</div>',
             **{item.url: chapter_html(item.index).replace('</html>', f'<a href="{catalog_url}">Table of contents</a></html>') for item in catalog}}
    class Model:
        is_available = True
        last_interaction_id = None
        async def generate_json(self, schema, **kwargs):
            if schema is ClassificationResult:
                return ClassificationResult(page_type="CHAPTER", novel_title="Novel", chapter_title="Chapter 2")
            if schema is DOMStructurePlan:
                return DOMStructurePlan(title_selector="h1", content_selector="#chapter-content")
            return ExtractionReview(is_accurate=True, quality_score=1, title_accurate=True, has_clutter=False)
    model = Model()
    monkeypatch.setattr(api_bridge, "LLMClient", lambda: model)
    monkeypatch.setattr(api_bridge, "ObscuraClient", lambda: FixtureFetcher(pages))
    result = await extract_novel_chapters(catalog[1].url, tmp_path / "raw", state_dir=tmp_path / "state")
    assert result["status"] == "completed", result
    assert domain_memory.get_recipe(catalog[1].url).sample_toc_url == catalog_url
    warm = await api_bridge.discover(catalog[1].url, FixtureFetcher(pages), model)
    assert warm["is_complete"] and warm["toc_url"] == catalog_url
    assert len(warm["chapters"]) == 3
