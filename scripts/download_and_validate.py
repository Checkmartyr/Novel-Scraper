"""Run the existing agent, then check its catalog, Markdown, hashes and receipts.

Edit the settings below, then run:
    uv run python scripts/download_and_validate.py

Rerun the same URL to resume. Validation is local and never fetches chapter text.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

# Edit these settings. Rerunning resumes the same download.
URL = 'https://novelight.net/book/of-steel-and-roses-silver-haired-loli-on-a-rampage'
CONCURRENCY = 3
REFRESH = False
OVERWRITE = False
# Set to a downloaded novel folder to validate without downloading.
VALIDATE_ONLY = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def validate_download(directory: Path) -> dict:
    from src.scraper.runtime import canonical_url

    directory = directory.resolve()
    errors = []
    metadata = json.loads((directory / 'metadata.json').read_text(encoding='utf-8'))
    source = canonical_url(metadata['source_url'])
    job = hashlib.sha256(source.encode()).hexdigest()[:32]
    state = directory / '.scraper'
    snapshots = [state / kind / f'{job}.json' for kind in ('snapshots', 'inspections')]
    snapshot_path = next((path for path in snapshots if path.is_file()), None)
    snapshot = json.loads(snapshot_path.read_text(encoding='utf-8')) if snapshot_path else {}
    if not snapshot.get('is_complete') or canonical_url(snapshot.get('url', '')) != source:
        errors.append('A complete catalog snapshot for this source is required')
    catalog = snapshot.get('chapters', [])
    expected = {canonical_url(chapter['url']) for chapter in catalog}
    if not expected or len(expected) != len(catalog):
        errors.append('Catalog is empty or contains duplicate chapter URLs')
    records = metadata.get('chapters', [])
    actual = [canonical_url(record['url']) for record in records]
    if set(actual) != expected or len(actual) != len(expected):
        errors.append('Saved chapter outcomes do not match the catalog')
    if metadata.get('total_chapters') != len(expected) or metadata.get('completed_chapters') != len(expected):
        errors.append('Metadata counts do not match the complete catalog')
    if sorted(record['index'] for record in records) != list(range(1, len(expected) + 1)):
        errors.append('Chapter indices are missing or duplicated')
    catalog_indices = {canonical_url(chapter['url']): chapter['index'] for chapter in catalog}
    receipt_dir = state / ('sources-' + hashlib.sha256(str(directory).encode()).hexdigest()[:12]) / 'receipts'
    bodies, filenames = {}, set()
    verified = 0
    for record in records:
        label = f"Chapter {record['index']}"
        if record['index'] != catalog_indices.get(canonical_url(record['url'])):
            errors.append(f'{label}: index differs from the catalog')
        name = record.get('file', '')
        path = (directory / name).resolve()
        if not name or path.parent != directory or path.suffix.lower() != '.md' or name in filenames:
            errors.append(f'{label}: missing, duplicate, or invalid Markdown filename')
            continue
        filenames.add(name)
        if not record.get('success') or record.get('status') not in {'saved', 'skipped'} or not path.is_file():
            errors.append(f'{label}: chapter was not successfully saved')
            continue
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        receipt_path = receipt_dir / (hashlib.sha256(canonical_url(record['url']).encode()).hexdigest() + '.json')
        receipt = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.is_file() else {}
        if (digest != record.get('sha256') or digest != receipt.get('sha256') or
                receipt.get('source_url') != canonical_url(record['url']) or
                receipt.get('file') != name or receipt.get('state') != 'committed'):
            errors.append(f'{label}: file hash or source receipt does not match')
        text = raw.decode('utf-8-sig').strip()
        if text.startswith('---\n'):
            text = text.partition('\n---\n')[2].strip()
        heading, _, body = text.partition('\n')
        body = body.strip()
        if not heading.startswith('# ') or not any(character.isalnum() for character in body):
            errors.append(f'{label}: missing heading or chapter body')
        body_hash = hashlib.sha256(' '.join(body.split()).encode()).hexdigest()
        if body_hash in bodies:
            errors.append(f'{label}: body duplicates {bodies[body_hash]}')
        bodies[body_hash] = label
        verified += 1
    extras = sorted(path.name for path in directory.glob('*.md') if path.name not in filenames)
    if extras:
        errors.append('Untracked Markdown files: ' + ', '.join(extras))
    return {'passed': not errors, 'output_dir': str(directory), 'catalog_entries': len(expected),
            'files_checked': verified, 'errors': errors,
            'validation_scope': 'Local catalog coverage, ordering, Markdown structure, duplicate bodies, hashes and source receipts; no new source-content audit.'}


async def run(args) -> int:
    from src.scraper.runtime import atomic_json, safe_error

    if args.validate_only:
        destination = args.validate_only
    else:
        from src.main import run_headless
        result = await run_headless(args.url, concurrency=args.concurrency,
                                    refresh=args.refresh, overwrite=args.overwrite)
        if not result.get('output_dir'):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        destination = Path(result['output_dir'])
        atomic_json(destination / 'download-result.json', result)
    try:
        report = validate_download(destination)
    except (OSError, ValueError, KeyError, TypeError) as error:
        report = {'passed': False, 'output_dir': str(destination), 'errors': [safe_error(error)]}
    if not args.validate_only and result.get('status') != 'completed':
        report['passed'] = False
        report['errors'].append('Agent status: ' + result.get('status', 'unknown'))
    atomic_json(destination / 'validation-report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Report: {destination / 'validation-report.json'}")
    return 0 if report['passed'] else 1


def main():
    if not 1 <= CONCURRENCY <= 10:
        print('CONCURRENCY must be between 1 and 10.', file=sys.stderr)
        return 2
    if not URL and not VALIDATE_ONLY:
        print('Set URL or VALIDATE_ONLY in the settings.', file=sys.stderr)
        return 2
    args = SimpleNamespace(url=URL, concurrency=CONCURRENCY, refresh=REFRESH,
                           overwrite=OVERWRITE, validate_only=Path(VALIDATE_ONLY) if VALIDATE_ONLY else None)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print('Interrupted. Rerun the same command to resume.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
