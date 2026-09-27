"""Unit and integration tests for WebNovel handler and autonomous extraction pipeline."""

import pytest
from src.core.webnovel_handler import (
    is_webnovel_url,
    parse_webnovel_url,
    build_webnovel_toc_html,
)
from src.agent.toc.tools import (
    ClaimInspector,
    EmbeddedStateExtractor,
    DomLinkExtractor,
    TocAuditor,
)
from src.agent.classifier import PageClassifier, ChapterLink
from src.agent.analyzer import ChapterAnalyzer


def test_is_webnovel_url():
    assert is_webnovel_url("https://www.webnovel.com/book/the-villainess-with-a-heroine-harem_21092118006417205") is True
    assert is_webnovel_url("https://www.webnovel.com/book/21092118006417205") is True
    assert is_webnovel_url("https://www.webnovel.com/book/21092118006417205/catalog") is True
    assert is_webnovel_url("https://www.webnovel.com/book/the-villainess-with-a-heroine-harem_21092118006417205/the-'villainess-system'-to-the-rescue!_56618917044478356") is True
    assert is_webnovel_url("https://m.webnovel.com/book/21092118006417205") is True
    assert is_webnovel_url("https://syosetu.com/n1234") is False
    assert is_webnovel_url("https://kakuyomu.jp/works/1234") is False
    assert is_webnovel_url("") is False


def test_parse_webnovel_url():
    # 1. Book detail with slug
    bid, slug, chid = parse_webnovel_url("https://www.webnovel.com/book/the-villainess-with-a-heroine-harem_21092118006417205")
    assert bid == "21092118006417205"
    assert slug == "the-villainess-with-a-heroine-harem"
    assert chid is None

    # 2. Book detail with ID only
    bid, slug, chid = parse_webnovel_url("https://www.webnovel.com/book/21092118006417205")
    assert bid == "21092118006417205"
    assert slug is None
    assert chid is None

    # 3. Catalog URL
    bid, slug, chid = parse_webnovel_url("https://www.webnovel.com/book/21092118006417205/catalog")
    assert bid == "21092118006417205"
    assert chid is None

    # 4. Chapter URL
    bid, slug, chid = parse_webnovel_url(
        "https://www.webnovel.com/book/the-villainess-with-a-heroine-harem_21092118006417205/the-'villainess-system'-to-the-rescue!_56618917044478356"
    )
    assert bid == "21092118006417205"
    assert slug == "the-villainess-with-a-heroine-harem"
    assert chid == "56618917044478356"


def test_build_webnovel_toc_html():
    meta = {
        "book_id": "21092118006417205",
        "novel_title": "The Villainess with a Heroine Harem",
        "author": "TestAuthor",
        "description": "A fun yuri reincarnation adventure.",
        "total_chapters": 3,
    }
    chapters = [
        {"order": 1, "title": "Chapter 1", "url": "https://www.webnovel.com/book/21092118006417205/ch_1"},
        {"order": 2, "title": "Chapter 2", "url": "https://www.webnovel.com/book/21092118006417205/ch_2"},
        {"order": 3, "title": "Chapter 3", "url": "https://www.webnovel.com/book/21092118006417205/ch_3"},
    ]

    html = build_webnovel_toc_html("21092118006417205", meta, chapters)
    assert "The Villainess with a Heroine Harem" in html
    assert "TestAuthor" in html
    assert "__WEBNOVEL_DATA__" in html
    assert "ch_1" in html
    assert "ch_2" in html
    assert "ch_3" in html


def test_claim_inspector_webnovel_embedded():
    meta = {
        "novel_title": "The Villainess with a Heroine Harem",
        "author": "YuriWriter",
        "description": "Synopsis text",
        "total_chapters": 822,
    }
    chapters = [{"order": i, "title": f"Chapter {i}", "url": f"https://www.webnovel.com/ch_{i}"} for i in range(1, 823)]
    html = build_webnovel_toc_html("21092118006417205", meta, chapters)

    claimed, title, author, desc = ClaimInspector.inspect(html, "https://www.webnovel.com/book/21092118006417205/catalog")
    assert claimed == 822
    assert "The Villainess with a Heroine Harem" in title
    assert author == "YuriWriter"
    assert desc == "Synopsis text"


def test_embedded_state_extractor_webnovel():
    meta = {
        "novel_title": "WebNovel Test",
        "author": "Author",
        "total_chapters": 10,
    }
    chapters = [{"order": i, "title": f"Chapter {i}: Title", "url": f"https://www.webnovel.com/ch_{i}"} for i in range(1, 11)]
    html = build_webnovel_toc_html("12345", meta, chapters)

    extracted = EmbeddedStateExtractor.extract(html, "https://www.webnovel.com/book/12345")
    assert len(extracted) == 10
    assert extracted[0].title == "Chapter 1: Title"
    assert extracted[0].url == "https://www.webnovel.com/ch_1"
    assert extracted[9].index == 10


def test_dom_link_extractor_webnovel_catalog():
    catalog_html = """
    <div class="volume-item">
      <h3 class="volume-title">Volume 1</h3>
      <ol class="content-list">
        <li class="g_col">
          <a href="/book/21092118006417205/chapter-1_111" title="Chapter 1: The Start">
            <strong>Chapter 1: The Start</strong>
            <small class="time">5 years ago</small>
          </a>
        </li>
        <li class="g_col">
          <a href="/book/21092118006417205/chapter-2_222" title="Chapter 2: The Middle">
            <strong>Chapter 2: The Middle</strong>
          </a>
        </li>
      </ol>
    </div>
    """
    extracted = DomLinkExtractor.extract(catalog_html, "https://www.webnovel.com/book/21092118006417205/catalog")
    assert len(extracted) == 2
    assert extracted[0].title == "Chapter 1: The Start"
    assert extracted[0].url == "https://www.webnovel.com/book/21092118006417205/chapter-1_111"
    assert extracted[1].title == "Chapter 2: The Middle"


@pytest.mark.asyncio
async def test_page_classifier_heuristic_webnovel_toc():
    classifier = PageClassifier()
    meta = {
        "novel_title": "The Villainess with a Heroine Harem",
        "author": "NovelAuthor",
        "total_chapters": 5,
    }
    chapters = [
        {"order": i, "title": f"Chapter {i}: Part {i}", "url": f"https://www.webnovel.com/book/21092118006417205/ch_{i}"}
        for i in range(1, 6)
    ]
    html = build_webnovel_toc_html("21092118006417205", meta, chapters)

    res = await classifier.classify("https://www.webnovel.com/book/the-villainess-with-a-heroine-harem_21092118006417205", html)
    assert res.page_type == "TOC"
    assert len(res.chapter_links) == 5
    assert "Villainess" in res.novel_title


@pytest.mark.asyncio
async def test_page_classifier_heuristic_webnovel_chapter():
    classifier = PageClassifier()
    chapter_html = """
    <html>
      <head><title>Chapter 1 - The Villainess - WebNovel</title></head>
      <body>
        <h1>Chapter 1: The Start</h1>
        <div class="cha-words"><p>Story text goes here...</p></div>
      </body>
    </html>
    """
    url = "https://www.webnovel.com/book/the-villainess-with-a-heroine-harem_21092118006417205/the-'villainess-system'-to-the-rescue!_56618917044478356"
    res = await classifier.classify(url, chapter_html)
    assert res.page_type == "CHAPTER"
    assert res.toc_url == "https://www.webnovel.com/book/21092118006417205/catalog"


def test_chapter_analyzer_webnovel():
    analyzer = ChapterAnalyzer()
    sample_html = """
    <html>
      <body>
        <h1>Chapter 1: The Start</h1>
        <div class="cha-words">
          <p>""" + "First paragraph of the chapter story. " * 30 + """</p>
          <p>""" + "Second paragraph of the chapter story. " * 30 + """</p>
          <div class="m-thou">Thought bubble to strip</div>
          <div class="report-wrap">Report button</div>
        </div>
      </body>
    </html>
    """
    plan = analyzer._heuristic_analyze(sample_html)
    assert plan.title_selector == "h1"
    assert plan.content_selector == ".cha-words"
    assert ".m-thou" in plan.remove_selectors
    assert ".report-wrap" in plan.remove_selectors
