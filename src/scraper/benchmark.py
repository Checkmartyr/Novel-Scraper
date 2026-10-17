"""Reproducible offline catalog and chapter recovery measurements.

Run ``python -m src.scraper.benchmark --output report.json``. Fixture websites
are synthetic, annotated layout families; these scores are not live-site rates.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from difflib import SequenceMatcher
import json
import hashlib
from pathlib import Path
import re
import tempfile
import time
from unittest.mock import patch


def text_scores(actual, expected):
    actual, expected = re.findall(r"\w+", actual), re.findall(r"\w+", expected)
    matches = sum(block.size for block in SequenceMatcher(None, expected, actual, autojunk=False).get_matching_blocks())
    return {"precision": matches / max(1, len(actual)), "recall": matches / max(1, len(expected))}


def corpus():
    """Fixed gold data independent of parser output, grouped by website family."""
    cases = []
    for family, split in [("static", "development"), ("delayed", "development"),
                          ("wordpress-migration", "held_out"), ("split-story", "held_out"),
                          ("short-prologue", "held_out"), ("changed-layout", "held_out")]:
        source = f"https://{family}.test/novel/home"
        host = f"https://new-{family}.test" if family == "wordpress-migration" else f"https://{family}.test"
        pages, rendered, gold = {}, {}, []
        for index in range(1, 6):
            title = f"Chapter {index}: Homecoming"
            paragraphs = [f"The traveller returned on day {index}, crossing the quiet garden beneath the old oak.",
                          "She heard a familiar voice and saw the door open slowly in the morning light.",
                          f"At last she was home, and the lamp stayed lit until dawn on day {index}."]
            if family == "short-prologue":
                title, paragraphs = f"Prologue {index}", [f"The light returned on day {index}."]
            url = f"{host}/novel/home/chapter-{index}"
            body = "".join(f"<p>{part}</p>" for part in paragraphs)
            if family == "split-story":
                body = f'<div class="preface">{body[:body.index("</p>")+4]}</div><section class="continuation">{body[body.index("</p>")+4:]}</section>'
            selector = "chapter-content" if family in {"static", "delayed"} else "story-new"
            html = f'<html><h1>{title}</h1><article><div id="{selector}">{body}</div></article><footer>Copyright Example</footer><a href="{source}">Table of contents</a></html>'
            pages[url] = html
            if family == "delayed":
                rendered[url] = html
                pages[url] = f'<html><h1>{title}</h1><div id="chapter-content"><div class="skeleton"></div></div></html>'
            gold.append({"url": url, "title": title, "text": "\n\n".join(paragraphs)})
        catalog = '<html><h1>Homecoming</h1><p>5 Chapters</p><div class="chapter-list">' + ''.join(
            f'<a href="{item["url"]}">{item["title"]}</a>' for item in gold) + '</div></html>'
        pages[source] = catalog
        cases.append({"family": family, "split": split, "url": source, "pages": pages, "rendered": rendered, "gold": gold})
    return cases


class FixtureFetcher:
    def __init__(self, case):
        from src.scraper.runtime import ScraperLimits
        self.case, self.limits = case, ScraperLimits()
        self.metrics = {"requests": 0, "rendered": 0, "browser_launches": 0, "recipe_hits": 0}

    async def fetch_html(self, url, **kwargs):
        from src.scraper.telemetry import stage
        with stage("browser" if kwargs.get("force_browser") else "retrieval") as row:
            result = await self._fetch_html(url, **kwargs)
            row.update(outcome="observed", reason="Frozen evidence retrieved; content awaits audit")
            return result

    async def _fetch_html(self, url, **kwargs):
        self.metrics["requests"] += 1
        if self.case.get("fetch_records", {}).get(url):
            from src.scraper.recovery import FetchRecord, current_scope
            record = FetchRecord(**self.case["fetch_records"][url])
            self.fetch_records = getattr(self, "fetch_records", {})
            self.fetch_records[url] = record
            if current_scope():
                current_scope().note_redirect(url, record.final_url, record.redirects)
        if kwargs.get("force_browser"):
            self.metrics["rendered"] += 1
            self.metrics["browser_launches"] = 1
            return self.case["rendered"][url] if url in self.case["rendered"] else self.case["pages"][url]
        return self.case["pages"][url]

    async def close(self):
        pass

    @asynccontextmanager
    async def get_playwright_page(self, **kwargs):
        raise RuntimeError("Fixture has no interactive catalog; recovery must remain incomplete")
        yield


async def measure_case(case, directory, use_llm=False):
    from src import api_bridge
    from src.agent.domain_memory import DomainMemoryManager
    from src.agent.llm import LLMClient
    from src.scraper.runtime import canonical_url

    class OfflineModel:
        is_available = False
        last_interaction_id = None

    memory = DomainMemoryManager(directory / "recipes")
    measurements = []
    for temperature in ("cold", "warm"):
        fetcher = FixtureFetcher(case)
        model = LLMClient() if use_llm else OfflineModel()
        calls = 0
        if use_llm:
            generate = model.generate_json
            async def counted(*args, **kwargs):
                nonlocal calls
                calls += 1
                return await generate(*args, **kwargs)
            model.generate_json = counted
        output = directory / temperature
        state = directory / f"state-{temperature}"
        start = time.monotonic()
        positions = sorted({0, len(case["gold"]) // 2, len(case["gold"]) - 1})
        samples = [case["gold"][index] for index in positions]
        with patch("src.agent.domain_memory.domain_memory", memory), \
             patch.object(api_bridge, "LLMClient", lambda: model), \
             patch.object(api_bridge, "ObscuraClient", lambda: fetcher):
            result = await api_bridge.extract_novel_chapters(case["url"], output,
                chapter_indices=[index + 1 for index in positions], concurrency=1, state_dir=state, job_id="benchmark")
        snapshot_path = state / "snapshots" / "benchmark.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else {}
        predicted = {canonical_url(item["url"]) for item in snapshot.get("chapters", [])}
        expected = {canonical_url(item["url"]) for item in case["gold"]}
        overlap = len(predicted & expected)
        chapter_metrics = []
        outcomes = {item["url"]: item for item in result.get("outcomes", [])}
        for gold in samples:
            outcome = outcomes.get(gold["url"], {})
            path = output / outcome.get("file", "missing.md")
            content = path.read_text(encoding="utf-8") if path.is_file() else ""
            body = content.split("\n\n", 1)[-1] if content else ""
            scores = text_scores(body, gold["text"])
            title = content.splitlines()[0].lstrip("# ") if content else ""
            normalize = lambda value: re.sub(r"[^\w]+", "", value).casefold()
            chapter_metrics.append({**scores, "title_correct": normalize(title) == normalize(gold["title"]),
                "ending_preserved": body.strip().endswith(gold["text"].split("\n\n")[-1]),
                "published": bool(outcome.get("success"))})
        complete = predicted == expected and bool(snapshot.get("is_complete"))
        clean = all(item["published"] and item["precision"] == 1 and item["recall"] == 1
                    and item["title_correct"] and item["ending_preserved"] for item in chapter_metrics)
        measurements.append({"family": case["family"], "split": case["split"], "recipes": temperature,
            "catalog_precision": overlap / max(1, len(predicted)), "catalog_recall": overlap / len(expected),
            "false_completion": bool(snapshot.get("is_complete")) and predicted != expected,
            "success": complete and clean, "seconds": round(time.monotonic() - start, 4),
            "model_calls": calls, "browser_launches": fetcher.metrics["browser_launches"],
            "browser_renders": fetcher.metrics["rendered"], "chapters": chapter_metrics})
        measurements[-1]["recovery_stages"] = result.get("metrics", {}).get("recovery_stages", [])
        measurements[-1]["pipeline_status"] = result.get("status")
        measurements[-1]["failure_reasons"] = list(dict.fromkeys(
            [str(result["error"])] if result.get("error") else []))
        measurements[-1]["failure_reasons"].extend(list(dict.fromkeys(
            [*snapshot.get("issues", []), *(outcome["error"] for outcome in result.get("outcomes", []) if outcome.get("error"))])))
    return measurements


async def run_benchmark(use_llm=False, compare_trafilatura=False):
    results = []
    with tempfile.TemporaryDirectory(prefix="scraper-benchmark-") as temporary:
        for case in corpus():
            results.extend(await measure_case(case, Path(temporary) / case["family"], use_llm))
    report = {"corpus": "synthetic-unfamiliar-v1", "mode": "configured_model" if use_llm else "deterministic",
              "corpus_sha256": hashlib.sha256(json.dumps(corpus(), sort_keys=True).encode()).hexdigest(),
              "runs": results, "success_rate": sum(item["success"] for item in results) / len(results),
              "false_completions": sum(item["false_completion"] for item in results)}
    if compare_trafilatura:
        try:
            import trafilatura
        except ImportError:
            report["trafilatura"] = {"status": "not_installed"}
        else:
            report["trafilatura"] = {"status": "evaluated", "chapters": [
                {"family": case["family"], **text_scores(trafilatura.extract(
                    case["rendered"].get(gold["url"], case["pages"][gold["url"]]), include_comments=False) or "", gold["text"])}
                for case in corpus() for gold in case["gold"]]}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--llm", action="store_true", help="Explicitly enable configured model calls on offline HTML")
    parser.add_argument("--compare-trafilatura", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run_benchmark(args.llm, args.compare_trafilatura))
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
