import hashlib

import pytest

from src.agent.classifier import ChapterLink
from src.agent.ch.analyzer import DOMStructurePlan
from src.agent.ch.code_generator import ExtractedChapter
from src.scraper.batch_runner import BatchScraperRunner
from src.scraper.runtime import ChapterCheckpoint
from src.scraper.storage import NovelStorage


class RefreshFetcher:
    async def fetch_html(self, url, **kwargs):
        assert kwargs.get("refresh") is True
        return "<html><h1>Chapter 1</h1><article><p>Updated source.</p></article></html>"

    async def replay_api_recipe(self, recipe):
        return {"title": "Chapter 1", "content": ["API paragraph one.", "API paragraph two."]}


def _result(text):
    return ExtractedChapter(title="Chapter 1", content_markdown=text, word_count=2,
                            char_count=len(text), success=True)


@pytest.mark.asyncio
async def test_refresh_reports_source_update_without_overwriting(tmp_path, monkeypatch):
    storage = NovelStorage(base_output_dir=tmp_path / "out", direct_output=True,
                           overwrite=False, state_dir=tmp_path / "state")
    old_file = await storage.save_chapter("Novel", 1, "Chapter 1", _result("Old source."),
                                          "https://fixture.test/book/1", content_sha256="old-digest")
    old_bytes = old_file.read_bytes()
    runner = BatchScraperRunner("Novel", DOMStructurePlan(title_selector="h1", content_selector="article"),
                                obscura_client=RefreshFetcher(), storage=storage, refresh=True)
    monkeypatch.setattr(runner, "_verified_extract", lambda *args, **kwargs: _result("Updated source."))
    outcome = await runner._scrape_chapter(ChapterLink(index=1, title="Chapter 1", url="https://fixture.test/book/1"))

    assert outcome["status"] == "source_update_available"
    assert old_file.read_bytes() == old_bytes
    assert hashlib.sha256(old_file.read_bytes()).hexdigest() == storage.sources["https://fixture.test/book/1"]["sha256"]


@pytest.mark.asyncio
async def test_refresh_protects_local_edits_even_when_overwrite_is_enabled(tmp_path, monkeypatch):
    storage = NovelStorage(base_output_dir=tmp_path / "out", direct_output=True,
                           overwrite=True, state_dir=tmp_path / "state")
    old_file = await storage.save_chapter("Novel", 1, "Chapter 1", _result("Old source."),
                                          "https://fixture.test/book/1", content_sha256="old-digest")
    old_file.write_text("my local edit", encoding="utf-8")
    runner = BatchScraperRunner("Novel", DOMStructurePlan(title_selector="h1", content_selector="article"),
                                obscura_client=RefreshFetcher(), storage=storage, refresh=True)
    monkeypatch.setattr(runner, "_verified_extract", lambda *args, **kwargs: _result("Updated source."))
    outcome = await runner._scrape_chapter(ChapterLink(index=1, title="Chapter 1", url="https://fixture.test/book/1"))

    assert outcome["status"] == "local_modified"
    assert old_file.read_text(encoding="utf-8") == "my local edit"


@pytest.mark.asyncio
async def test_chapter_api_recipe_replay_produces_auditable_page(tmp_path, monkeypatch):
    from src.agent.api_recipes import ApiRecipe
    from src.agent.domain_memory import DomainMemoryManager

    memory = DomainMemoryManager(tmp_path / "recipes")
    recipe = ApiRecipe(method="GET", endpoint="https://fixture.test/api/chapter/1",
        response_mapping={"title": "title", "content": "content"},
        representation="paragraphs", page_evidence="https://fixture.test/book/1")
    assert memory.promote_api_recipe("https://fixture.test/book/1", recipe,
        toc_complete=True, novel_identity_verified=True, quality_score=0.95)
    monkeypatch.setattr("src.agent.domain_memory.domain_memory", memory)
    runner = BatchScraperRunner("Novel", DOMStructurePlan(title_selector="h1", content_selector="article"),
                                obscura_client=RefreshFetcher(), storage=NovelStorage(base_output_dir=tmp_path / "out"))

    snapshot = await runner._api_chapter_snapshot(ChapterLink(index=1, title="Chapter 1", url="https://fixture.test/book/1"))

    assert snapshot is not None
    html, provenance = snapshot
    assert "API paragraph one." in html and "API paragraph two." in html
    assert provenance["kind"] == "api"


@pytest.mark.asyncio
async def test_refresh_resume_skips_a_verified_chapter_in_same_generation(tmp_path):
    url = "https://fixture.test/book/1"
    storage = NovelStorage(base_output_dir=tmp_path / "out", direct_output=True,
                           overwrite=False, state_dir=tmp_path / "state")
    path = storage.base_output_dir / "0001 - Chapter 1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Chapter 1\n\nPreviously verified.", encoding="utf-8")
    checkpoint = ChapterCheckpoint(tmp_path / "state", "job")
    checkpoint.save(url, {"url": url, "status": "saved", "file": path.name,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "refresh_generation": "generation-a"})

    class NoFetch:
        async def fetch_html(self, *args, **kwargs):
            raise AssertionError("a verified chapter from the active refresh generation should resume")

    runner = BatchScraperRunner("Novel", DOMStructurePlan(title_selector="h1", content_selector="article"),
        obscura_client=NoFetch(), storage=storage, checkpoint=checkpoint, refresh=True)
    runner.refresh_generation = "generation-a"
    outcome = await runner._scrape_chapter(ChapterLink(index=1, title="Chapter 1", url=url))

    assert outcome["status"] == "skipped"
