from src.core.page_analysis import PageAnalysis, PageAnalysisCache
from src.agent.ch.analyzer import DOMStructurePlan
from src.agent.ch.code_generator import ChapterCodeGenerator


def test_page_analysis_keeps_source_evidence_unchanged_when_a_working_copy_is_cleaned():
    analysis = PageAnalysis('<article><p>Story text.</p><aside>Keep as evidence</aside></article>')

    working = analysis.copy_soup()
    working.select_one("aside").decompose()

    assert analysis.select_one("aside").get_text(strip=True) == "Keep as evidence"
    assert analysis.select_one("article").get_text(" ", strip=True) == "Story text. Keep as evidence"


def test_page_analysis_cache_keeps_only_the_latest_twelve_snapshots():
    cache = PageAnalysisCache(max_snapshots=12)
    for number in range(13):
        cache.get(str(number), f"<p>{number}</p>")

    assert cache.get("0", "<p>0</p>").select_one("p").get_text(strip=True) == "0"
    assert len(cache) == 12


def test_snapshot_reuses_parse_tree_and_selector_results():
    cache = PageAnalysisCache()
    first = cache.get("chapter", "<article><p>One</p></article>")
    first.select("article p")
    first.select("article p")
    second = cache.get("chapter", "<article><p>One</p></article>")

    assert first is second
    assert first.metrics == {"tree_builds": 1, "selector_queries": 1, "selector_cache_hits": 1}
    assert cache.metrics == {"hits": 1, "misses": 1}


def test_chapter_extraction_removals_do_not_change_shared_source_evidence():
    html = '<h1>Chapter 4</h1><article><p>First line <em>continues</em>.</p><aside>noise</aside><p>The ending stays.</p></article>'
    analysis = PageAnalysis(html)
    generator = ChapterCodeGenerator(DOMStructurePlan(
        title_selector="h1", content_selector="article", remove_selectors=["aside"]))

    extracted = generator.extract(html, page_analysis=analysis)
    verified = generator.test_and_verify(html, extracted, page_analysis=analysis)
    namespace = {}
    exec(verified.generated_code, namespace)
    generated = namespace["extract_chapter"](html)

    assert extracted.content_markdown == "First line continues.\n\nThe ending stays."
    assert generated["content"] == extracted.content_markdown
    assert generated["title"] == extracted.title
    assert analysis.select_one("aside").get_text(strip=True) == "noise"
    assert not verified.coverage_issues
