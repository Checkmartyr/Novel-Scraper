"""Offline tests never inherit host credentials, recipes, or novel output."""
import os
import tempfile
import pytest

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
if os.getenv("NOVEL_SCRAPER_LIVE_TESTS") != "1":
    os.environ["NOVEL_SCRAPER_MODEL"] = "mock-scraper"
    os.environ["NOVEL_MODEL"] = "mock-model"
    os.environ["GEMINI_API_KEY"] = ""
    os.environ["NOVEL_FALLBACK_MODEL"] = "mock-fallback"
_runtime = tempfile.TemporaryDirectory(prefix="scraper-tests-")
os.environ["RECIPES_DIR"] = _runtime.name
os.environ["OUTPUT_DIR"] = _runtime.name
os.environ["LOGS_DIR"] = _runtime.name


def pytest_configure(config):
    config.addinivalue_line("markers", "live: opt-in external website acceptance")


def pytest_collection_modifyitems(items):
    names = {"test_kakuyomu_toc_agent_autonomous", "test_syosetu_live_toc_agent",
             "test_freewebnovel_toc_extraction", "test_freewebnovel_chapter_fetch", "test_live_empirenovel_crawl",
             "test_obscura_playwright_networkidle"}
    for item in items:
        if item.name in names or item.name.startswith("test_live_"):
            item.add_marker(pytest.mark.live)
        if item.get_closest_marker("live") and os.getenv("NOVEL_SCRAPER_LIVE_TESTS") != "1":
            item.add_marker(pytest.mark.skip(reason="Set NOVEL_SCRAPER_LIVE_TESTS=1 for live acceptance"))


@pytest.fixture(autouse=True)
def isolated_recipes(tmp_path, monkeypatch):
    from src.agent.domain_memory import DomainMemoryManager
    monkeypatch.setattr("src.agent.domain_memory.domain_memory", DomainMemoryManager(tmp_path / "recipes"))


@pytest.fixture(autouse=True)
def offline_network_guard(request, monkeypatch):
    if request.node.get_closest_marker("live") and os.getenv("NOVEL_SCRAPER_LIVE_TESTS") == "1":
        return
    import httpx
    from contextlib import asynccontextmanager
    original_send = httpx.AsyncClient.send
    async def guarded_send(client, outbound, *args, **kwargs):
        if outbound.url.host not in {"127.0.0.1", "localhost", "::1"} and not isinstance(client._transport_for_url(outbound.url), httpx.MockTransport):
            raise RuntimeError("External HTTP requires a mocked transport or an opt-in live test")
        return await original_send(client, outbound, *args, **kwargs)
    @asynccontextmanager
    async def unavailable_browser(*args, **kwargs):
        raise RuntimeError("Browser access requires an explicit mock or an opt-in live test")
        yield
    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send)
    monkeypatch.setattr("src.core.obscura_client.ObscuraClient.get_playwright_page", unavailable_browser)
