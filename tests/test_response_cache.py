import threading
import asyncio
import pytest

from src.core.response_cache import ResponseCache


def test_cache_requires_exact_session_and_representation(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.store("https://example.test/a", "session-a", {"accept": "text/html"},
                {"content-type": "text/html", "etag": '"v1"'}, b"page")

    assert cache.load("https://example.test/a", "session-a", {"accept": "text/html"}).body == b"page"
    assert cache.load("https://example.test/a", "session-b", {"accept": "text/html"}) is None
    assert cache.load("https://example.test/a", "session-a", {"accept": "application/json"}) is None


def test_cache_honors_vary_and_no_store(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.store("https://example.test/a", "s", {"accept-language": "en"},
                {"vary": "Accept-Language"}, b"english")

    assert cache.load("https://example.test/a", "s", {"accept-language": "en"}).body == b"english"
    assert cache.load("https://example.test/a", "s", {"accept-language": "th"}) is None
    cache.store("https://example.test/b", "s", {}, {"cache-control": "private, no-store"}, b"x")
    assert cache.load("https://example.test/b", "s", {}) is None


def test_cache_metadata_never_persists_url_or_auth_secrets(tmp_path):
    cache = ResponseCache(tmp_path)
    url = "https://example.test/a?access_token=secret-value"
    cache.store(url, "session-digest", {"authorization": "Bearer request-secret"},
                {"etag": '"v1"', "set-cookie": "sid=response-secret"}, b"body")
    saved = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    assert "secret-value" not in saved
    assert "request-secret" not in saved
    assert "response-secret" not in saved


def test_observed_request_headers_drop_credentials():
    from src.core.obscura_client import _safe_network_headers

    headers = _safe_network_headers({
        "content-type": "application/json",
        "authorization": "Bearer secret",
        "x-api-key": "secret",
        "cookie": "sid=secret",
    })
    assert headers == {"content-type": "application/json"}


def test_cache_rejects_oversized_and_recovers_corrupt_entry(tmp_path):
    cache = ResponseCache(tmp_path, max_response_bytes=4)
    assert not cache.store("https://example.test/a", "s", {}, {}, b"12345")
    cache.store("https://example.test/a", "s", {}, {}, b"ok")
    path = next(tmp_path.glob("*.json"))
    path.write_text("{bad", encoding="utf-8")
    assert cache.load("https://example.test/a", "s", {}) is None


def test_304_merges_metadata_and_requires_matching_body(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.store("https://example.test/a", "s", {},
                {"etag": '"old"', "cache-control": "max-age=1", "content-type": "text/html"}, b"body")
    merged = cache.revalidate_304("https://example.test/a", "s", {},
                                  {"etag": '"new"', "cache-control": "max-age=60"})
    assert merged.body == b"body"
    assert merged.headers["etag"] == '"new"'
    assert cache.revalidate_304("https://example.test/missing", "s", {}, {}) is None


def test_cache_publication_is_safe_for_concurrent_writers(tmp_path):
    cache = ResponseCache(tmp_path)
    threads = [threading.Thread(target=cache.store,
                                args=("https://example.test/a", "s", {}, {}, value))
               for value in (b"one", b"two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert cache.load("https://example.test/a", "s", {}).body in {b"one", b"two"}
    assert len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_get_fetch_uses_validator_and_304_body(tmp_path):
    from src.core.obscura_client import ObscuraClient

    class FakeResponse:
        def __init__(self, status, headers, text=""):
            self.status_code, self.headers, self.text = status, headers, text
            self.url = "https://example.test/a"
            self.history = []
            self.content = text.encode()
            self.encoding = "utf-8"

    class FakeHttp:
        def __init__(self):
            self.calls = []
            self.headers = {"accept": "*/*", "user-agent": "test"}
            self.cookies = type("Jar", (), {"jar": []})()

        async def get(self, url, headers=None):
            self.calls.append(headers or {})
            if len(self.calls) == 1:
                return FakeResponse(200, {"etag": '"x"', "content-type": "text/html"}, "<html>" + ("fresh page " * 30) + "</html>")
            return FakeResponse(304, {"etag": '"x"'})

    client = ObscuraClient(state_dir=tmp_path)
    client._http_client = FakeHttp()
    client.pace = lambda url: asyncio.sleep(0)
    first = await client.fetch_html("https://example.test/a", refresh=True)
    second = await client.fetch_html("https://example.test/a", refresh=True)
    assert first == second
    assert client._http_client.calls[1]["If-None-Match"] == '"x"'
