"""Fixed evidence, staged escalation, and owned evaluation resource contracts."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.agent.ch.analyzer import DOMStructurePlan
from src.agent.ch.candidates import structural_signature
from src.agent.ch.code_generator import ChapterCodeGenerator
from src.agent.ch.review_loop import ReviewLoopOrchestrator
from src.scraper.benchmark import corpus, measure_case
from src.scraper.evaluation import checked_file, digest, load_case, isolated_live, replay
from src.scraper.telemetry import (evaluation_budget, reserve_model_call, ModelBudgetExceeded,
    recovery_ceiling, stage_enabled, traced_job, stage, mark_validated)


def test_frozen_capture_hash_and_path_are_enforced(tmp_path):
    path = tmp_path / 'source.html'
    path.write_text('<p>Source</p>')
    item = {'path': path.name, 'sha256': digest(path)}
    assert checked_file(tmp_path, item) == path
    path.write_text('changed')
    with pytest.raises(ValueError, match='changed'):
        checked_file(tmp_path, item)
    with pytest.raises(ValueError, match='escapes'):
        checked_file(tmp_path, {'path': '../outside', 'sha256': ''})


def test_annotations_require_review_and_three_sample_texts(tmp_path):
    site = {'id': 'example', 'split': 'held_out', 'url': 'https://fixture.test/book',
            'captures': {}, 'catalog': [{'url': 'https://fixture.test/book/1', 'title': 'Chapter 1'}]}
    with pytest.raises(ValueError, match='reviewed'):
        load_case(site, tmp_path)
    site['annotation_status'] = 'reviewed'
    with pytest.raises(ValueError, match='Gold text'):
        load_case(site, tmp_path)


@pytest.mark.asyncio
async def test_deterministic_candidates_precede_model_analysis():
    html = '<h1>Prologue: Home</h1><article><p>A light returned.</p></article>'
    analyzer = SimpleNamespace(analyze=AsyncMock(side_effect=AssertionError('Model ran before deterministic candidates')))
    model = SimpleNamespace(is_available=True)
    result = await ReviewLoopOrchestrator(analyzer=analyzer, llm_client=model).run(html, 'https://fixture.test/chapter-1')
    assert result.approved
    analyzer.analyze.assert_not_awaited()


def test_source_evidence_includes_adjacent_sections_and_inline_ending():
    html = '''<h1>Chapter 1</h1><article><div id="story"><p>The long opening passage.</p>
    <p>Another part of the journey.</p><p>The travellers walked home.</p></div>
    <section>And then <em>the true ending</em> arrived.</section></article>'''
    narrow = ChapterCodeGenerator(DOMStructurePlan(title_selector='h1', content_selector='#story'))
    assert 'actual source ending' in ' '.join(narrow.test_and_verify(html).coverage_issues)
    complete = ChapterCodeGenerator(DOMStructurePlan(title_selector='h1', content_selector='article'))
    assert not complete.test_and_verify(html).coverage_issues


def test_source_boundaries_do_not_cross_another_chapter():
    html = '''<main><article class="story"><h1>Chapter 1</h1><p>The actual ending.</p></article>
    <article><h1>Chapter 2</h1><p>A different chapter.</p></article></main>'''
    plan = DOMStructurePlan(title_selector='h1', content_selector='article.story')
    assert not ChapterCodeGenerator(plan).test_and_verify(html).coverage_issues
    broad = DOMStructurePlan(title_selector='h1', content_selector='main')
    assert any('outside' in issue for issue in ChapterCodeGenerator(broad).test_and_verify(html).coverage_issues)


def test_context_signatures_include_semantics_neighbors_and_distribution():
    signature = structural_signature('<main role="main"><h2>Chapter 21</h2><section class="story" itemprop="articleBody"><p>Hi.</p><p>Longer paragraph.</p></section></main>', '.story')
    assert signature['semantic'] == {'itemprop': 'articleBody'}
    assert signature['ancestor_context'][0]['semantic'] == {'role': 'main'}
    assert signature['neighbor_heading']['template'] == 'Chapter #'
    assert len(signature['paragraph_distribution']) == 3


def test_evaluation_budget_is_shared_and_bounded(tmp_path, monkeypatch):
    ledger = tmp_path / 'budget.json'
    monkeypatch.setenv('NOVEL_SCRAPER_EVALUATION_BUDGET_FILE', str(ledger))
    with evaluation_budget() as first:
        for _ in range(6): reserve_model_call(visual=True)
        with pytest.raises(ModelBudgetExceeded): reserve_model_call(visual=True)
        assert first['visual_calls'] == 6
    with evaluation_budget() as second:
        for _ in range(24): reserve_model_call()
        with pytest.raises(ModelBudgetExceeded): reserve_model_call()
        assert second['model_calls'] == 24
    assert json.loads(ledger.read_text()) == {'model_calls': 30, 'visual_calls': 6}


@pytest.mark.asyncio
async def test_trace_counts_verified_content_once_per_job():
    @traced_job
    async def job():
        for _ in range(2):
            with stage('candidate') as row:
                mark_validated(row, ['chapter:https://fixture.test/1'])
                row.update(outcome='approved', reason='Source verified')
        return {}
    result = await job()
    rows = result['metrics']['recovery_stages']
    assert [row['newly_validated'] for row in rows] == [1, 0]
    assert all(set(['attempts', 'seconds', 'outcome', 'reason']) <= row.keys() for row in rows)


def test_nested_ceiling_cannot_enable_a_disabled_stage(monkeypatch):
    monkeypatch.setenv('NOVEL_SCRAPER_RECOVERY_CEILING', 'browser')
    with recovery_ceiling('repair'):
        assert stage_enabled('browser') and not stage_enabled('model')
        with recovery_ceiling('deterministic'):
            assert not stage_enabled('browser')


@pytest.mark.asyncio
async def test_stage_ablation_uses_identical_gold_with_isolated_recipes(tmp_path, monkeypatch):
    case = next(case for case in corpus() if case['family'] == 'delayed')
    monkeypatch.setenv('NOVEL_SCRAPER_RECOVERY_CEILING', 'deterministic')
    before = await measure_case(case, tmp_path / 'before')
    monkeypatch.setenv('NOVEL_SCRAPER_RECOVERY_CEILING', 'browser')
    after = await measure_case(case, tmp_path / 'after')
    assert not any(row['success'] for row in before)
    assert all(row['success'] for row in after)
    assert all(row['recovery_stages'] for row in after)


@pytest.mark.asyncio
async def test_failed_http_attempt_preserves_redirect_and_status(monkeypatch):
    from src.core.obscura_client import ObscuraClient
    monkeypatch.setenv('MIN_DELAY_SECONDS', '0')
    original = httpx.AsyncClient
    async def respond(request):
        if request.url.path == '/old':
            return httpx.Response(302, headers={'location': '/denied'})
        return httpx.Response(403, text='Forbidden')
    monkeypatch.setattr('src.core.obscura_client.httpx.AsyncClient', lambda **kwargs:
        original(transport=httpx.MockTransport(respond), **kwargs))
    client = ObscuraClient()
    with recovery_ceiling('deterministic'):
        with pytest.raises(RuntimeError, match='ceiling'):
            await client.fetch_html('https://fixture.test/old')
    record = client.fetch_records['https://fixture.test/old']
    assert record.final_url.endswith('/denied') and record.status == 403
    assert record.redirects == ('https://fixture.test/old',) and record.error
    await client.close()


def test_owned_worker_timeout_preserves_partial_and_targets_owned_process(tmp_path, monkeypatch):
    import subprocess
    import src.scraper.evaluation as evaluation
    monkeypatch.setenv('NOVEL_SCRAPER_LIVE_TESTS', '1')
    (tmp_path / 'partial.json').write_text(json.dumps({'chapters': [{'url': 'https://fixture.test/1'}], 'is_complete': False}))
    class Process:
        pid = 87654321
        polls = 0
        def wait(self, timeout):
            self.polls += 1
            if self.polls == 1: raise subprocess.TimeoutExpired('owned-worker', timeout)
            return 0
    monkeypatch.setattr(evaluation.subprocess, 'Popen', lambda *args, **kwargs: Process())
    targets = []
    monkeypatch.setattr(evaluation.subprocess, 'run', lambda args, **kwargs: targets.append(args))
    monkeypatch.setattr(evaluation.os, 'killpg', lambda pid, sig: targets.append(pid), raising=False)
    result = isolated_live({'url': 'https://fixture.test'}, tmp_path, timeout=0.01)
    assert not result['success'] and result['inspection']['chapters']
    expected = [['taskkill', '/PID', '87654321', '/T', '/F']] if evaluation.os.name == 'nt' else [87654321]
    assert targets == expected


@pytest.mark.asyncio
async def test_model_budget_is_enforced_before_provider_invocation():
    from src.agent.llm import LLMClient
    client = LLMClient(api_key='offline-fixture-key')
    client.get_chat_model = lambda **kwargs: pytest.fail('Provider invoked past its budget')
    with evaluation_budget(0, 0):
        with pytest.raises(ModelBudgetExceeded):
            await client.generate_json('Return a plan', DOMStructurePlan)


@pytest.mark.asyncio
async def test_an_ambiguous_landing_page_is_not_a_complete_single_chapter():
    from src.api_bridge import discover
    from src.scraper.benchmark import FixtureFetcher
    url = 'https://fixture.test/novel/home'
    case = {'pages': {url: '<h1>A collection</h1><div><a href="/part-a">Dawn</a><a href="/part-b">Dusk</a></div>'}, 'rendered': {}}
    with recovery_ceiling('deterministic'):
        result = await discover(url, FixtureFetcher(case), SimpleNamespace(is_available=False))
    assert not result['is_complete'] and not result['chapters']
    assert any('ambiguous' in issue for issue in result['issues'])


def test_rank_cache_is_bounded_and_isolated_from_mutation():
    from src.agent.ch.candidates import rank_candidates, _rank_candidates
    _rank_candidates.cache_clear()
    html = '<h1>Prologue</h1><article class="story"><p>A light returned.</p></article>'
    first = rank_candidates(html)
    first[0].signature['classes'].append('poisoned')
    second = rank_candidates(html)
    assert 'poisoned' not in second[0].signature['classes']
    assert _rank_candidates.cache_info().hits == 1
    changed = rank_candidates(html.replace('returned', 'faded'))
    assert changed[0].text != second[0].text
    assert _rank_candidates.cache_info().maxsize == 12


@pytest.mark.asyncio
async def test_replay_retains_unrecorded_retrieval_failure_reasons(tmp_path):
    case = corpus()[0]
    missing = case['gold'][0]['url']
    case['pages'].pop(missing)
    rows = await measure_case(case, tmp_path)
    assert all(not row['success'] for row in rows)
    assert all(any(missing in reason for reason in row['failure_reasons']) for row in rows)
    assert any(event['stage'] == 'retrieval' and event['outcome'] == 'failed'
               for row in rows for event in row['recovery_stages'])


def test_advertisement_containers_are_not_adjacent_story_prose():
    html = '''<h1 class="reader-header-chapter">Chapter 66</h1><div class="reader-wrapper">
    <div class="reader-content"><p>The true ending remained intact.</p>
    <div class="adsterra-banner-container"><span>Advertisement</span></div></div>
    <div class="adsterra-banner-container"><span>Advertisement</span></div></div>'''
    generator = ChapterCodeGenerator(DOMStructurePlan(title_selector='.reader-header-chapter', content_selector='.reader-content'))
    verification = generator.test_and_verify(html)
    assert not verification.coverage_issues
    assert generator.extract(html).content_markdown == 'The true ending remained intact.'
