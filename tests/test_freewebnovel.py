import pytest
from src.agent.toc.agent import TocAgent
from src.core.obscura_client import ObscuraClient


@pytest.mark.asyncio
async def test_freewebnovel_toc_extraction():
    url = "https://freewebnovel.com/novel/the-harem-system-rewards-me-for-everything"
    agent = TocAgent()
    state = await agent.extract_toc(url)

    assert state["is_complete"] is True
    assert len(state["extracted_chapters"]) >= 27

    # Verify Chapter 1 does not have the button label "Read first"
    ch1 = state["extracted_chapters"][0]
    assert ch1.index == 1
    assert "Read first" not in ch1.title
    assert "Chapter 1" in ch1.title
    assert ch1.url.endswith("/chapter-1")

    # Verify sequential ordering (1..N)
    for idx, ch in enumerate(state["extracted_chapters"], start=1):
        assert ch.index == idx
        assert f"/chapter-{idx}" in ch.url

    ch_last = state["extracted_chapters"][-1]
    assert ch_last.index == len(state["extracted_chapters"])
    assert f"Chapter {ch_last.index}" in ch_last.title
    assert ch_last.url.endswith(f"/chapter-{ch_last.index}")


@pytest.mark.asyncio
async def test_freewebnovel_chapter_fetch():
    url = "https://freewebnovel.com/novel/the-harem-system-rewards-me-for-everything/chapter-1"
    client = ObscuraClient()
    try:
        html = await client.fetch_html(url)
        assert len(html) > 1000
        assert "Chapter 1" in html
        assert "<p>" in html
    finally:
        await client.close()
