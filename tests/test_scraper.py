import pytest
import asyncio
import json
from pathlib import Path

from src.core.binary_manager import find_obscura, ensure_obscura
from src.core.obscura_client import ObscuraClient
from src.agent.classifier import PageClassifier, ChapterLink
from src.agent.analyzer import ChapterAnalyzer, DOMStructurePlan
from src.agent.code_generator import ChapterCodeGenerator, ExtractedChapter
from src.scraper.storage import NovelStorage, sanitize_filename

SAMPLE_CHAPTER_HTML = """
<!DOCTYPE html>
<html>
<head><title>My Awesome Novel - Chapter 1: The Beginning</title></head>
<body>
  <div class="nav-bar">
    <a href="/novel/awesome-novel">Table of Contents</a>
    <a href="/novel/awesome-novel/chapter-2">Next Chapter</a>
  </div>
  <div class="content-wrapper">
    <h1 class="chapter-title">Chapter 1: The Beginning</h1>
    <div class="ad-banner">Ad here</div>
    <div id="chapter-content">
      <p>The wind howled across the desolate plains as the young cultivator stood at the peak of the mountain.</p>
      <p>Ten thousand years had passed since the Great War, yet the scars on the land remained unhealed.</p>
      <p>He held the ancient jade pendant tightly in his hand, feeling the subtle warmth emanating from within.</p>
      <p>"It is time," he whispered, stepping forward into the celestial vortex.</p>
    </div>
    <div class="comments">Comments section</div>
  </div>
</body>
</html>
"""

SAMPLE_TOC_HTML = """
<!DOCTYPE html>
<html>
<head><title>My Awesome Novel - Table of Contents</title></head>
<body>
  <h1>My Awesome Novel</h1>
  <p class="author">Author: Immortal Dreamer</p>
  <div class="chapter-list">
    <a href="/chapter-1">Chapter 1: The Beginning</a>
    <a href="/chapter-2">Chapter 2: The Path Ahead</a>
    <a href="/chapter-3">Chapter 3: Encounter with Beasts</a>
    <a href="/chapter-4">Chapter 4: The Ancient Temple</a>
    <a href="/chapter-5">Chapter 5: Breakthrough</a>
    <a href="/chapter-6">Chapter 6: Return to the Sect</a>
  </div>
</body>
</html>
"""

def test_sanitize_filename():
    assert sanitize_filename('Chapter 1: "The Beginning"?') == "Chapter 1 The Beginning"
    assert sanitize_filename("Path/To\\Invalid:Name*") == "PathToInvalidName"

def test_binary_manager():
    bin_path = find_obscura()
    assert bin_path is not None
    assert bin_path.is_file()

def test_heuristic_classifier_toc():
    classifier = PageClassifier()
    summary = classifier._extract_page_summary(SAMPLE_TOC_HTML, "https://example.com/novel")
    result = classifier._heuristic_classify("https://example.com/novel", SAMPLE_TOC_HTML, summary)
    
    assert result.page_type == "TOC"
    assert result.novel_title == "My Awesome Novel"
    assert len(result.chapter_links) == 6

def test_heuristic_classifier_chapter():
    classifier = PageClassifier()
    summary = classifier._extract_page_summary(SAMPLE_CHAPTER_HTML, "https://example.com/novel/chapter-1")
    result = classifier._heuristic_classify("https://example.com/novel/chapter-1", SAMPLE_CHAPTER_HTML, summary)
    
    assert result.page_type == "CHAPTER"
    assert result.next_chapter_url == "https://example.com/novel/awesome-novel/chapter-2"
    assert result.toc_url == "https://example.com/novel/awesome-novel"

def test_analyzer_and_code_generator():
    analyzer = ChapterAnalyzer()
    plan = analyzer._heuristic_analyze(SAMPLE_CHAPTER_HTML)
    
    assert "chapter-title" in plan.title_selector or "h1" in plan.title_selector
    assert "chapter-content" in plan.content_selector
    
    generator = ChapterCodeGenerator(plan)
    verification = generator.test_and_verify(SAMPLE_CHAPTER_HTML)
    
    assert verification.success is True
    assert "Beginning" in verification.chapter_title
    assert verification.word_count > 30
    assert "wind howled" in verification.preview_markdown
    assert "Ad here" not in verification.preview_markdown

@pytest.mark.asyncio
async def test_storage(tmp_path):
    storage = NovelStorage(base_output_dir=tmp_path)
    
    chapter = ExtractedChapter(
        title="Chapter 1: The Beginning",
        content_markdown="Paragraph one.\n\nParagraph two.",
        word_count=4,
        char_count=28,
        success=True
    )
    
    saved_file = await storage.save_chapter(
        novel_title="Test Novel",
        chapter_index=1,
        chapter_title="Chapter 1: The Beginning",
        chapter=chapter,
        source_url="https://example.com/ch1"
    )
    
    assert saved_file.is_file()
    content = saved_file.read_text(encoding="utf-8")
    assert "novel: \"Test Novel\"" not in content
    assert "---" not in content
    assert "# Chapter 1 The Beginning" in content
    assert "Paragraph one." in content
    
    # Test with include_frontmatter=True
    storage_fm = NovelStorage(base_output_dir=tmp_path, include_frontmatter=True)
    saved_fm = await storage_fm.save_chapter(
        novel_title="Test Novel FM",
        chapter_index=2,
        chapter_title="Chapter 2: FM",
        chapter=chapter,
        source_url="https://example.com/ch2"
    )
    content_fm = saved_fm.read_text(encoding="utf-8")
    assert "novel: \"Test Novel FM\"" in content_fm
    assert "chapter_number: 2" in content_fm
    
    meta_file = await storage.save_metadata(
        novel_title="Test Novel",
        source_url="https://example.com",
        author="Tester",
        total_chapters=1,
        completed_chapters=1
    )
    assert meta_file.is_file()

@pytest.mark.asyncio
async def test_save_metadata_with_chapter_links_and_merge(tmp_path):
    storage = NovelStorage(base_output_dir=tmp_path)
    
    links = [
        ChapterLink(index=1, title="Chapter 1: Start", url="https://example.com/1"),
        ChapterLink(index=2, title="Chapter 2: Mid", url="https://example.com/2"),
        ChapterLink(index=3, title="Chapter 3: End", url="https://example.com/3"),
    ]
    
    meta_path = await storage.save_metadata(
        novel_title="Test Odyssey",
        source_url="https://example.com/novel",
        author="Great Author",
        description="Epic journey",
        total_chapters=3,
        chapter_index_list=links,
        extraction_strategy="paginated_dom",
        audit_passed=True,
    )
    assert meta_path.is_file()
    
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert data["novel_title"] == "Test Odyssey"
    assert data["author"] == "Great Author"
    assert data["total_chapters"] == 3
    assert data["completed_chapters"] == 0
    assert data["extraction_strategy"] == "paginated_dom"
    assert data["audit_passed"] is True
    assert "created_at" in data
    assert len(data["chapters"]) == 3
    assert data["chapters"][0]["title"] == "Chapter 1: Start"
    assert data["chapters"][0]["url"] == "https://example.com/1"
    assert data["chapters"][0]["downloaded"] is False
    assert data["chapters"][0]["success"] is False
    
    orig_created_at = data["created_at"]
    
    # Simulate update during batch downloading
    updated_records = [
        {"index": 1, "title": "Chapter 1: Start", "url": "https://example.com/1", "success": True, "downloaded": True},
        {"index": 2, "title": "Chapter 2: Mid", "url": "https://example.com/2", "success": True, "downloaded": True},
        {"index": 3, "title": "Chapter 3: End", "url": "https://example.com/3", "success": False, "downloaded": False},
    ]
    
    meta_path_updated = await storage.save_metadata(
        novel_title="Test Odyssey",
        source_url="https://example.com/novel",
        completed_chapters=2,
        chapter_index_list=updated_records,
    )
    
    data_updated = json.loads(meta_path_updated.read_text(encoding="utf-8"))
    assert data_updated["created_at"] == orig_created_at
    assert data_updated["author"] == "Great Author"
    assert data_updated["extraction_strategy"] == "paginated_dom"
    assert data_updated["completed_chapters"] == 2
    assert data_updated["chapters"][0]["downloaded"] is True
    assert data_updated["chapters"][0]["success"] is True

def test_langchain_interactions_model_conversion():
    from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
    from src.agent.interactions_model import ChatGeminiInteractions
    
    model = ChatGeminiInteractions(model="gemini-2.5-flash")
    messages = [
        SystemMessage(content="System rule"),
        HumanMessage(content="User input 1"),
        AIMessage(content="Assistant answer 1"),
        HumanMessage(content="User input 2"),
    ]
    sys_inst, inputs = model._convert_messages(messages)
    assert sys_inst == "System rule"
    assert len(inputs) == 3
    assert inputs[0]["type"] == "user_input"
    assert inputs[1]["type"] == "model_output"
    assert inputs[2]["type"] == "user_input"

def test_cli_auto_without_url_raises(monkeypatch):
    import sys
    from src.main import main
    monkeypatch.setattr(sys, "argv", ["novel-scraper", "--auto"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code != 0

def test_obscura_github_repo_env_override(monkeypatch):
    monkeypatch.setenv("OBSCURA_GITHUB_REPO", "custom-org/custom-obscura")
    import importlib
    import src.config
    importlib.reload(src.config)
    assert src.config.OBSCURA_GITHUB_REPO == "custom-org/custom-obscura"
    # Restore
    monkeypatch.delenv("OBSCURA_GITHUB_REPO", raising=False)
    importlib.reload(src.config)
    assert src.config.OBSCURA_GITHUB_REPO == "h4ckf0r0day/obscura"

@pytest.mark.asyncio
async def test_obscura_playwright_networkidle():
    client = ObscuraClient()
    try:
        html = await client.fetch_html("https://example.com", wait_until="networkidle")
        assert "<html" in html.lower()
        assert "example domain" in html.lower()
    finally:
        await client.close()

