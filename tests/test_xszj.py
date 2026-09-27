import pytest
from unittest.mock import AsyncMock, MagicMock
from bs4 import BeautifulSoup
from src.agent.analyzer import DOMStructurePlan
from src.agent.code_generator import ChapterCodeGenerator, ExtractedChapter
from src.agent.classifier import PageClassifier, ChapterLink
from src.scraper.batch_runner import BatchScraperRunner
from src.scraper.storage import NovelStorage, sanitize_filename
from src.utils.romanizer import romanize_text
from src.core.obscura_client import ObscuraClient

SAMPLE_CH1_P1_HTML = """
<!DOCTYPE html>
<html>
<head><title>第1章 这是个超能力世界 （1/3）-我绑定了灭世大BOSS-小说之家</title></head>
<body>
  <h1 class="bookname">第1章 这是个超能力世界 （1/3）</h1>
  <div id="content">
    <p>朝霞流转，金光万道。</p>
    <p>今天正是新生入学的时间，琉璃能力者学院外长长的坡道两旁，樱花树正值盛开。</p>
  </div>
  <div class="readbtn">
    <a href="/b/352734/c/6042804?page=2">下一页</a>
  </div>
</body>
</html>
"""

SAMPLE_CH1_P2_HTML = """
<!DOCTYPE html>
<html>
<head><title>第1章 这是个超能力世界 （2/3）-我绑定了灭世大BOSS-小说之家</title></head>
<body>
  <h1 class="bookname">第1章 这是个超能力世界 （2/3）</h1>
  <div id="content">
    <p>少女的肌肤是如此白皙，柔和阳光的照射之下更是显得白皙无瑕。</p>
    <p>所以龙宫奈奈激动，纯粹是因为七海织姬终于要开始带飞她了。</p>
  </div>
  <div class="readbtn">
    <a href="/b/352734/c/6042804?page=3">下一页</a>
  </div>
</body>
</html>
"""

SAMPLE_CH1_P3_HTML = """
<!DOCTYPE html>
<html>
<head><title>第1章 这是个超能力世界 （3/3）-我绑定了灭世大BOSS-小说之家</title></head>
<body>
  <h1 class="bookname">第1章 这是个超能力世界 （3/3）</h1>
  <div id="content">
    <p>班级：自然系七班。</p>
    <p>琉璃学院作为七都的知名能力者学院，经费是出了名的充足。</p>
  </div>
  <div class="readbtn">
    <a href="/b/352734/c/6042808">下一章</a>
  </div>
</body>
</html>
"""

def test_chinese_novel_romanization():
    title = "我绑定了灭世大BOSS"
    romanized = romanize_text(title)
    assert romanized == "WoBangDingLeMieShiDaBOSS"


def test_clean_chapter_title_pagination():
    raw_title = "第1章 这是个超能力世界 （1/3）"
    clean_title = sanitize_filename("第1章 这是个超能力世界 （1/3）")
    # Storage cleans pagination
    storage = NovelStorage()
    plan = DOMStructurePlan(
        title_selector="h1.bookname",
        content_selector="div#content",
    )
    gen = ChapterCodeGenerator(plan)
    res = gen.extract(SAMPLE_CH1_P1_HTML)
    assert "（1/3）" in res.title or res.title == "第1章 这是个超能力世界"


@pytest.mark.asyncio
async def test_batch_runner_subpage_stitching(tmp_path):
    plan = DOMStructurePlan(
        title_selector="h1.bookname",
        content_selector="div#content",
        remove_selectors=["div.readbtn"],
    )

    # Mock obscura to return P1, P2, P3
    mock_obscura = MagicMock(spec=ObscuraClient)
    async def fake_fetch(url: str, **kwargs):
        if "page=2" in url:
            return SAMPLE_CH1_P2_HTML
        elif "page=3" in url:
            return SAMPLE_CH1_P3_HTML
        return SAMPLE_CH1_P1_HTML
    mock_obscura.fetch_html = AsyncMock(side_effect=fake_fetch)

    storage = NovelStorage(base_output_dir=tmp_path)
    runner = BatchScraperRunner(
        novel_title="我绑定了灭世大BOSS",
        initial_plan=plan,
        obscura_client=mock_obscura,
        storage=storage,
    )

    generator = ChapterCodeGenerator(plan)
    initial_res = generator.extract(SAMPLE_CH1_P1_HTML)
    stitched_res = await runner._stitch_subpages(
        initial_url="https://xszj.org/b/352734/c/6042804",
        initial_html=SAMPLE_CH1_P1_HTML,
        initial_result=initial_res,
    )

    # Title should have (1/3) stripped
    assert stitched_res.title == "第1章 这是个超能力世界"
    # Content should contain text from all 3 pages
    assert "朝霞流转" in stitched_res.content_markdown
    assert "龙宫奈奈" in stitched_res.content_markdown
    assert "自然系七班" in stitched_res.content_markdown
    assert stitched_res.success is True
