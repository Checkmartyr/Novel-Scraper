"""Unhydrated readers must not be mistaken for their settings panels."""
import asyncio
import hashlib
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.ch.analyzer import DOMStructurePlan
from src.agent.ch.candidates import rank_candidates, readiness_selector
from src.agent.ch.code_generator import ChapterCodeGenerator
from src.agent.ch.review_loop import ReviewLoopOrchestrator
from src.scraper.runtime import ScraperLimits

SHELL = '''<h1>Chapter 1</h1><div class="chapter-text__place">Loading...</div>
<div id="chapter-settings"><div>Reader settings</div><div>Font size and margins</div>
<label>Font<select><option>Large</option></select></label><button>Apply</button></div>
<div class="bookmark-ph-alert">Sign in to save your position</div>'''
PROSE = ''.join(f'<p>Synthetic passage {i} describes a different event and preserves its ending.</p>' for i in range(8))
READY = SHELL.replace('Loading...', PROSE).replace('chapter-text__place', 'chapter-text random-layout-token')
OFFLINE = SimpleNamespace(is_available=False)


def test_readiness_uses_witnessed_story_instead_of_settings():
    assert readiness_selector(SHELL, '#chapter-settings') == 'div[class*="chapter-text"]'
    assert rank_candidates(SHELL) == []
    assert readiness_selector('<div id="chapter-settings">Settings</div>', '#chapter-settings') is None


@pytest.mark.asyncio
async def test_loading_shell_is_rejected_before_model_analysis():
    model = SimpleNamespace(is_available=True, generate_json=AsyncMock())
    result = await ReviewLoopOrchestrator(llm_client=model).run(SHELL, 'https://fixture.test/chapter/1')
    assert not result.approved and result.plan.content_selector == 'div.chapter-text__place'
    assert 'loading' in result.review.issues[0]
    model.generate_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_hydrated_story_passes_without_comparing_it_to_settings():
    result = await ReviewLoopOrchestrator(llm_client=OFFLINE).run(READY, 'https://fixture.test/chapter/1')
    assert result.approved and not result.verification.coverage_issues
    assert result.plan.content_selector == 'div.chapter-text'
    extracted = ChapterCodeGenerator(result.plan).extract(READY)
    assert 'Synthetic passage 7' in extracted.content_markdown
    assert 'Reader settings' not in extracted.content_markdown
    changed_style = READY.replace('random-layout-token', 'another-style-token')
    assert not ChapterCodeGenerator(result.plan).test_and_verify(changed_style).coverage_issues


@pytest.mark.asyncio
async def test_chapter_renderer_uses_interactive_browser_and_observed_container(monkeypatch):
    from src.core.obscura_client import ObscuraClient
    class Page:
        wait = None
        def on(self, *args): pass
        async def route(self, *args): pass
        async def goto(self, *args, **kwargs):
            assert kwargs['wait_until'] == 'commit'
            return None
        async def content(self): return READY if self.wait else SHELL
        async def wait_for_function(self, expression, **kwargs): self.wait = kwargs['arg']
    page = Page()
    @asynccontextmanager
    async def interactive(**kwargs): yield page
    client = ObscuraClient()
    monkeypatch.setattr(client, 'get_interactive_page', interactive)
    try:
        html = await client.fetch_html('https://fixture.test/chapter/1', force_browser=True,
                                      readiness='chapter', content_selector=readiness_selector(SHELL))
    finally:
        await client.close()
    assert page.wait == 'div[class*="chapter-text"]'
    assert 'Synthetic passage 7' in html


@pytest.mark.asyncio
async def test_failed_preflight_does_not_fetch_every_chapter(tmp_path, monkeypatch):
    from src import api_bridge
    chapters = [{'index': i, 'title': f'Chapter {i}', 'url': f'https://fixture.test/book/chapter-{i}'} for i in range(1, 9)]
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps({'url': 'https://fixture.test/book', 'novel_title': 'Fixture',
                                    'is_complete': True, 'chapters': chapters}), encoding='utf-8')
    client = SimpleNamespace(limits=ScraperLimits(), metrics={}, close=AsyncMock(), fetch_html=AsyncMock(return_value=SHELL))
    monkeypatch.setattr(api_bridge, '_obscura_client', lambda *args: client)
    monkeypatch.setattr(api_bridge, 'LLMClient', lambda: OFFLINE)
    result = await api_bridge.extract_novel_chapters('https://fixture.test/book', tmp_path/'raw',
        state_dir=tmp_path/'state', job_id='preflight', snapshot_file=snapshot)
    assert result['failed'] == 8 and result['files'] == []
    assert len({call.args[0] for call in client.fetch_html.call_args_list}) <= 5
    assert client.fetch_html.await_count <= 10
    assert len(list((tmp_path/'state/checkpoints/preflight').glob('*.json'))) == 8


@pytest.mark.asyncio
async def test_hydrated_batch_exports_verified_synthetic_chapters(tmp_path, monkeypatch):
    from src import api_bridge
    from scripts.download_and_validate import validate_download
    source = 'https://fixture.test/book'
    chapters = [{'index': i, 'title': f'Chapter {i}', 'url': f'{source}/chapter-{i}'} for i in range(1, 9)]
    snapshot = tmp_path/'snapshot.json'
    snapshot.write_text(json.dumps({'url': source, 'novel_title': 'Fixture', 'is_complete': True,
                                    'chapters': chapters}), encoding='utf-8')
    async def fetch(url, **kwargs):
        index = int(url.rsplit('-', 1)[1])
        html = READY if kwargs.get('force_browser') else SHELL
        return html.replace('Chapter 1', f'Chapter {index}').replace('Synthetic passage', f'Synthetic chapter {index} passage')
    client = SimpleNamespace(limits=ScraperLimits(), metrics={}, close=AsyncMock(), fetch_html=AsyncMock(side_effect=fetch))
    monkeypatch.setattr(api_bridge, '_obscura_client', lambda *args: client)
    monkeypatch.setattr(api_bridge, 'LLMClient', lambda: OFFLINE)
    destination = tmp_path/'raw'
    job = hashlib.sha256(source.encode()).hexdigest()[:32]
    result = await api_bridge.extract_novel_chapters(source, destination, job_id=job, snapshot_file=snapshot)
    assert result['status'] == 'completed' and result['completed'] == 8
    assert len(list(destination.glob('*.md'))) == 8
    validation = validate_download(destination)
    assert validation['passed'], validation['errors']


def test_validator_reports_upstream_failure_instead_of_filename_error(tmp_path):
    from scripts.download_and_validate import validate_download
    metadata = {'source_url': 'https://fixture.test/book', 'total_chapters': 1, 'completed_chapters': 0,
                'chapters': [{'index': 1, 'url': 'https://fixture.test/book/chapter-1',
                              'status': 'failed', 'success': False, 'error': 'Story did not hydrate'}]}
    (tmp_path/'metadata.json').write_text(json.dumps(metadata), encoding='utf-8')
    report = validate_download(tmp_path)
    assert report['outcome_counts'] == {'failed': 1}
    assert report['failure_reasons'] == {'Story did not hydrate': 1}
    assert any('Story did not hydrate' in error for error in report['errors'])
    assert not any('invalid Markdown filename' in error for error in report['errors'])
