import pytest
import json
from pathlib import Path
from bs4 import BeautifulSoup

from src.agent.classifier import ChapterLink
from src.agent.toc.state import TocState
from src.agent.toc.tools import (
    ClaimInspector,
    EmbeddedStateExtractor,
    DomLinkExtractor,
    PaginatedTocCrawler,
    TocAuditor,
)
from src.agent.toc.graph import TocGraphWorkflow
from src.agent.toc.agent import TocAgent


# --- ClaimInspector Tests ---

def test_claim_inspector_japanese_badge():
    html = """
    <html><head><title>【書籍化】最強魔王の冒険 - カクヨム</title></head>
    <body>
        <div class="novel-meta">
            <span class="badge">連載中 全111話</span>
            <a class="author" href="/users/author1">魔王作家</a>
        </div>
    </body></html>
    """
    claimed, title, author, desc = ClaimInspector.inspect(html, "https://example.com/novel")
    assert claimed == 111
    assert title == "最強魔王の冒険"
    assert author == "魔王作家"


def test_claim_inspector_english_badge():
    html = """
    <html><head><title>Super Cultivator - Read Novel</title></head>
    <body>
        <p>Status: Ongoing, 350 Chapters</p>
    </body></html>
    """
    claimed, title, author, desc = ClaimInspector.inspect(html, "https://example.com/novel")
    assert claimed == 350
    assert "Super Cultivator" in title


def test_claim_inspector_json_props():
    html = """
    <html><head><title>Test Novel</title></head>
    <body>
        <script id="__NEXT_DATA__" type="application/json">
        {"props": {"pageProps": {"publicEpisodeCount": 99}}}
        </script>
    </body></html>
    """
    claimed, title, author, desc = ClaimInspector.inspect(html, "https://example.com/novel")
    assert claimed == 99


def test_claim_inspector_accordion_ranges():
    html = """
    <html><head><title>Accordion Novel</title></head>
    <body>
        <div class="accordions">
            <button>1〜30</button>
            <button>31〜60</button>
            <button>61〜90</button>
            <button>91〜111</button>
        </div>
    </body></html>
    """
    claimed, title, author, desc = ClaimInspector.inspect(html, "https://example.com/novel")
    assert claimed == 111


# --- EmbeddedStateExtractor Tests ---

def test_embedded_state_apollo_kakuyomu():
    apollo_payload = {
        "props": {
            "pageProps": {
                "__APOLLO_STATE__": {
                    "Work:12345": {
                        "id": "12345",
                        "title": "Apollo Novel",
                    },
                    "TableOfContentsChapter:": {
                        "episodeUnions": [
                            {"__ref": "Episode:ep1"},
                            {"__ref": "Episode:ep2"},
                            {"__ref": "Episode:ep3"},
                        ]
                    },
                    "Episode:ep1": {"id": "ep1", "title": "Chapter 1: The Start"},
                    "Episode:ep2": {"id": "ep2", "title": "Chapter 2: Midpoint"},
                    "Episode:ep3": {"id": "ep3", "title": "Chapter 3: Climax"},
                }
            }
        }
    }
    html = f"""
    <html><body>
        <script id="__NEXT_DATA__" type="application/json">{json.dumps(apollo_payload)}</script>
    </body></html>
    """
    chapters = EmbeddedStateExtractor.extract(html, "https://kakuyomu.jp/works/12345")
    assert len(chapters) == 3
    assert chapters[0].title == "Chapter 1: The Start"
    assert chapters[0].url == "https://kakuyomu.jp/works/12345/episodes/ep1"
    assert chapters[2].title == "Chapter 3: Climax"
    assert chapters[2].url == "https://kakuyomu.jp/works/12345/episodes/ep3"


def test_embedded_state_jsonld():
    jsonld_payload = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Chapter 1", "url": "/novel/ch1"},
            {"@type": "ListItem", "position": 2, "name": "Chapter 2", "url": "/novel/ch2"},
            {"@type": "ListItem", "position": 3, "name": "Chapter 3", "url": "/novel/ch3"},
            {"@type": "ListItem", "position": 4, "name": "Chapter 4", "url": "/novel/ch4"},
        ]
    }
    html = f"""
    <html><body>
        <script type="application/ld+json">{json.dumps(jsonld_payload)}</script>
    </body></html>
    """
    chapters = EmbeddedStateExtractor.extract(html, "https://example.com/novel")
    assert len(chapters) == 4
    assert chapters[0].url == "https://example.com/novel/ch1"
    assert chapters[3].title == "Chapter 4"


# --- DomLinkExtractor Tests ---

def test_dom_link_extractor_kakuyomu_selector():
    html = """
    <html><body>
        <div class="WorkTocSection_body">
            <a class="WorkTocSection_link" href="/works/123/episodes/1">
                <span class="WorkTocSection_title">Episode 1</span>
            </a>
            <a class="WorkTocSection_link" href="/works/123/episodes/2">
                <span class="WorkTocSection_title">Episode 2</span>
            </a>
        </div>
    </body></html>
    """
    chapters = DomLinkExtractor.extract(html, "https://kakuyomu.jp/works/123")
    assert len(chapters) == 2
    assert chapters[0].url == "https://kakuyomu.jp/works/123/episodes/1"
    assert chapters[0].title == "Episode 1"


def test_dom_link_extractor_heuristics():
    html = """
    <html><body>
        <div class="toc-container">
            <a href="/novel/1/episodes/101">第1話 はじまり</a>
            <a href="/novel/1/episodes/102">第2話 出会い</a>
            <a href="/novel/1/episodes/103">第3話 旅立ち</a>
            <a href="/bookmark">Bookmark</a>
        </div>
    </body></html>
    """
    chapters = DomLinkExtractor.extract(html, "https://example.com/novel/1")
    assert len(chapters) == 3
    assert chapters[0].title == "第1話 はじまり"
    assert chapters[2].title == "第3話 旅立ち"


# --- PaginatedTocCrawler Tests ---

def test_paginated_toc_crawler_detection():
    html = """
    <html><body>
        <div class="pagination">
            <a href="/novel/123?page=1">1</a>
            <a href="/novel/123?page=2">2</a>
            <a href="/novel/123?page=3">3</a>
            <a href="/novel/123?page=2">Next</a>
        </div>
    </body></html>
    """
    urls = PaginatedTocCrawler.detect_pagination_urls(html, "https://example.com/novel/123")
    assert len(urls) >= 2
    assert "https://example.com/novel/123?page=2" in urls
    assert "https://example.com/novel/123?page=3" in urls


# --- TocAuditor Tests ---

def test_toc_auditor_complete():
    chapters = [
        ChapterLink(index=i, title=f"Chapter {i}", url=f"https://example.com/ch/{i}")
        for i in range(1, 112)
    ]
    is_complete, conf, unexpanded, has_pag, issues = TocAuditor.audit(
        chapters=chapters,
        claimed_count=111,
        html="<div>Clean Page</div>",
    )
    assert is_complete is True
    assert conf == 1.0
    assert len(issues) == 0


def test_toc_auditor_discrepancy_detected():
    # Only 7 chapters extracted, but page claims 111
    chapters = [
        ChapterLink(index=i, title=f"Chapter {i}", url=f"https://example.com/ch/{i}")
        for i in range(1, 8)
    ]
    html = """
    <div>
        <button class="WorkTocAccordion_trigger">91〜111</button>
        <button>つづきを表示</button>
    </div>
    """
    is_complete, conf, unexpanded, has_pag, issues = TocAuditor.audit(
        chapters=chapters,
        claimed_count=111,
        html=html,
    )
    assert is_complete is False
    assert conf <= 0.1
    assert unexpanded is True
    assert any("discrepancy" in iss.lower() for iss in issues)


# --- LangGraph TocAgent Workflow Tests ---

@pytest.mark.asyncio
async def test_toc_agent_full_langgraph_flow(tmp_path):
    # Simulate an HTML page with both Apollo state (10 chapters) and claimed count of 10
    apollo_payload = {
        "props": {
            "pageProps": {
                "__APOLLO_STATE__": {
                    "Work:999": {"id": "999", "title": "Graph Verified Novel"},
                    "TableOfContentsChapter:": {
                        "episodeUnions": [{"__ref": f"Episode:{i}"} for i in range(1, 11)]
                    },
                    **{
                        f"Episode:{i}": {"id": f"{i}", "title": f"Episode {i}: Tale"}
                        for i in range(1, 11)
                    }
                }
            }
        }
    }
    html = f"""
    <html>
    <head><title>Graph Verified Novel - カクヨム</title></head>
    <body>
        <span>全10話</span>
        <script id="__NEXT_DATA__" type="application/json">{json.dumps(apollo_payload)}</script>
    </body>
    </html>
    """

    agent = TocAgent()
    state = await agent.extract_toc("https://kakuyomu.jp/works/999", html=html)

    assert state["is_complete"] is True
    assert len(state["extracted_chapters"]) == 10
    assert state["claimed_chapter_count"] == 10
    assert state["novel_title"] == "Graph Verified Novel"
    assert state["extraction_strategy"] == "embedded_state"
    assert state["confidence_score"] == 1.0

    # Test export to text
    out_file = tmp_path / "test_toc_export.txt"
    TocAgent.export_to_text(state, out_file)
    assert out_file.is_file()
    content = out_file.read_text(encoding="utf-8")
    assert "Graph Verified Novel" in content
    assert "Total Chapters: 10" in content
    assert "Episode 10: Tale" in content
