import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.download_and_validate import run, validate_download


def test_main_uses_hardcoded_settings_without_arguments(monkeypatch):
    from scripts import download_and_validate as launcher
    agent = AsyncMock(return_value=0)
    monkeypatch.setattr(launcher, 'run', agent)
    monkeypatch.setattr(launcher, 'URL', 'https://fixture.test/book')
    monkeypatch.setattr(launcher, 'CONCURRENCY', 2)
    monkeypatch.setattr(launcher, 'REFRESH', True)
    assert launcher.main() == 0
    args = agent.call_args.args[0]
    assert args.url == 'https://fixture.test/book'
    assert args.concurrency == 2 and args.refresh
    assert args.validate_only is None


def make_download(folder: Path):
    source = 'https://fixture.test/novel/example'
    job = hashlib.sha256(source.encode()).hexdigest()[:32]
    state = folder / '.scraper'
    receipts = state / ('sources-' + hashlib.sha256(str(folder.resolve()).encode()).hexdigest()[:12]) / 'receipts'
    receipts.mkdir(parents=True)
    (state / 'snapshots').mkdir()
    chapters, records = [], []
    for index in (1, 2):
        url = source + f'/chapter-{index}'
        name = f'{index:04d} - Chapter {index}.md'
        text = f'# Chapter {index}\n\nThis is synthetic chapter {index}.\n'
        (folder / name).write_text(text, encoding='utf-8', newline='')
        digest = hashlib.sha256((folder / name).read_bytes()).hexdigest()
        chapters.append({'index': index, 'url': url, 'title': f'Chapter {index}'})
        records.append({'index': index, 'url': url, 'file': name, 'sha256': digest, 'success': True, 'status': 'saved'})
        (receipts / (hashlib.sha256(url.encode()).hexdigest() + '.json')).write_text(json.dumps(
            {'source_url': url, 'file': name, 'sha256': digest, 'state': 'committed'}), encoding='utf-8')
    (state / 'snapshots' / f'{job}.json').write_text(json.dumps({'url': source, 'is_complete': True, 'chapters': chapters}), encoding='utf-8')
    (folder / 'metadata.json').write_text(json.dumps({'source_url': source, 'total_chapters': 2, 'completed_chapters': 2, 'chapters': records}), encoding='utf-8')
    return source


def test_validator_accepts_complete_synthetic_download(tmp_path):
    make_download(tmp_path)
    report = validate_download(tmp_path)
    assert report['passed'] and report['files_checked'] == 2


def test_validator_detects_changed_missing_and_untracked_files(tmp_path):
    make_download(tmp_path)
    (tmp_path / '0001 - Chapter 1.md').write_text('# Chapter 1\n\nChanged', encoding='utf-8')
    (tmp_path / '0002 - Chapter 2.md').unlink()
    (tmp_path / 'extra.md').write_text('Untracked', encoding='utf-8')
    report = validate_download(tmp_path)
    assert not report['passed']
    assert any('hash' in error for error in report['errors'])
    assert any('not successfully saved' in error for error in report['errors'])
    assert any('Untracked' in error for error in report['errors'])


def test_validator_requires_complete_catalog_and_matching_order(tmp_path):
    make_download(tmp_path)
    metadata_path = tmp_path / 'metadata.json'
    metadata = json.loads(metadata_path.read_text())
    metadata['chapters'].reverse()
    metadata['chapters'][0]['index'] = 1
    metadata['chapters'][1]['index'] = 2
    metadata_path.write_text(json.dumps(metadata), encoding='utf-8')
    snapshot_path = next((tmp_path / '.scraper/snapshots').glob('*.json'))
    snapshot = json.loads(snapshot_path.read_text())
    snapshot['is_complete'] = False
    snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')
    report = validate_download(tmp_path)
    assert not report['passed']
    assert any('complete catalog' in error for error in report['errors'])
    assert any('index differs' in error for error in report['errors'])


@pytest.mark.asyncio
async def test_launcher_reuses_agent_and_publishes_validation_report(tmp_path, monkeypatch):
    from src import main
    source = make_download(tmp_path)
    agent = AsyncMock(return_value={'status': 'completed', 'output_dir': str(tmp_path)})
    monkeypatch.setattr(main, 'run_headless', agent)
    args = SimpleNamespace(url=source, validate_only=None, concurrency=2, refresh=False, overwrite=False)
    assert await run(args) == 0
    agent.assert_awaited_once_with(source, concurrency=2, refresh=False, overwrite=False)
    assert json.loads((tmp_path / 'validation-report.json').read_text())['passed']
    assert (tmp_path / 'download-result.json').is_file()
