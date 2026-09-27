"""Unit and integration tests for Dek-D handler and TOC extraction."""

import pytest
from src.core.dekd_handler import (
    is_dekd_url,
    parse_dekd_url,
    build_dekd_toc_html,
    handle_dekd_url,
)
from src.agent.toc.tools import (
    ClaimInspector,
    EmbeddedStateExtractor,
    DomLinkExtractor,
    TocAuditor,
)
from src.agent.toc.agent import TocAgent
from src.agent.classifier import ChapterLink


def test_is_dekd_url():
    assert is_dekd_url("https://writer.dek-d.com/Kumari/writer/view.php?id=2655851") is True
    assert is_dekd_url("https://writer.dek-d.com/Kumari/writer/viewlongc.php?id=2655851&chapter=1") is True
    assert is_dekd_url("https://novel.dek-d.com/novel/2655851") is True
    assert is_dekd_url("https://novel.dek-d.com/novel/2655851/chapter/1") is True
    assert is_dekd_url("https://www.dek-d.com/writer/view.php?id=2655851") is True
    assert is_dekd_url("https://syosetu.com/n1234") is False
    assert is_dekd_url("https://kakuyomu.jp/works/1234") is False
    assert is_dekd_url("") is False


def test_parse_dekd_url():
    # 1. Standard writer TOC URL
    nid, user, ch = parse_dekd_url("https://writer.dek-d.com/Kumari/writer/view.php?id=2655851")
    assert nid == "2655851"
    assert user == "Kumari"
    assert ch is None

    # 2. Standard writer Chapter URL
    nid, user, ch = parse_dekd_url("https://writer.dek-d.com/Kumari/writer/viewlongc.php?id=2655851&chapter=15")
    assert nid == "2655851"
    assert user == "Kumari"
    assert ch == "15"

    # 3. novel.dek-d.com TOC URL
    nid, user, ch = parse_dekd_url("https://novel.dek-d.com/novel/2655851")
    assert nid == "2655851"
    assert ch is None

    # 4. novel.dek-d.com Chapter URL
    nid, user, ch = parse_dekd_url("https://novel.dek-d.com/novel/2655851/chapter/5")
    assert nid == "2655851"
    assert ch == "5"


def test_build_dekd_toc_html():
    meta = {
        "novel_title": "ฉันกลายเป็นยัยหัวชมพู",
        "author": "Kumari",
        "user_slug": "Kumari",
        "category": "แฟนตาซี",
        "thumbnail": "https://image.dek-d.com/test.jpg",
        "total_chapters": 2,
    }
    chapters = [
        {"id": 101, "order": 1, "title": "ตอนที่ 1 กำปั้น"},
        {"id": 102, "order": 2, "title": "ตอนที่ 2 ลาวา"},
    ]

    html = build_dekd_toc_html("2655851", meta, chapters)
    assert "ฉันกลายเป็นยัยหัวชมพู" in html
    assert "Kumari" in html
    assert "__DEKD_DATA__" in html
    assert "viewlongc.php?id=2655851&chapter=1" in html
    assert "viewlongc.php?id=2655851&chapter=2" in html
    assert "ตอนที่ 1 กำปั้น" in html


def test_claim_inspector_dekd_embedded():
    meta = {
        "novel_title": "ยัยหัวชมพู",
        "author": "Kumari",
        "user_slug": "Kumari",
        "total_chapters": 62,
    }
    chapters = [{"id": i, "order": i, "title": f"Chapter {i}"} for i in range(1, 63)]
    html = build_dekd_toc_html("2655851", meta, chapters)

    claimed, title, author, desc = ClaimInspector.inspect(html, "https://writer.dek-d.com/Kumari/writer/view.php?id=2655851")
    assert claimed == 62
    assert "ยัยหัวชมพู" in title
    assert author == "Kumari"


def test_claim_inspector_thai_stat_badge():
    html = """
    <html>
      <head><title>นิยาย ทดสอบ : Dek-D.com</title></head>
      <body>
        <h1>ทดสอบ</h1>
        <div class="stat-text">
          <strong><span class="stat_number">62</span></strong><br>
          <p class="iconname">ตอน </p>
        </div>
      </body>
    </html>
    """
    claimed, title, author, desc = ClaimInspector.inspect(html, "https://writer.dek-d.com/test/writer/view.php?id=123")
    assert claimed == 62


def test_embedded_state_extractor_dekd():
    meta = {
        "novel_title": "ยัยหัวชมพู",
        "author": "Kumari",
        "user_slug": "Kumari",
        "total_chapters": 3,
    }
    chapters = [
        {"id": 1, "order": 1, "title": "ตอนที่ 1"},
        {"id": 2, "order": 2, "title": "ตอนที่ 2"},
        {"id": 3, "order": 3, "title": "ตอนที่ 3"},
    ]
    html = build_dekd_toc_html("2655851", meta, chapters)

    extracted = EmbeddedStateExtractor.extract(html, "https://writer.dek-d.com/Kumari/writer/view.php?id=2655851")
    assert len(extracted) == 3
    assert extracted[0].index == 1
    assert extracted[0].title == "ตอนที่ 1"
    assert "chapter=1" in extracted[0].url
    assert extracted[2].index == 3
    assert extracted[2].title == "ตอนที่ 3"
    assert "chapter=3" in extracted[2].url


def test_dom_link_extractor_dekd():
    html = """
    <html>
      <body>
        <div class="chapter-list">
          <a href="/Kumari/writer/viewlongc.php?id=2655851&chapter=1" class="chapter-link">1. ผู้หญิงเขาคุยกันด้วยกำปั้นนะ☆</a>
          <a href="/Kumari/writer/viewlongc.php?id=2655851&chapter=2" class="chapter-link">2. ตอนที่สอง</a>
        </div>
      </body>
    </html>
    """
    url = "https://writer.dek-d.com/Kumari/writer/view.php?id=2655851"
    chapters = DomLinkExtractor.extract(html, url)
    assert len(chapters) == 2
    assert chapters[0].index == 1
    assert chapters[1].index == 2
    assert "viewlongc.php?id=2655851&chapter=1" in chapters[0].url
    assert "viewlongc.php?id=2655851&chapter=2" in chapters[1].url


@pytest.mark.asyncio
async def test_dekd_toc_agent_full_workflow():
    """Test full LangGraph TocAgent execution on synthetic Dek-D HTML."""
    meta = {
        "novel_title": "(นิยายแปล)ฉันกลายเป็นยัยหัวชมพูแห่งสถาบัน",
        "author": "Kumari",
        "user_slug": "Kumari",
        "total_chapters": 62,
    }
    chapters = [{"id": i, "order": i, "title": f"ตอนที่ {i}"} for i in range(1, 63)]
    html = build_dekd_toc_html("2655851", meta, chapters)
    url = "https://writer.dek-d.com/Kumari/writer/view.php?id=2655851"

    agent = TocAgent()
    state = await agent.extract_toc(url, html=html)

    assert state["is_complete"] is True
    assert state["claimed_chapter_count"] == 62
    assert len(state["extracted_chapters"]) == 62
    assert state["confidence_score"] >= 0.95
    assert state["extracted_chapters"][0].index == 1
    assert "ตอนที่ 1" in state["extracted_chapters"][0].title
    assert state["extracted_chapters"][61].index == 62
    assert "ตอนที่ 62" in state["extracted_chapters"][61].title
