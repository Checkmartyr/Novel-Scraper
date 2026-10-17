"""Opt-in site acceptance, including private authorized-session inputs."""
import json
import os
import pytest
from src.scraper.evaluation import isolated_live

MATRIX = json.loads(os.getenv("NOVEL_SCRAPER_LIVE_MATRIX", json.dumps([
    {"name": "public-paginated-syosetu", "url": "https://ncode.syosetu.com/n2273dh/"},
    {"name": "embedded-kakuyomu", "url": "https://kakuyomu.jp/works/16817330663722833570"},
])))


@pytest.mark.live
@pytest.mark.parametrize("entry", MATRIX, ids=[entry.get("name", f"site-{index}") for index, entry in enumerate(MATRIX)])
def test_catalog_and_verified_representative_chapters(entry, monkeypatch, tmp_path):
    if entry.get("session_state"):
        monkeypatch.setenv("NOVEL_SCRAPER_STORAGE_STATE", entry["session_state"])
    report = isolated_live(entry, tmp_path)
    result = report.get("inspection", {})
    if entry.get("expected_blocker"):
        assert not result.get("is_complete"), result
        assert entry["expected_blocker"].casefold() in json.dumps(result).casefold(), result
        return
    assert result.get("success") and result.get("is_complete"), result
    assert result["chapters"] and result["completeness_evidence"]
    assert not result.get("claimed_chapter_count") or result["total_chapters"] >= result["claimed_chapter_count"]
    samples = sorted({1, result["total_chapters"] // 2 + 1, result["total_chapters"]})
    extracted = report.get("extraction") or report
    assert extracted["status"] == "completed", extracted
    assert extracted["saved"] == len(samples), extracted
    assert len(extracted["files"]) == len(samples)
