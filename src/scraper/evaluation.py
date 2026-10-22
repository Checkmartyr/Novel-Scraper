"""Capture, replay annotated websites, and isolate opt-in live acceptance.

Raw HTML and gold chapter text belong under ignored .evaluation/, not in reports.
"""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from unittest.mock import patch


def source_hash():
    base = Path(__file__).resolve().parents[2]
    return hashlib.sha256(b"".join(str(path.relative_to(base)).encode() + path.read_bytes()
        for path in sorted((base / "src").rglob("*.py")))).hexdigest()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_file(root, item):
    path = (root / item["path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Capture path escapes its declared root")
    if digest(path) != item["sha256"]:
        raise ValueError(f"Frozen evidence changed: {item['path']}")
    return path


def load_case(site, root):
    if site.get("annotation_status") != "reviewed":
        raise ValueError("Independent source annotations have not been reviewed")
    pages, rendered = {}, {}
    for url, variants in site["captures"].items():
        for transport, item in variants.items():
            html = checked_file(root, item).read_text(encoding="utf-8")
            (rendered if transport == "browser" else pages)[url] = html
    gold = []
    for entry in site["catalog"]:
        item = dict(entry)
        if "text_file" in item:
            item["text"] = checked_file(root, item.pop("text_file")).read_text(encoding="utf-8")
        gold.append(item)
    samples = {0, len(gold) // 2, len(gold) - 1}
    if not gold or any("text" not in gold[index] for index in samples):
        raise ValueError("Gold text is required for first, middle and last entries")
    return {"family": site["id"], "split": site["split"], "url": site["url"],
            "pages": pages, "rendered": rendered, "gold": gold,
            "fetch_records": site.get("fetch_records", {})}


async def capture(urls, root, readiness=None):
    from src.core.obscura_client import ObscuraClient
    from src.scraper.runtime import atomic_json, safe_error
    root.mkdir(parents=True, exist_ok=True)
    index = {"captures": {}, "fetch_records": {}, "errors": {}}
    index_path = root / "capture-index.json"
    if index_path.exists():
        index.update(json.loads(index_path.read_text(encoding="utf-8")))
    async with ObscuraClient() as client:
        for url in urls:
            for browser in (False, True):
                transport = "browser" if browser else "http"
                try:
                    html = await client.fetch_html(url, force_browser=browser, readiness=readiness if browser else None)
                    path = root / (hashlib.sha256(url.encode()).hexdigest()[:20] + f"-{transport}-" + hashlib.sha256(html.encode()).hexdigest()[:10] + ".html")
                    path.write_text(html, encoding="utf-8")
                    index["captures"].setdefault(url, {})[transport] = {
                        "path": path.name, "sha256": digest(path)}
                except Exception as error:
                    index["errors"][url + "#" + transport] = safe_error(error)
                record = client.fetch_records.get(url)
                if record:
                    value = asdict(record)
                    # Captures do not persist cookies, authorization, or other headers.
                    value["headers"] = {key: value for key, value in value["headers"].items()
                                        if key.lower() in {"content-type", "link", "location"}}
                    index["fetch_records"][url] = value
                atomic_json(index_path, index)
    return index


async def replay(manifest, root, stages, use_llm=False):
    from src.scraper.benchmark import measure_case
    from src.scraper.telemetry import evaluation_budget
    runs, unavailable = [], []
    with tempfile.TemporaryDirectory(prefix="capture-replay-") as temporary, evaluation_budget() as counts:
        for site in manifest["sites"]:
            try:
                case = load_case(site, root)
            except (ValueError, OSError, KeyError) as error:
                unavailable.append({"site": site["id"], "reason": str(error), "success": False})
                continue
            for ceiling in stages:
                with patch.dict(os.environ, {"NOVEL_SCRAPER_RECOVERY_CEILING": ceiling}):
                    rows = await measure_case(case, Path(temporary) / site["id"] / ceiling, use_llm)
                for row in rows:
                    row["ceiling"] = ceiling
                runs.extend(rows)
        budget = dict(counts)
    return {"source_sha256": source_hash(), "manifest_sha256": hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()).hexdigest(), "runs": runs,
        "model_mode": "configured_model" if use_llm else "disabled",
        "recovery_mode": os.getenv("NOVEL_SCRAPER_RECOVERY_MODE", "standard"),
        "unmeasured": unavailable, "model_budget": budget,
        "requested_runs": (len(manifest["sites"]) * len(stages) * 2),
        "measured_runs": len(runs),
        "success_rate": sum(row["success"] for row in runs) / (len(manifest["sites"]) * len(stages) * 2) if runs else None,
        "false_completions": sum(row["false_completion"] for row in runs)}


async def live_worker(entry, directory):
    if entry.get("trial") == "visual":
        return await visual_trial()
    from src.api_bridge import inspect_novel_url, extract_novel_chapters
    from src.scraper.runtime import atomic_json
    from src.scraper.telemetry import evaluation_budget
    with evaluation_budget(int(entry.get("model_calls", 0)), int(entry.get("visual_calls", 0))) as counts:
        result = await inspect_novel_url(entry["url"])
        atomic_json(directory / "partial.json", result)
        extracted = None
        if result.get("is_complete") and result.get("chapters"):
            size = len(result["chapters"])
            samples = sorted({1, size // 2 + 1, size})
            extracted = await extract_novel_chapters(entry["url"], directory / "chapters",
                chapter_indices=samples, snapshot_file=directory / "partial.json", state_dir=directory / "state")
        return {"inspection": result, "extraction": extracted,
            "success": bool(extracted and extracted.get("success") and extracted.get("saved") == len(samples)),
            "model_budget": dict(counts)}


def isolated_live(entry, directory, timeout=None):
    """The worker and descendants are owned by this invocation only."""
    from src.scraper.runtime import ScraperLimits, atomic_json
    if os.getenv("NOVEL_SCRAPER_LIVE_TESTS") != "1":
        raise RuntimeError("External acceptance requires NOVEL_SCRAPER_LIVE_TESTS=1")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    entry_path = directory / "entry.json"
    atomic_json(entry_path, entry)
    env = dict(os.environ, PYTHON_DOTENV_DISABLED="1", RECIPES_DIR=str(directory / "recipes"),
               OUTPUT_DIR=str(directory / "output"), LOGS_DIR=str(directory / "logs"))
    if entry.get("session_state"):
        env["NOVEL_SCRAPER_STORAGE_STATE"] = entry["session_state"]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    command = [sys.executable, "-m", "src.scraper.evaluation", "worker", "--entry", str(entry_path), "--root", str(directory)]
    with (directory / "worker.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=log, **options)
        try:
            process.wait(timeout=timeout or 4 * ScraperLimits.from_env().discovery_seconds + ScraperLimits.from_env().cleanup_seconds + 10)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            partial = directory / "partial.json"
            return {"success": False, "error": "Live worker deadline exhausted",
                    "inspection": json.loads(partial.read_text(encoding="utf-8")) if partial.exists() else {},
                    "model_budget": {"model_calls": entry.get("model_calls", 0),
                                     "visual_calls": entry.get("visual_calls", 0), "usage_uncertain": True}}
    report = directory / "result.json"
    return json.loads(report.read_text(encoding="utf-8")) if report.exists() else {
        "success": False, "error": f"Live worker exited with status {process.returncode}"}


async def visual_trial():
    """Compare the same synthetic controls with DOM evidence and screenshot help."""
    from contextlib import asynccontextmanager
    from src.agent.llm import LLMClient
    from src.agent.toc.discovery import expand_catalog
    from src.agent.toc.tools import DomLinkExtractor
    from src.core.obscura_client import ObscuraClient
    from src.scraper.telemetry import evaluation_budget
    model = LLMClient()
    if not model.is_available:
        return {"status": "unmeasured", "reason": "Configured model credentials unavailable", "runs": []}
    runs = []
    with evaluation_budget() as counts:
        async with ObscuraClient() as client:
            for contents in (1, 2):
                labels = ["Theme", "Zoom", "Contents"]
                labels[contents], labels[2] = labels[2], labels[contents]
                styles = ''.join(f'button:nth-of-type({i+1})::before{{content:"{label}"}}' for i, label in enumerate(labels))
                buttons = ''.join('<button onclick="document.body.innerHTML=' +
                    "'" + ''.join(f'&lt;a href=/book/{n}&gt;Chapter {n}&lt;/a&gt;' for n in range(1, 4)) +
                    "'" + '"></button>' if label == 'Contents' else '<button></button>' for label in labels)
                document = '<style>button{font:24px sans-serif;padding:24px;margin:20px}' + styles + '</style>' + buttons
                for vision in (False, True):
                    with patch.dict(os.environ, {"NOVEL_SCRAPER_VISUAL_RECOVERY": "1" if vision else "0"}):
                        async with client.get_playwright_page() as raw:
                            class Page:
                                def __getattr__(self, name): return getattr(raw, name)
                                async def goto(self, *args, **kwargs):
                                    await raw.evaluate("html => { document.head.innerHTML = ''; document.body.innerHTML = html; }", document)
                            class Browser:
                                limits = client.limits
                                metrics = {}
                                async def pace(self, *args): pass
                                @asynccontextmanager
                                async def get_playwright_page(self, **kwargs): yield Page()
                            before = dict(counts)
                            try:
                                result = await expand_catalog("https://fixture.test/book", Browser(), DomLinkExtractor.extract, model)
                                success = {chapter.url for chapter in result.chapters} == {
                                    f"https://fixture.test/book/{n}" for n in range(1, 4)}
                                row = {"success": success, "status": "measured"}
                            except Exception as error:
                                from src.scraper.runtime import safe_error
                                row = {"success": False, "status": "unmeasured", "reason": safe_error(error)}
                            runs.append({"fixture": contents, "visual_enabled": vision, **row,
                                "model_calls": counts["model_calls"] - before["model_calls"],
                                "visual_calls": counts["visual_calls"] - before["visual_calls"]})
        return {"status": "evaluated", "runs": runs, "model_budget": dict(counts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "replay", "live", "worker", "visual"])
    parser.add_argument("--root", type=Path, default=Path(".evaluation"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--entry", type=Path)
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--readiness", choices=["catalog", "chapter"])
    parser.add_argument("--budget-file", type=Path, default=Path(".evaluation/model-budget.json"))
    parser.add_argument("--stages", nargs="+", default=["deterministic", "browser", "model", "repair"])
    args = parser.parse_args()
    if args.llm:
        os.environ["NOVEL_SCRAPER_EVALUATION_BUDGET_FILE"] = str(args.budget_file.resolve())
        from src.agent.llm import LLMClient
        configured_model = LLMClient()
    if args.action == "visual" and not args.llm:
        parser.error("Visual provider trials require --llm")
    if args.action in {"capture", "live", "worker", "visual"} and os.getenv("NOVEL_SCRAPER_LIVE_TESTS") != "1":
        parser.error("External acceptance requires NOVEL_SCRAPER_LIVE_TESTS=1")
    args.root.mkdir(parents=True, exist_ok=True)
    if args.action == "capture":
        result = asyncio.run(capture(args.url, args.root, args.readiness))
    elif args.action == "visual":
        result = isolated_live({"trial": "visual", "url": "https://fixture.test/book",
                                "model_calls": 30, "visual_calls": 6}, args.root / "visual-worker")
    elif args.action == "worker":
        entry = json.loads(args.entry.read_text(encoding="utf-8"))
        result = asyncio.run(live_worker(entry, args.root))
        args.output = args.root / "result.json"
    else:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if args.action == "replay":
            result = asyncio.run(replay(manifest, args.root, args.stages, args.llm))
        else:
            calls, visual, rows = 30 if args.llm else 0, 6 if args.llm else 0, []
            for site in manifest["sites"]:
                entry = {**site, "model_calls": calls, "visual_calls": visual}
                row = isolated_live(entry, args.root / site["id"])
                used = row.get("model_budget", {})
                calls = max(0, calls - used.get("model_calls", entry["model_calls"]))
                visual = max(0, visual - used.get("visual_calls", entry["visual_calls"]))
                rows.append({"site": site["id"], "split": site["split"], **row})
            result = {"source_sha256": source_hash(), "runs": rows, "remaining_model_calls": calls,
                      "remaining_visual_calls": visual}
    if args.output:
        from src.scraper.runtime import atomic_json
        atomic_json(args.output, result)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
