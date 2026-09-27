import pytest
from src.agent.classifier import ChapterLink
from src.agent.toc.tools import (
    ClaimInspector,
    DomLinkExtractor,
    PaginatedTocCrawler,
    TocAuditor,
)
from src.agent.toc.agent import TocAgent


def test_dom_link_extractor_syosetu():
    html = """
    <html><body>
        <div class="p-eplist">
            <div class="p-eplist__sublist">
                <a class="p-eplist__subtitle" href="/n2273dh/1/">異世界は平和でした</a>
                <a class="p-eplist__subtitle" href="/n2273dh/2/">死亡フラグはちゃんとありました</a>
                <a class="p-eplist__subtitle" href="/n2273dh/3/">公爵様は良い人でした</a>
            </div>
        </div>
    </body></html>
    """
    chapters = DomLinkExtractor.extract(html, "https://ncode.syosetu.com/n2273dh/")
    assert len(chapters) == 3
    assert chapters[0].title == "異世界は平和でした"
    assert chapters[0].url == "https://ncode.syosetu.com/n2273dh/1/"
    assert chapters[2].title == "公爵様は良い人でした"
    assert chapters[2].url == "https://ncode.syosetu.com/n2273dh/3/"


def test_paginated_toc_crawler_syosetu_range():
    html = """
    <html><body>
        <div class="c-pager">
            <div class="c-pager__pager">
                <a class="c-pager__item c-pager__item--next" href="/n2273dh/?p=2">次へ</a>
                <a class="c-pager__item c-pager__item--last" href="/n2273dh/?p=27">最後へ</a>
            </div>
        </div>
    </body></html>
    """
    urls = PaginatedTocCrawler.detect_pagination_urls(html, "https://ncode.syosetu.com/n2273dh/")
    assert len(urls) == 26
    assert urls[0] == "https://ncode.syosetu.com/n2273dh/?p=2"
    assert urls[-1] == "https://ncode.syosetu.com/n2273dh/?p=27"


def test_claim_inspector_syosetu_author_cleaning():
    html = """
    <html><head><title>勇者召喚に巻き込まれたけど、異世界は平和でした - 小説家になろう</title></head>
    <body>
        <h1 class="p-novel__title">勇者召喚に巻き込まれたけど、異世界は平和でした</h1>
        <div class="p-novel__author">作者：灯台</div>
    </body></html>
    """
    claimed, title, author, desc = ClaimInspector.inspect(html, "https://ncode.syosetu.com/n2273dh/")
    assert title == "勇者召喚に巻き込まれたけど、異世界は平和でした"
    assert author == "灯台"


@pytest.mark.asyncio
async def test_syosetu_live_toc_agent():
    agent = TocAgent()
    try:
        state = await agent.extract_toc("https://ncode.syosetu.com/n2273dh/")
        assert state["novel_title"] == "勇者召喚に巻き込まれたけど、異世界は平和でした"
        assert state["author"] == "灯台"
        assert len(state["extracted_chapters"]) >= 2635
        assert state["claimed_chapter_count"] >= 2635
        assert state["is_complete"] is True
        assert state["confidence_score"] == 1.0
        assert state["extracted_chapters"][0].title == "異世界は平和でした"
        assert state["extracted_chapters"][0].url == "https://ncode.syosetu.com/n2273dh/1/"
        assert "続・魔界の遊園地" in state["extracted_chapters"][-1].title
        assert state["extracted_chapters"][-1].url.startswith("https://ncode.syosetu.com/n2273dh/")
    finally:
        await agent.obscura.close()
