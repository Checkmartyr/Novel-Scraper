import pytest
from src.agent.classifier import PageClassifier
from src.agent.analyzer import ChapterAnalyzer, DOMStructurePlan
from src.agent.code_generator import ChapterCodeGenerator

def test_clean_novel_title():
    classifier = PageClassifier()
    raw = "【2月28日、書籍第一巻発売！！】悪役貴族による弱小領地の革命開拓 - カクヨム"
    cleaned = classifier._clean_novel_title(raw)
    assert cleaned == "悪役貴族による弱小領地の革命開拓"

def test_heuristic_kakuyomu_chapter_analyzer():
    html = """
    <html>
    <head><title>第1話　悪役貴族、姉妹ができる - カクヨム</title></head>
    <body>
        <p class="widget-episodeTitle js-vertical-composition-item">第1話　悪役貴族、姉妹ができる</p>
        <div class="widget-episodeBody js-episode-body">
            <p>「は、話がある」夕食時、挙動不審な父上が唐突にそう言った。</p>
            <p>僕の名前はクノウ・ドラーナ。十歳。ガルダナキア王国の辺境にあるドラーナ男爵家の嫡男である。</p>
            <p>要するに貴族なわけだが、屋敷こそ立派なものの、男爵家はとんでもなく貧乏だった。領地経営も火の車である。</p>
            <p>父上は言った。「実はお前に、新しい義理の姉妹ができることになったんだ」</p>
        </div>
    </body>
    </html>
    """
    analyzer = ChapterAnalyzer()
    plan = analyzer._heuristic_analyze(html)
    assert plan.title_selector in [".widget-episodeTitle", "h1", "h2", "title"]
    assert "widget-episodeBody" in plan.content_selector

    generator = ChapterCodeGenerator(plan)
    verif = generator.test_and_verify(html)
    assert verif.success is True
    assert "第1話" in verif.chapter_title
    assert "夕食時" in verif.preview_markdown

@pytest.mark.asyncio
async def test_kakuyomu_toc_agent_autonomous():
    from src.agent.toc.agent import TocAgent
    agent = TocAgent()
    try:
        state = await agent.extract_toc("https://kakuyomu.jp/works/16817330663722833570")
        assert len(state["extracted_chapters"]) == 111
        assert state["claimed_chapter_count"] == 111
        assert state["is_complete"] is True
        assert state["confidence_score"] == 1.0
        assert "悪役貴族" in state["novel_title"]
        assert state["author"] == "ナガワ　ヒイロ"
        assert state["extracted_chapters"][0].url == "https://kakuyomu.jp/works/16817330663722833570/episodes/16817330663722869163"
        assert state["extracted_chapters"][-1].url == "https://kakuyomu.jp/works/16817330663722833570/episodes/2912051595229036232"
    finally:
        await agent.obscura.close()


@pytest.mark.asyncio
async def test_saved_kakuyomu_recipe_stays_with_requested_work(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from typing import cast

    from src.agent.analyzer import DOMStructurePlan
    from src.agent.classifier import PageClassifier
    from src.agent.domain_memory import DomainMemoryManager
    from src.agent.llm import LLMClient
    import src.agent.domain_memory as dm_module

    old_work_url = "https://kakuyomu.jp/works/16818093077104932645"
    requested_work_url = "https://kakuyomu.jp/works/822139845524406618"
    manager = DomainMemoryManager(recipes_dir=tmp_path)
    manager.save_recipe(
        domain_or_url=old_work_url,
        sample_toc_url=old_work_url,
        sample_chapter_url=f"{old_work_url}/episodes/old-episode",
        toc_strategy="embedded_state",
        chapter_plan=DOMStructurePlan(title_selector="h1", content_selector="body"),
        quality_score=0.5,
    )
    monkeypatch.setattr(dm_module, "domain_memory", manager)
    monkeypatch.setattr(
        manager,
        "test_chapter_recipe",
        lambda recipe, html: SimpleNamespace(success=True, chapter_title="Cached recipe"),
    )

    class UnavailableLLM:
        is_available = False

    class MixedTocAgent:
        async def extract_toc(self, url, html=None):
            return {
                "extracted_chapters": [
                    ChapterLink(index=1, title="Wrong work", url=f"{old_work_url}/episodes/old-episode"),
                    ChapterLink(index=2, title="This work", url=f"{requested_work_url}/episodes/new-episode"),
                ],
                "novel_title": "Requested Work",
            }

    from src.agent.classifier import ChapterLink

    classifier = PageClassifier(
        llm_client=cast(LLMClient, UnavailableLLM()),
        toc_agent=MixedTocAgent(),
    )
    toc_html = f"""
    <html><head><title>Requested Work - カクヨム</title></head><body>
      <h1>Requested Work</h1><span>全56話</span>
      <a class="WorkTocSection_link" href="{old_work_url}/episodes/old-episode">
        <span class="WorkTocSection_title">Wrong work episode</span>
      </a>
      <a class="WorkTocSection_link" href="{requested_work_url}/episodes/new-episode">
        <span class="WorkTocSection_title">第1話</span>
      </a>
    </body></html>
    """

    toc_result = await classifier.classify(requested_work_url, toc_html)

    assert toc_result.page_type == "TOC"
    assert toc_result.toc_url is None
    assert len(toc_result.chapter_links) == 1
    assert toc_result.toc_state["extracted_chapters"] == toc_result.chapter_links
    assert all(
        chapter.url.startswith(requested_work_url + "/episodes/")
        for chapter in toc_result.chapter_links
    )

    episode_url = f"{requested_work_url}/episodes/new-episode"
    episode_html = """
    <html><head><title>第1話 - Requested Work</title></head><body>
      <h1>第1話</h1><p>A chapter from the requested work.</p>
    </body></html>
    """
    episode_result = await classifier.classify(episode_url, episode_html)

    assert episode_result.page_type == "CHAPTER"
    assert episode_result.toc_url == requested_work_url
