import pytest
from src.agent.classifier import ChapterLink
from src.agent.toc.tools import (
    ClaimInspector,
    DomLinkExtractor,
    PaginatedTocCrawler,
)
from src.agent.toc.agent import TocAgent


EMPIRE_SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>I Will Create A Good Ending For The Yandere Villainess read online | Empire Novel</title>
</head>
<body>
    <h1>I Will Create a Good Ending for the Yandere Villainess</h1>
    <div class="author-info">
        <a href="/novels-list?author=PagE_PickEr">PagE_PickEr</a>
    </div>
    <div class="col-6 text-center">
        <div>First ChapterChapter 1</div>
    </div>
    <div class="col-6 text-center">
        <div>Last ChapterChapter 665</div>
    </div>

    <!-- Chapter cards -->
    <a class="chapter_link" href="https://www.empirenovel.com/novel/i-will-create-a-good-ending-for-the-yandere-villainess/665" rel="nofollow">
        <div class="rounded-3 p-3 m-2 chapter position-relative">
            <div>
                Chapter&nbsp; 665
                <div class="small fst-italic">Jul 27, 2026</div>
            </div>
        </div>
    </a>
    <a class="chapter_link" href="https://www.empirenovel.com/novel/i-will-create-a-good-ending-for-the-yandere-villainess/664" rel="nofollow">
        <div class="rounded-3 p-3 m-2 chapter position-relative">
            <div>
                Chapter&nbsp; 664
                <div class="small fst-italic">Jul 20, 2026</div>
            </div>
        </div>
    </a>

    <!-- Pagination -->
    <ul class="pagination">
        <li class="page-item active"><span>1</span></li>
        <li class="page-item"><a href="https://www.empirenovel.com/novel/i-will-create-a-good-ending-for-the-yandere-villainess?page=2">2</a></li>
        <li class="page-item disabled"><span>...</span></li>
        <li class="page-item"><a href="https://www.empirenovel.com/novel/i-will-create-a-good-ending-for-the-yandere-villainess?page=23">23</a></li>
    </ul>
</body>
</html>
"""


def test_claim_inspector_empirenovel():
    claimed, title, author, desc = ClaimInspector.inspect(
        EMPIRE_SAMPLE_HTML,
        "https://www.empirenovel.com/novel/i-will-create-a-good-ending-for-the-yandere-villainess"
    )
    assert claimed == 665
    assert title == "I Will Create a Good Ending for the Yandere Villainess"
    assert author == "PagE_PickEr"


def test_dom_link_extractor_empirenovel():
    chapters = DomLinkExtractor.extract(
        EMPIRE_SAMPLE_HTML,
        "https://www.empirenovel.com/novel/i-will-create-a-good-ending-for-the-yandere-villainess"
    )
    assert len(chapters) == 2
    assert chapters[0].title == "Chapter 665"
    assert "Jul 27, 2026" not in chapters[0].title
    assert chapters[0].url == "https://www.empirenovel.com/novel/i-will-create-a-good-ending-for-the-yandere-villainess/665"
    assert chapters[1].title == "Chapter 664"


def test_paginated_crawler_empirenovel():
    pages = PaginatedTocCrawler.detect_pagination_urls(
        EMPIRE_SAMPLE_HTML,
        "https://www.empirenovel.com/novel/i-will-create-a-good-ending-for-the-yandere-villainess"
    )
    assert len(pages) == 22
    assert pages[0] == "https://www.empirenovel.com/novel/i-will-create-a-good-ending-for-the-yandere-villainess?page=2"
    assert pages[-1] == "https://www.empirenovel.com/novel/i-will-create-a-good-ending-for-the-yandere-villainess?page=23"


@pytest.mark.asyncio
async def test_live_empirenovel_crawl():
    url = "https://www.empirenovel.com/novel/i-will-create-a-good-ending-for-the-yandere-villainess"
    agent = TocAgent()
    try:
        state = await agent.extract_toc(url)
        assert state["is_complete"] is True
        assert state["extraction_strategy"] == "paginated_dom"
        assert len(state["extracted_chapters"]) >= 655
        assert state["extracted_chapters"][0].index == 1
        assert "Chapter 1" in state["extracted_chapters"][0].title
    finally:
        await agent.obscura.close()
