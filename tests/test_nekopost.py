import pytest
from src.core.nekopost_handler import (
    is_nekopost_url,
    parse_nekopost_url,
    build_nekopost_toc_html,
    build_nekopost_chapter_html,
)
from src.agent.toc.agent import TocAgent
from src.core.obscura_client import ObscuraClient


def test_parse_nekopost_url():
    assert is_nekopost_url("https://www.nekopost.net/novel/17961") is True
    assert is_nekopost_url("https://nekopost.net/novel/17961/1") is True
    assert is_nekopost_url("https://www.nekopost.net/comic/1234") is False
    assert is_nekopost_url("https://ncode.syosetu.com/n2273dh/") is False

    assert parse_nekopost_url("https://www.nekopost.net/novel/17961") == (17961, None)
    assert parse_nekopost_url("https://www.nekopost.net/novel/17961/") == (17961, None)
    assert parse_nekopost_url("https://www.nekopost.net/novel/17961/1") == (17961, "1")
    assert parse_nekopost_url("https://www.nekopost.net/novel/17961/0.1") == (17961, "0.1")
    assert parse_nekopost_url("https://www.nekopost.net/comic/17961") == (None, None)


def test_build_nekopost_toc_html():
    project_info = {
        "projectName": "Test Project",
        "authorName": "Test Author",
        "info": "A synopsis description."
    }
    chapters = [
        {"ChapterNo": "1", "ChapterName": "Intro", "ChapterID": 101},
        {"ChapterNo": "2", "ChapterName": "Beginning", "ChapterID": 102}
    ]
    html = build_nekopost_toc_html(1234, project_info, chapters)
    assert "Test Project" in html
    assert "Test Author" in html
    assert "A synopsis description." in html
    assert '<script id="__NEKOPOST_DATA__"' in html
    assert 'href="https://www.nekopost.net/novel/1234/1"' in html
    assert 'href="https://www.nekopost.net/novel/1234/2"' in html
    assert "Intro" in html
    assert "Beginning" in html


def test_build_nekopost_chapter_html():
    html = build_nekopost_chapter_html(
        project_id=1234,
        chapter_no="1",
        chapter_title="Chapter 1: Intro",
        content_html="<p>Hello world from Nekopost!</p>",
        novel_title="Test Project"
    )
    assert "Chapter 1: Intro" in html
    assert 'id="chapter-content"' in html
    assert "<p>Hello world from Nekopost!</p>" in html


@pytest.mark.asyncio
async def test_live_nekopost_toc_and_agent():
    url = "https://www.nekopost.net/novel/17961"
    agent = TocAgent()
    state = await agent.extract_toc(url)

    assert state["is_complete"] is True
    assert state["novel_title"] is not None
    assert "นักบุญหญิง" in state["novel_title"]
    assert state["claimed_chapter_count"] >= 27
    assert len(state["extracted_chapters"]) >= 27
    assert state["extracted_chapters"][0].index == 1
    assert "0.1" in state["extracted_chapters"][0].title
    assert "https://www.nekopost.net/novel/17961/" in state["extracted_chapters"][-1].url


@pytest.mark.asyncio
async def test_live_nekopost_chapter_content():
    url = "https://www.nekopost.net/novel/17961/1"
    client = ObscuraClient()
    html = await client.fetch_html(url)

    assert "Chapter 1" in html
    assert 'id="chapter-content"' in html
    assert len(html) > 500
