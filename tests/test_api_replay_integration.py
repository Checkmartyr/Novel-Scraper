from src.agent.toc.discovery import (
    infer_api_response_mapping,
    network_pagination_requests,
    replay_api_catalog,
)
from src.agent.api_recipes import capture_api_recipe
from src.agent.toc.discovery import capture_observed_api_recipe
from src.core.obscura_client import _safe_network_headers


def test_api_capture_mapping_discovers_catalog_and_chapter_fields():
    response = {"data": {"book": {"chapters": [
        {"chapterTitle": "Chapter 1", "chapterUrl": "/book/7/1"},
    ]}}}
    assert infer_api_response_mapping(response) == {
        "mapping": {"chapters": "data.book.chapters", "title": "chapterTitle", "url": "chapterUrl"},
        "representation": "json",
    }


def test_capture_metadata_keeps_safe_request_context_and_drops_credentials():
    headers = _safe_network_headers({"content-type": "application/json", "authorization": "Bearer hidden",
                                    "cookie": "session=hidden", "x-api-key": "hidden"})
    recipe = capture_observed_api_recipe(method="GET", url="https://reader.example/api/book/7/chapters",
        headers=headers, response_body={"items": [{"name": "One", "href": "/book/7/1"}]},
        page_url="https://reader.example/book/7",
        mapping={"chapters": "items", "title": "name", "url": "href"})
    assert recipe is not None
    assert recipe.request_headers == {"content-type": "application/json"}
    assert recipe.page_evidence == "https://reader.example/book/7"
    assert "hidden" not in recipe.model_dump_json()
    chapter = {"data": {"chapter": {"title": "Chapter 1", "content": ["One", "Two"]}}}
    assert infer_api_response_mapping(chapter) == {
        "mapping": {"title": "data.chapter.title", "content": "data.chapter.content"},
        "representation": "paragraphs",
    }


def test_post_cursor_frontier_preserves_method_body_bindings_and_progresses():
    first_body = {"query": "query Chapters($cursor: String) { chapters(cursor: $cursor) { title url } }",
                  "variables": {"cursor": "first"}}
    observed = [{"url": "https://reader.example/graphql", "method": "POST",
                 "post_data": first_body, "headers": {"content-type": "application/json"},
                 "source_page_url": "https://reader.example/book/7",
                 "trusted_read_only": True,
                 "data": {"data": {"chapters": [{"title": "Chapter 1", "url": "/book/7/1"}],
                                    "nextCursor": "second"}}}]

    frontier = network_pagination_requests(observed)
    assert len(frontier) == 1
    assert frontier[0]["method"] == "POST"
    assert frontier[0]["url"] == "https://reader.example/graphql"
    assert frontier[0]["body"]["variables"]["cursor"] == "second"
    assert first_body["variables"]["cursor"] == "first"
    assert network_pagination_requests([{**observed[0], "post_data": frontier[0]["body"],
                                         "data": {"data": {"chapters": [], "nextCursor": "third"}}}])[0]["body"]["variables"]["cursor"] == "third"


def test_catalog_replay_rejects_stale_schema_and_keeps_browser_fallback_available():
    recipe = capture_api_recipe(method="GET", url="https://reader.example/api/book/7/chapters",
        headers={"accept": "application/json"}, response_body={"items": [{"name": "One", "href": "/book/7/1"}]},
        page_url="https://reader.example/book/7",
        mapping={"chapters": "items", "title": "name", "url": "href"})
    assert replay_api_catalog(recipe, {"items": [{"name": "One", "href": "/book/7/1"}]},
                              "https://reader.example/book/7")[0].url == "https://reader.example/book/7/1"
    assert replay_api_catalog(recipe, {"unexpected": []}, "https://reader.example/book/7") is None
