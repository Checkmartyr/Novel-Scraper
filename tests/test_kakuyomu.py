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

