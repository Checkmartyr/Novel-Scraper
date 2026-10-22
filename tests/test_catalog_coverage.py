"""Layout-independent catalog regressions; all chapter text and browsers are synthetic."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import json
import time

import httpx
import pytest

from src.agent.classifier import ChapterLink
from src.agent.toc.agent import TocAgent
from src.agent.toc.discovery import BrowserAction, BrowserActions, catalog_controls, expand_catalog
from src.agent.toc.graph import TocGraphWorkflow
from src.agent.toc.tools import ClaimInspector, DomLinkExtractor, TocAuditor
from src.scraper.recovery import recovery_scope
from src.scraper.runtime import ScraperLimits, belongs_to_observed_novel
from src.utils.title_cleaner import chapter_number_bounds, extract_chapter_number

URL = "https://fixture.test/book/a-novel"


def links(numbers):
    return '<div class="chapter-list">' + ''.join(
        f'<a href="/book/chapter/{9000-number}">{number} chapter - Journey</a>'
        for number in numbers) + '</div>'


def test_total_claim_ignores_recent_preview_and_noise():
    html = '<h1>Novel</h1><h2>Last 10 chapters</h2><div><span>Loaded Chapters</span><span>120</span></div>'
    html += '<div class="recommendations"><p>999 chapters</p></div><div id="comments"><p>888 chapters</p></div>'
    assert ClaimInspector.inspect(html, URL)[0] == 120
    assert ClaimInspector.explicit_counts(html) == {120}
    assert ClaimInspector.inspect('<h2>Last 10 chapters</h2>' + links([1, 2]), URL)[0] is None
    assert ClaimInspector.inspect('<p>Latest Chapter: 120</p>' + links([1, 2]), URL)[0] is None
    assert ClaimInspector.inspect('<script type="application/json">{"recommendations":{"chapterCount":999}}</script>' + links([1, 2]), URL)[0] is None


def test_conflicting_totals_are_not_a_complete_catalog():
    html = '<p>Total Chapters: 2</p><p>Loaded Chapters: 3</p>' + links([1, 2, 3])
    result = TocAuditor.audit(DomLinkExtractor.extract(html, URL), 3, html)
    assert not result[0] and any('Conflicting' in issue for issue in result[-1])


@pytest.mark.asyncio
async def test_shared_routes_require_exact_catalog_evidence():
    html = links([1, 2]) + '<div class="recommendations">' + links([99]) + '</div>'
    html += '<div class="chapter-list"><a href="/book/another/chapter-3">Chapter 3</a></div>'
    chapters = DomLinkExtractor.extract(html, URL)
    assert len(chapters) == 2
    assert not belongs_to_observed_novel(URL, chapters[0].url)
    with recovery_scope(URL) as scope:
        await scope.observe_catalog(URL, html, SimpleNamespace())
        assert all(belongs_to_observed_novel(URL, chapter.url) for chapter in chapters)
        assert not belongs_to_observed_novel(URL, 'https://fixture.test/book/chapter/unwitnessed')
        assert not belongs_to_observed_novel(URL, 'https://fixture.test/book/another/chapter-3')
        assert not belongs_to_observed_novel(URL, 'https://fixture.test/book/chapter/8901')


@pytest.mark.parametrize('label,expected', [('12 chapter - Journey', 12), ('12.5 episode - Extra', 12.5), ('Chapter 12', 12), ('12 chapter - Chapter 99 recalled', 12)])
def test_labels_take_precedence_over_opaque_ids(label, expected):
    assert extract_chapter_number(label, 'https://fixture.test/book/chapter/987654') == expected
    assert chapter_number_bounds(label) == (expected, expected)


class RangePage:
    def __init__(self, ranges, *, fail=False, tab=False):
        self.ranges = ranges
        self.current = 0
        self.fail = fail
        self.tab = tab
        self.revealed = not tab
        self.actions = []

    def on(self, *args): pass
    async def goto(self, *args, **kwargs): pass
    async def evaluate(self, *args): pass
    async def wait_for_function(self, *args, **kwargs): pass

    async def content(self):
        if not self.revealed:
            return '<a href="#" role="tab">Chapters</a>'
        options = ''.join(f'<option value="{i}" {"selected" if self.current == i else ""}>{i*10+1} - {i*10+10}</option>' for i in range(len(self.ranges)))
        return '<select aria-label="Chapters">' + options + '</select>' + links(self.ranges[self.current])

    def locator(self, selector):
        return SimpleNamespace(first=SimpleNamespace(select_option=self.select, click=self.click))

    async def select(self, value, **kwargs):
        self.actions.append(('select', value))
        if self.fail:
            raise RuntimeError('selection failed')
        self.current = int(value)

    async def click(self, **kwargs):
        self.actions.append(('click', None))
        self.revealed = True


class Browser:
    def __init__(self, page, **limits):
        self.page = page
        self.limits = ScraperLimits(**limits)
        self.metrics = {}

    async def pace(self, *args): pass

    @asynccontextmanager
    async def get_playwright_page(self, **kwargs):
        yield self.page


@pytest.mark.asyncio
@pytest.mark.parametrize('tab', [False, True])
async def test_ranges_survive_unchanged_counts_and_virtualized_views(tab):
    page = RangePage([[3, 4], [3, 4], [1, 2]], tab=tab)
    browser = Browser(page, toc_cycles=10)
    result = await expand_catalog(URL, browser, DomLinkExtractor.extract)
    assert result.exhausted
    assert {chapter.title for chapter in result.chapters} == {f'{i} chapter - Journey' for i in range(1, 5)}
    assert page.actions.count(('select', '1')) == 1
    assert page.actions.count(('select', '2')) == 1
    state = {'url': URL, 'html': result.html, 'extracted_chapters': result.chapters, 'is_complete': True}
    finalized = await TocGraphWorkflow(obscura_client=browser)._node_finalize(state)
    assert [chapter_number_bounds(ch.title)[0] for ch in finalized['extracted_chapters']] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_full_toc_agent_covers_120_entries_across_dropdown_ranges():
    class Page(RangePage):
        async def content(self):
            return '<h1>Fixture Novel</h1><h2>Last 10 chapters</h2><p>Total Chapters: 120</p>' + await super().content()
    page = Page([list(range(start, start+24)) for start in (97, 73, 49, 25, 1)])
    browser = Browser(page)
    async def fetch(*args, **kwargs): return await page.content()
    browser.fetch_html = AsyncMock(side_effect=fetch)
    state = await TocAgent(obscura_client=browser).extract_toc(URL)
    assert state['is_complete']
    assert state['claimed_chapter_count'] == 120
    assert len(state['extracted_chapters']) == 120
    assert [chapter_number_bounds(ch.title)[0] for ch in state['extracted_chapters']] == list(range(1, 121))


@pytest.mark.asyncio
@pytest.mark.parametrize('fail,cycles', [(True, 8), (False, 1)])
async def test_failed_or_budget_limited_ranges_stay_incomplete(fail, cycles):
    page = RangePage([[1], [2], [3]], fail=fail)
    result = await expand_catalog(URL, Browser(page, toc_cycles=cycles), DomLinkExtractor.extract)
    assert not result.exhausted
    assert result.chapters


def test_disabled_options_and_unrelated_controls_are_not_actions():
    controls = catalog_controls('<select aria-label="Chapters"><option>1 - 10</option><option disabled value="bad">11 - 20</option></select><form><button>Show all chapters</button></form><div hidden><select aria-label="Chapters"><option>x</option></select></div><select name="theme"><option>light</option><option>dark</option></select>')
    assert len(controls) == 1
    assert [option['value'] for option in controls[0]['options']] == ['1 - 10']
    assert catalog_controls('<nav role="tablist"><a href="#catalog" role="tab">Chapters</a></nav>')[0]['automatic']
    assert catalog_controls('<button aria-expanded="false">Add to List</button><button aria-expanded="false">Rate it</button>') == []


@pytest.mark.asyncio
async def test_invented_model_selectors_and_values_never_execute():
    class Page(RangePage):
        async def content(self): return '<button id="reveal">Reveal list</button>'
    page = Page([[1]])
    model = SimpleNamespace(is_available=True, generate_json=AsyncMock(return_value=BrowserActions(actions=[
        BrowserAction(action='select', selector='#invented', value='made-up'),
        BrowserAction(action='select', selector='html:nth-of-type(1) > body:nth-of-type(1) > button:nth-of-type(1)', value='bad')
    ])))
    await expand_catalog(URL, Browser(page, toc_cycles=3), DomLinkExtractor.extract, model)
    assert page.actions == []
    assert 'Select values must exactly match supplied enabled options' in model.generate_json.call_args.kwargs['system_instruction']


@pytest.mark.asyncio
async def test_css_hidden_controls_do_not_block_visible_ranges():
    class Page(RangePage):
        async def content(self):
            return '<button style="display:none">Show all chapters</button>' + await super().content()
        async def evaluate(self, expression):
            if '.filter(selector' in expression:
                return [control['selector'] for control in catalog_controls(await self.content()) if control['action'] == 'select']
    page = Page([[1], [2]])
    result = await expand_catalog(URL, Browser(page), DomLinkExtractor.extract)
    assert result.exhausted and len(result.chapters) == 2
    assert ('click', None) not in page.actions


@pytest.mark.asyncio
async def test_runtime_selected_value_overrides_stale_html_attribute():
    class Page(RangePage):
        async def content(self):
            return (await super().content()).replace(' selected', '')
        async def evaluate(self, expression):
            if '.filter(selector' in expression:
                return [{'selector': control['selector'], 'value': str(self.current)}
                        for control in catalog_controls(await self.content())]
    page = Page([[1], [2], [3]])
    page.current = 2
    result = await expand_catalog(URL, Browser(page), DomLinkExtractor.extract)
    assert result.exhausted and len(result.chapters) == 3
    assert ('select', '0') in page.actions


@pytest.mark.asyncio
async def test_styled_native_select_uses_only_enabled_witnessed_options():
    class Page(RangePage):
        async def content(self):
            return (await super().content()).replace('<select ', '<select style="display:none" ')
        async def select(self, value, **kwargs):
            assert kwargs['force'] is True
            assert value in {'0', '1'}
            await super().select(value, **kwargs)
    page = Page([[1], [2]])
    result = await expand_catalog(URL, Browser(page), DomLinkExtractor.extract)
    assert result.exhausted and len(result.chapters) == 2


@pytest.mark.asyncio
async def test_native_select_fallback_is_fixed_and_rechecks_option_evidence():
    class Page(RangePage):
        async def select(self, value, **kwargs):
            raise RuntimeError('option.label is unavailable')
        async def evaluate(self, expression, argument=None):
            if argument is not None:
                assert "option.hasAttribute('disabled')" in expression
                assert "new Event('change'" in expression
                assert argument['value'] in {'0', '1'}
                self.current = int(argument['value'])
                self.actions.append(('fallback', argument['value']))
                return True
    page = Page([[1], [2]])
    result = await expand_catalog(URL, Browser(page), DomLinkExtractor.extract)
    assert result.exhausted and len(result.chapters) == 2
    assert ('fallback', '1') in page.actions


@pytest.mark.asyncio
async def test_stalled_catalog_navigation_obeys_deadline():
    class Page(RangePage):
        async def goto(self, *args, **kwargs): await asyncio.sleep(60)
    browser = Browser(Page([[1]]))
    browser.discovery_deadline = time.monotonic() + 0.02
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await expand_catalog(URL, browser, DomLinkExtractor.extract)
    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_incomplete_catalog_pauses_before_any_chapter_fetch(tmp_path, monkeypatch):
    from src import api_bridge
    client = SimpleNamespace(metrics={}, close=AsyncMock(), fetch_html=AsyncMock())
    monkeypatch.setattr(api_bridge, '_obscura_client', lambda *args: client)
    inspection = {'url': URL, 'novel_title': 'Fixture', 'is_complete': False,
                  'claimed_chapter_count': 120, 'chapters': [ChapterLink(index=1, title='Chapter 1', url=URL+'/1').model_dump()],
                  'issues': ['Unvisited chapter ranges']}
    discovery = AsyncMock(return_value=inspection)
    monkeypatch.setattr(api_bridge, 'discover', discovery)
    result = await api_bridge.extract_novel_chapters(URL, tmp_path/'novel', state_dir=tmp_path/'state', job_id='catalog')
    assert result['status'] == 'paused' and not result['is_complete']
    assert result['total_chapters'] == 1 and result['claimed_chapter_count'] == 120
    assert result['files'] == []
    client.fetch_html.assert_not_awaited()
    snapshot = json.loads((tmp_path/'state/snapshots/catalog.json').read_text())
    assert snapshot['issues'] == inspection['issues']
    await api_bridge.extract_novel_chapters(URL, tmp_path/'novel', state_dir=tmp_path/'state', job_id='catalog')
    assert discovery.await_count == 2
    assert not list(tmp_path.rglob('*.md'))


@pytest.mark.asyncio
async def test_terminal_ui_cannot_start_with_incomplete_catalog():
    from src.ui.app import NovelScraperApp
    app = SimpleNamespace(toc_state={'is_complete': False}, log_msg=lambda *args: None)
    await NovelScraperApp.action_start_batch(app)
    assert not hasattr(app, 'batch_runner')


@pytest.mark.asyncio
@pytest.mark.parametrize('inspection', [
    {'success': True, 'url': URL, 'novel_title': 'Fixture', 'chapters': [], 'is_complete': False},
    {'success': False, 'url': URL, 'error': 'DNS lookup failed', 'chapters': [], 'is_complete': False},
])
async def test_headless_cli_stops_after_incomplete_inspection(tmp_path, monkeypatch, inspection):
    from src import api_bridge, main
    monkeypatch.setattr(api_bridge, 'inspect_novel_url', AsyncMock(return_value=inspection))
    extraction = AsyncMock()
    monkeypatch.setattr(api_bridge, 'extract_novel_chapters', extraction)
    monkeypatch.setattr(main, 'NovelStorage', lambda: SimpleNamespace(get_novel_dir=lambda title: tmp_path))
    monkeypatch.setattr(main, 'setup_file_logging', lambda **kwargs: None)
    result = await main.run_headless(URL)
    assert result['status'] == 'paused'
    extraction.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspection_reports_dns_failure_without_chapter_requests(monkeypatch):
    from src import api_bridge
    client = SimpleNamespace(metrics={}, close=AsyncMock(), fetch_html=AsyncMock(side_effect=httpx.ConnectError('DNS lookup failed')))
    monkeypatch.setattr(api_bridge, '_obscura_client', lambda *args: client)
    events = []
    result = await api_bridge.inspect_novel_url(URL, on_progress=events.append)
    assert not result['is_complete'] and 'DNS' in result['error']
    assert any('failed' in event['message'] for event in events)
    assert client.fetch_html.await_count == 1


@pytest.mark.asyncio
async def test_http_connection_failure_reaches_bounded_browser_fallback(monkeypatch):
    from src.core.obscura_client import ObscuraClient
    client = ObscuraClient()
    attempts = []
    async def fail(request):
        attempts.append(request.url)
        raise httpx.ConnectError('DNS lookup failed')
    client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    client.pace = AsyncMock()
    @asynccontextmanager
    async def no_browser(**kwargs):
        raise RuntimeError('Browser unavailable')
        yield
    monkeypatch.setattr(client, 'get_playwright_page', no_browser)
    events = []
    client.on_progress = events.append
    try:
        with pytest.raises(RuntimeError, match='Browser unavailable'):
            await client.fetch_html(URL)
    finally:
        await client.close()
    assert len(attempts) == client.limits.request_attempts
    assert any('browser fallback' in event['message'] for event in events)


def test_timeout_retains_total_and_pending_controls():
    from src.api_bridge import partial_discovery
    chapter = ChapterLink(index=1, title='Chapter 1', url=URL+'/chapter-1')
    pending = [{'action': 'select', 'selector': '#ranges', 'value': 'next'}]
    client = SimpleNamespace(last_toc_state={'claimed_chapter_count': 120, 'extracted_chapters': [chapter], 'pending_catalog_controls': pending})
    result = partial_discovery(URL, client)
    assert result['claimed_chapter_count'] == 120
    assert result['pending_catalog_controls'] == pending
    assert not result['is_complete']


@pytest.mark.asyncio
@pytest.mark.parametrize('supported', [True, False])
async def test_catalog_browser_fallback_is_capability_based_and_session_scoped(supported):
    from src.core.obscura_client import ObscuraClient
    client = ObscuraClient()
    old_page = SimpleNamespace(evaluate=AsyncMock(return_value=supported))
    native_page = SimpleNamespace(close=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=native_page), close=AsyncMock())
    native_browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    launch = AsyncMock(return_value=native_browser)
    client._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=AsyncMock())
    client._storage_state = '/private/session.json'
    client._page_limits['fixture.test'] = asyncio.Semaphore(1)
    @asynccontextmanager
    async def existing(**kwargs): yield old_page
    client.get_playwright_page = existing
    async with client.get_catalog_page(origin='fixture.test') as page:
        assert page is (old_page if supported else native_page)
    if supported:
        launch.assert_not_awaited()
    else:
        launch.assert_awaited_once_with(channel='chrome', headless=True, timeout=10000)
        native_browser.new_context.assert_awaited_once_with(storage_state='/private/session.json')
        native_page.close.assert_awaited_once()
        assert client.metrics['browser_launches'] == 1
    await client.close()
    if not supported:
        native_browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_standard_browser_retains_original_bounded_recovery():
    from src.core.obscura_client import ObscuraClient
    client = ObscuraClient()
    page = SimpleNamespace(evaluate=AsyncMock(return_value=False))
    launch = AsyncMock(side_effect=RuntimeError('Browser not installed'))
    client._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=AsyncMock())
    @asynccontextmanager
    async def existing(**kwargs): yield page
    client.get_playwright_page = existing
    async with client.get_catalog_page(origin='fixture.test') as result:
        assert result is page
    assert launch.await_count == 3
    await client.close()
