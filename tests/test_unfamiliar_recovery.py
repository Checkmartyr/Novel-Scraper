"""Recovery acceptance uses independent gold content, never error-as-success."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from langchain_core.messages import HumanMessage
import pytest

from src.agent.ch.analyzer import ChapterAnalyzer, DOMStructurePlan
from src.agent.ch.candidates import rank_candidates
from src.agent.ch.code_generator import ChapterCodeGenerator
from src.agent.ch.observer import ExtractionObserver
from src.agent.ch.review_loop import ReviewLoopOrchestrator
from src.agent.ch.self_healer import SelfHealer
from src.agent.llm.client import ChatGeminiInteractions
from src.agent.toc.discovery import BrowserActions, expand_catalog
from src.agent.toc.tools import DomLinkExtractor
from src.api_bridge import discover
from src.core.obscura_client import ObscuraClient
from src.scraper.benchmark import corpus, FixtureFetcher, run_benchmark
from src.scraper.recovery import before_deadline, recovery_scope
from src.scraper.runtime import belongs_to_observed_novel, ScraperLimits


OFFLINE = SimpleNamespace(is_available=False, last_interaction_id=None)


def test_browser_retry_waits_for_story_instead_of_rejected_navigation_plan():
    from src.agent.ch.candidates import readiness_selector
    html = '<header><div class="navbar-inner">NovelBin</div></header><div class="reader-content"><div class="skeleton"></div></div>'
    assert readiness_selector(html, "div.navbar-inner") == ".reader-content"
    assert readiness_selector('<nav><div id="menu">Menu</div></nav>', "#menu") is None


def test_wordpress_catalog_ignores_comments_and_site_navigation():
    from src.agent.toc.tools import ClaimInspector, TocAuditor
    html = '''<h1>Site Name</h1><nav><button aria-expanded="false">Menu</button></nav>
    <div id="actionbar"><button aria-expanded="false">Follow</button></div>
    <article><h1 class="entry-title">Novel Title</h1><div class="entry-content">
    <a href="/2020/prologue/">Prologue: Arrival</a><a href="/2020/chapter-1/">Chapter 1: Arrival</a>
    <a href="/2020/chapter-2/">Chapter 2: Home</a></div></article>
    <div id="comments"><div class="comment-author">Reader</div>
    <a href="/2020/chapter-999/">Chapter 999</a></div>'''
    chapters = DomLinkExtractor.extract(html, "https://fixture.test/novel-title/")
    assert [chapter.title for chapter in chapters] == ['Prologue: Arrival', 'Chapter 1: Arrival', 'Chapter 2: Home']
    _, title, author, _ = ClaimInspector.inspect(html, "https://fixture.test/novel-title/")
    assert title == 'Novel Title' and author is None
    assert TocAuditor.audit(chapters, None, html)[0]
    from src.agent.classifier import PageClassifier
    classifier = PageClassifier(llm_client=OFFLINE)
    result = classifier._heuristic_classify("https://fixture.test/novel-title/", html,
                                          classifier._extract_page_summary(html, "https://fixture.test/novel-title/"))
    assert result.author is None and result.novel_title == 'Novel Title'


@pytest.mark.asyncio
async def test_parked_migration_is_an_explicit_blocker(monkeypatch):
    from src.scraper.runtime import access_error
    page = '<title>Redirecting...</title><script>fetch("https://router.parklogic.com/chapter")</script><div>Loading...</div>'
    assert 'parking redirect' in access_error(page)
    case = next(case for case in corpus() if case['family'] == 'wordpress-migration')
    fetcher = FixtureFetcher(case)
    original = fetcher.fetch_html
    async def fetch(url, **kwargs):
        if url != case['url']:
            raise PermissionError(access_error(page))
        return await original(url, **kwargs)
    fetcher.fetch_html = fetch
    result = await discover(case['url'], fetcher, OFFLINE)
    assert not result['is_complete'] and result['observed_linked_entry_count'] == 5
    assert result['highest_observed_chapter_number'] == 5
    assert all('parking redirect' in issue for issue in result['source_scope']['unresolved'].values())


@pytest.mark.asyncio
async def test_migrated_catalog_requires_reciprocal_evidence_and_is_job_local():
    case = next(case for case in corpus() if case["family"] == "wordpress-migration")
    fetcher = FixtureFetcher(case)
    result = await discover(case["url"], fetcher, OFFLINE)
    assert result["is_complete"] and result["linked_entry_count"] == 5
    assert result["highest_chapter_number"] == 5
    assert {chapter["url"] for chapter in result["chapters"]} == {item["url"] for item in case["gold"]}
    assert result["source_scope"]["relationships"][0]["kind"] == "catalog_backlinks"
    assert not belongs_to_observed_novel(case["url"], case["gold"][0]["url"])


@pytest.mark.asyncio
async def test_failed_migration_proof_stays_incomplete():
    case = next(case for case in corpus() if case["family"] == "wordpress-migration")
    target = case["gold"][0]["url"]
    case["pages"][target] = case["pages"][target].replace(case["url"], "https://unrelated.test/other")
    result = await discover(case["url"], FixtureFetcher(case), OFFLINE)
    assert not result["is_complete"]
    assert any("cross-domain" in issue or "migrated" in issue for issue in result["issues"])
    assert not result["chapters"]


@pytest.mark.asyncio
async def test_migration_checks_all_volume_groups():
    case = next(case for case in corpus() if case["family"] == "wordpress-migration")
    first, rest = case["gold"][:2], case["gold"][2:]
    group = lambda entries: '<div class="chapter-list">' + ''.join(
        f'<a href="{item["url"]}">{item["title"]}</a>' for item in entries) + '</div>'
    case["pages"][case["url"]] = '<h1>Homecoming</h1><p>5 Chapters</p>' + group(first) + group(rest)
    result = await discover(case["url"], FixtureFetcher(case), OFFLINE)
    assert result["is_complete"] and result["linked_entry_count"] == 5


def test_two_in_story_references_do_not_turn_a_chapter_into_a_catalog():
    from src.agent.classifier import PageClassifier
    html = '<h1>Chapter 7</h1><article><p>This is story prose, remembering our past adventures.</p><a href="/novel/home/chapter-1">Chapter 1</a><a href="/novel/home/chapter-2">Chapter 2</a></article>'
    url = "https://fixture.test/novel/home/chapter-7"
    classifier = PageClassifier(llm_client=OFFLINE)
    assert classifier._heuristic_classify(url, html, classifier._extract_page_summary(html, url)).page_type == "CHAPTER"


@pytest.mark.asyncio
async def test_concurrent_job_scopes_do_not_share_approved_links():
    async def job(identifier):
        source = f"https://source.test/novel/{identifier}"
        target = f"https://moved.test/{identifier}/chapter-1"
        with recovery_scope(source) as scope:
            scope.approved_links.add(target)
            await asyncio.sleep(0)
            assert belongs_to_observed_novel(source, target)
            assert not belongs_to_observed_novel(source, f"https://moved.test/{3-identifier}/chapter-1")
    await asyncio.gather(job(1), job(2))


@pytest.mark.asyncio
async def test_http_fetch_retains_redirect_url_and_headers(monkeypatch):
    monkeypatch.setenv("MIN_DELAY_SECONDS", "0")
    case = corpus()[0]
    target = case["url"].replace("static.test", "moved.test")
    async def respond(request):
        if request.url.host == "static.test":
            return httpx.Response(301, headers={"location": target})
        return httpx.Response(200, text=case["pages"][case["url"]], headers={"x-source": "fixture"})
    client = ObscuraClient()
    client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True)
    try:
        with recovery_scope(case["url"], client) as scope:
            await client.fetch_html(case["url"])
            record = client.fetch_records[case["url"]]
            assert record.requested_url == case["url"]
            assert record.final_url == target
            assert record.headers["x-source"] == "fixture"
            assert record.redirects == (case["url"],)
            assert scope.relationships[0]["kind"] == "redirect"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wordpress_recovery_uses_only_advertised_document(monkeypatch):
    monkeypatch.setenv("MIN_DELAY_SECONDS", "0")
    requested = "https://wordpress.test/book"
    resource = "https://wordpress.test/wp-json/wp/v2/pages/7"
    shell = f'<html><link rel="alternate" type="application/json" href="{resource}"><article class="entry-content"></article></html>' + " " * 300
    calls = []
    async def respond(request):
        calls.append(str(request.url))
        if str(request.url) == requested:
            return httpx.Response(200, text=shell)
        assert str(request.url) == resource
        return httpx.Response(200, json={"link": requested, "title": {"rendered": "Homecoming"},
            "content": {"rendered": '<p>The story returned intact.</p>'}})
    client = ObscuraClient()
    client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        html = await client.fetch_html(requested)
        assert "The story returned intact." in html
        assert client.fetch_records[requested].transport == "wordpress_api"
        assert calls == [requested, resource]
        assert await client.fetch_advertised_document(requested, "<p>No advertised API</p>") is None
        assert calls == [requested, resource]
    finally:
        await client.close()


def story_html():
    return '<h1>Chapter 1: Home</h1><div id="chapter-content"><section class="preface"><p>' + 'The dawn was quiet. ' * 20 + '</p></section><section class="body"><p>' + 'A traveller walked through the old courtyard. ' * 20 + '</p></section><div class="ending"><p>The true ending remained beneath the morning stars.</p></div></div><footer>Copyright</footer>'


@pytest.mark.asyncio
async def test_model_removal_cannot_hide_the_missing_ending_from_audit():
    html = story_html()
    plan = DOMStructurePlan(title_selector="h1", content_selector="#chapter-content", remove_selectors=[".ending"])
    verification = ChapterCodeGenerator(plan).test_and_verify(html)
    assert "The true ending" in verification.source_tail
    assert any("ending" in issue for issue in verification.coverage_issues)
    model = SimpleNamespace(is_available=True, generate_json=AsyncMock())
    review = await ExtractionObserver(model).review("https://fixture.test/chapter-1", html, plan, verification)
    assert not review.is_accurate
    model.generate_json.assert_not_awaited()


def test_independent_evidence_detects_changed_order_and_duplicate_blocks():
    html = story_html()
    plan = DOMStructurePlan(title_selector="h1", content_selector="#chapter-content")
    generator = ChapterCodeGenerator(plan)
    actual = generator.extract(html)
    parts = actual.content_markdown.split("\n\n")
    reordered = actual.model_copy(update={"content_markdown": "\n\n".join(reversed(parts))})
    assert any("order" in issue for issue in generator.test_and_verify(html, reordered).coverage_issues)
    duplicated = actual.model_copy(update={"content_markdown": actual.content_markdown + "\n\n" + parts[0]})
    assert any("duplicates" in issue for issue in generator.test_and_verify(html, duplicated).coverage_issues)


@pytest.mark.asyncio
async def test_candidate_cascade_recovers_a_partial_story_plan():
    html = story_html()
    narrow = DOMStructurePlan(title_selector="h1", content_selector=".preface")
    analyzer = SimpleNamespace(analyze=AsyncMock(return_value=narrow), refine=AsyncMock(return_value=narrow))
    result = await ReviewLoopOrchestrator(analyzer=analyzer, llm_client=OFFLINE).run(html, "https://fixture.test/chapter-1")
    assert result.approved
    assert "The true ending" in result.verification.preview_markdown
    assert not result.verification.coverage_issues
    assert rank_candidates(html)[0].selector == "div#chapter-content"


@pytest.mark.asyncio
async def test_contextual_repair_reuses_approved_container_signature():
    from src.agent.domain_memory import domain_memory
    url = "https://fixture.test/novel/home/chapter-1"
    old = story_html()
    plan = DOMStructurePlan(title_selector="h1", content_selector="#chapter-content")
    remembered = domain_memory.save_recipe(url, "https://fixture.test/novel/home", url, "dom_heuristic", plan, sample_html=old)
    assert remembered.chapter_config.structural_signature["tag"] == "div"
    changed = old.replace('id="chapter-content"', 'class="story-new"')
    analyzer = SimpleNamespace(analyze=AsyncMock(side_effect=AssertionError("Contextual recovery should run first")))
    recovered, repaired, extracted = await SelfHealer(analyzer=analyzer, llm_client=OFFLINE).attempt_heal(url, changed, plan)
    assert recovered and extracted.success
    assert "story-new" in repaired.content_selector
    analyzer.analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_deadline_does_not_wait_forever_for_cancellation():
    finish = asyncio.Event()
    async def stubborn():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await finish.wait()
    start = time.monotonic()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await before_deadline(stubborn(), start + 0.01)
        assert time.monotonic() - start < 0.5
    finally:
        finish.set()
        await asyncio.sleep(0)


def test_maximum_recovery_profile_is_finite_and_overridable(monkeypatch):
    monkeypatch.setenv("NOVEL_SCRAPER_RECOVERY_MODE", "maximum")
    assert ScraperLimits.from_env().discovery_seconds == 900
    assert ScraperLimits.from_env().candidate_limit == 10
    monkeypatch.setenv("NOVEL_SCRAPER_DISCOVERY_SECONDS", "17")
    assert ScraperLimits.from_env().discovery_seconds == 17


@pytest.mark.asyncio
async def test_combined_ranges_are_not_confused_with_catalog_entry_count():
    source = "https://fixture.test/novel/home"
    html = '<h1>Homecoming</h1><div class="chapter-list"><a href="/novel/home/chapter-1">Chapter 1-2: Opening</a><a href="/novel/home/chapter-3">Chapter 3-4: Journey</a><a href="/novel/home/chapter-5">Chapter 5.5: Epilogue</a></div>'
    fetcher = FixtureFetcher({"pages": {source: html}, "rendered": {}})
    result = await discover(source, fetcher, OFFLINE)
    assert result["is_complete"]
    assert result["linked_entry_count"] == 3
    assert result["highest_chapter_number"] == 5.5
    assert result["claimed_chapter_count"] is None
    assert result["numbered_chapter_ranges"][0]["end"] == 2


def test_gemini_message_conversion_preserves_image_blocks():
    encoded = base64.b64encode(b"fixture-image").decode("ascii")
    model = ChatGeminiInteractions(api_key="test")
    _, converted = model._convert_messages([HumanMessage(content=[
        {"type": "text", "text": "Choose an observed catalog control"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
    ])])
    assert converted == [{"type": "text", "text": "Choose an observed catalog control"},
                         {"type": "image", "mime_type": "image/png", "data": encoded}]


@pytest.mark.asyncio
async def test_visual_recovery_selects_only_a_fresh_witnessed_control():
    class Page:
        expanded = False
        def on(self, *args): pass
        async def goto(self, *args, **kwargs): pass
        async def content(self):
            if not self.expanded:
                return '<button aria-label="Reveal list">Reveal list</button>'
            return '<button disabled>Reveal list</button>' + ''.join(
                f'<a href="/book/{i}">Chapter {i}</a>' for i in range(1, 4))
        async def screenshot(self, **kwargs): return b"observed-screenshot"
        async def evaluate(self, expression): return 0
        async def wait_for_function(self, *args, **kwargs): pass
        def locator(self, selector):
            assert selector.endswith("button:nth-of-type(1)")
            return SimpleNamespace(first=SimpleNamespace(click=self.click))
        async def click(self, **kwargs): self.expanded = True
    page = Page()
    class Browser:
        limits = ScraperLimits()
        metrics = {}
        async def pace(self, *args): pass
        @asynccontextmanager
        async def get_playwright_page(self, **kwargs): yield page
    model = SimpleNamespace(is_available=True, supports_vision=True)
    async def propose(**kwargs):
        if kwargs.get("images"):
            assert kwargs["images"] == [b"observed-screenshot"]
            controls = json.loads(kwargs["prompt_text"].split("\n")[-1])
            return BrowserActions(actions=[{"action": "click", "selector": controls[0]["selector"]},
                                          {"action": "click", "selector": "#invented"}])
        return BrowserActions(actions=[{"action": "stop"}])
    model.generate_json = AsyncMock(side_effect=propose)
    browser = Browser()
    result = await expand_catalog("https://fixture.test/book", browser, DomLinkExtractor.extract, model)
    assert len(result.chapters) == 3
    assert result.exhausted
    assert browser.metrics["visual_calls"] == 1


@pytest.mark.asyncio
async def test_expansion_reobserves_dom_after_each_click():
    class Page:
        stage = 0
        clicks = 0
        def on(self, *args): pass
        async def goto(self, *args, **kwargs): pass
        async def content(self):
            buttons = '<button>Load more</button>' * (2 - self.stage)
            return buttons + ''.join(f'<a href="/book/{i}">Chapter {i}</a>' for i in range(1, self.stage + 2))
        def locator(self, selector):
            assert selector.endswith("button:nth-of-type(1)")
            return SimpleNamespace(first=SimpleNamespace(click=self.click))
        async def click(self, **kwargs):
            self.clicks += 1
            self.stage += 1
        async def evaluate(self, expression): return 0
        async def wait_for_function(self, *args, **kwargs): pass
    page = Page()
    class Browser:
        limits = ScraperLimits()
        async def pace(self, *args): pass
        @asynccontextmanager
        async def get_playwright_page(self, **kwargs): yield page
    result = await expand_catalog("https://fixture.test/book", Browser(), DomLinkExtractor.extract)
    assert page.clicks == 2
    assert len(result.chapters) == 3
    assert result.exhausted


@pytest.mark.asyncio
async def test_synthetic_benchmark_recovers_more_without_false_completion():
    baseline = json.loads((Path(__file__).resolve().parents[1] / "docs/benchmarks/unfamiliar-baseline.json").read_text())
    report = await run_benchmark()
    assert report["corpus_sha256"] == baseline["corpus_sha256"]
    assert report["success_rate"] > baseline["success_rate"]
    assert report["false_completions"] == 0
    assert all(row["success"] for row in report["runs"])
