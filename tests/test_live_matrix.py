"""Opt-in site acceptance, including private authorized-session inputs."""
import json
import os
import pytest
from src.api_bridge import inspect_novel_url

MATRIX = json.loads(os.getenv("NOVEL_SCRAPER_LIVE_MATRIX", json.dumps([
    {"name": "public-paginated-syosetu", "url": "https://ncode.syosetu.com/n2273dh/"},
    {"name": "embedded-kakuyomu", "url": "https://kakuyomu.jp/works/16817330663722833570"},
])))


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize("entry", MATRIX, ids=[entry.get("name", f"site-{index}") for index, entry in enumerate(MATRIX)])
async def test_accessible_catalog_or_explicit_blocker(entry, monkeypatch):
    if entry.get("session_state"):
        monkeypatch.setenv("NOVEL_SCRAPER_STORAGE_STATE", entry["session_state"])
    result = await inspect_novel_url(entry["url"])
    if result.get("success") and result.get("is_complete"):
        assert result["chapters"]
        assert result["completeness_evidence"]
    else:
        assert result.get("error") or result.get("issues"), result
