import json

import pytest

from src.agent.api_recipes import (
    ApiRecipe,
    capture_api_recipe,
    graphql_query_is_replayable,
    render_api_chapter,
    replay_api_chapter,
)
from src.agent.domain_memory import DomainMemoryManager
from src.handlers.base import BasePlatformHandler
from src.handlers.nekopost import NekopostHandler
from src.agent.toc.discovery import capture_observed_api_recipe


def test_observed_get_recipe_keeps_bindings_and_strips_credentials():
    recipe = capture_api_recipe(
        method="GET",
        url="https://reader.example/api/chapters?book=novel-7&access_token=secret",
        headers={"Authorization": "Bearer secret", "Accept": "application/json"},
        response_body={"items": [{"title": "Chapter 1", "content": "A story."}]},
        page_url="https://reader.example/book/novel-7",
        mapping={"title": "items.0.title", "content": "items.0.content"},
    )

    assert recipe is not None
    saved = json.dumps(recipe.model_dump())
    assert "secret" not in saved
    assert recipe.method == "GET"
    assert recipe.response_mapping["content"] == "items.0.content"
    assert "book=novel-7" in recipe.endpoint


def test_observed_get_cursor_values_remain_distinct_recipe_variants(tmp_path):
    memory = DomainMemoryManager(recipes_dir=tmp_path)
    for cursor in ("first", "second"):
        recipe = capture_api_recipe(method="GET", url=f"https://reader.example/api/chapters?cursor={cursor}",
            headers={}, response_body={"items": []}, page_url="https://reader.example/book/7",
            mapping={"chapters": "items"})
        assert memory.promote_api_recipe("reader.example", recipe, toc_complete=True,
                                         novel_identity_verified=True, quality_score=0.9)
    loaded = DomainMemoryManager(recipes_dir=tmp_path).get_recipe("reader.example")
    assert {item.endpoint for item in loaded.api_recipes} == {
        "https://reader.example/api/chapters?cursor=first",
        "https://reader.example/api/chapters?cursor=second",
    }


@pytest.mark.parametrize("query", [
    "query Chapters { chapters { title content } }",
    "{ chapters { title } }",
])
def test_single_graphql_queries_are_replayable(query):
    assert graphql_query_is_replayable(query)


@pytest.mark.parametrize("query", [
    "mutation { updateChapter(id: 1) { id } }",
    "subscription { chapterAdded { id } }",
    "query A { chapters { id } } query B { chapters { id } }",
    "query A { chapters { ...ChapterFields } } fragment ChapterFields on Chapter { id }",
    "{ chapters } # comment\n mutation { deleteChapter }",
    "{ chapters } query B { chapters }",
    "chapters { id }",
])
def test_unsafe_or_ambiguous_graphql_is_rejected(query):
    assert not graphql_query_is_replayable(query)


def test_multiple_shorthand_operation_roots_are_rejected():
    assert not graphql_query_is_replayable("{ chapters { id } } { authors { id } }")


def test_graphql_success_errors_are_rejected_without_data_and_partial_data_is_marked():
    recipe = capture_api_recipe(method="POST", url="https://reader.example/graphql", headers={},
        request_body={"query": "query { chapter { title content } }"},
        response_body={"data": {"chapter": {"title": "T", "content": "Body"}}},
        page_url="https://reader.example/book/7",
        mapping={"title": "data.chapter.title", "content": "data.chapter.content"},
        representation="text", trusted_read_only=True)
    with pytest.raises(ValueError):
        replay_api_chapter(recipe, {"errors": [{"message": "denied"}]})
    partial = replay_api_chapter(recipe, {"data": {"chapter": {"title": "T", "content": "Body"}},
                                         "errors": [{"message": "one field failed"}]})
    assert partial["partial"] is True


def test_post_requires_explicit_read_only_declaration():
    kwargs = dict(
        method="POST", url="https://reader.example/graphql", headers={"Content-Type": "application/json"},
        response_body={"data": {"chapters": []}}, page_url="https://reader.example/book/7",
        request_body={"query": "query { chapters { title } }"},
        mapping={"chapters": "data.chapters"},
    )
    assert capture_api_recipe(**kwargs) is None
    assert capture_api_recipe(**kwargs, trusted_read_only=True) is not None
    kwargs["request_body"] = {"query": "mutation { deleteChapter }"}
    assert capture_api_recipe(**kwargs, trusted_read_only=True) is None


def test_platform_handlers_must_explicitly_declare_read_only_post_routes():
    assert BasePlatformHandler.is_read_only_api_request(None, "POST", "https://x.example/write", {}, {}) is False
    handler = NekopostHandler()
    assert handler.is_read_only_api_request("POST", "https://www.nekopost.net/api/project/detail2", {}, {"pid": 3}) is True
    assert handler.is_read_only_api_request("POST", "https://www.nekopost.net/handler/cinfo", {}, {"p": 3, "c": 4}) is True
    assert handler.is_read_only_api_request("POST", "https://www.nekopost.net/api/project/detail2", {}, {"pid": 3, "delete": True}) is False


def test_discovery_capture_uses_the_trusted_handler_declaration():
    recipe = capture_observed_api_recipe(
        method="POST", url="https://www.nekopost.net/api/project/detail2",
        headers={"content-type": "application/json"}, request_body={"pid": 7},
        response_body={"projectInfo": {"ListChapter": []}},
        page_url="https://www.nekopost.net/novel/7", handler=NekopostHandler(),
        mapping={"chapters": "projectInfo.ListChapter"},
    )
    assert recipe is not None
    assert recipe.trusted_read_only is True
    assert capture_observed_api_recipe(
        method="POST", url="https://www.nekopost.net/handler/cinfo",
        headers={}, request_body={"p": 7, "c": 1}, response_body="ciphertext",
        page_url="https://www.nekopost.net/novel/7", mapping={"content": "novelContent"},
    ) is None


def test_response_rendering_preserves_order_and_html_semantics():
    paragraphs = render_api_chapter(["First <word>", "Second & last."], representation="paragraphs")
    assert paragraphs == "First &lt;word&gt;\n\nSecond &amp; last."
    assert render_api_chapter("<p>One &amp; two.</p><p>Three.</p>", representation="html") == "One & two.\n\nThree."
    assert render_api_chapter("<div><p>Nested paragraph.</p></div>", representation="html") == "Nested paragraph."


def test_invalid_html_and_unknown_representation_are_not_replayed():
    with pytest.raises(ValueError):
        render_api_chapter("<script>steal()</script>", representation="html")
    with pytest.raises(ValueError):
        render_api_chapter("text", representation="unknown")


def test_recipe_schema_rejects_secret_bearing_request_values():
    with pytest.raises(ValueError):
        ApiRecipe(method="GET", endpoint="https://reader.example/api?token=secret", page_evidence="/book/7")
    with pytest.raises(ValueError):
        ApiRecipe(method="GET", endpoint="https://reader.example/api", page_evidence="https://reader.example/book?session=secret")


def test_api_recipe_is_persisted_only_after_quality_and_identity_approval(tmp_path):
    recipe = capture_api_recipe(
        method="GET", url="https://reader.example/api/book/7/chapters", headers={},
        response_body={"title": "Chapter 1", "content": "Story"},
        page_url="https://reader.example/book/7",
        mapping={"title": "title", "content": "content"}, representation="text",
    )
    memory = DomainMemoryManager(recipes_dir=tmp_path)
    assert memory.promote_api_recipe("https://reader.example/book/7", recipe, toc_complete=False,
        novel_identity_verified=True, quality_score=0.99) is False
    assert memory.promote_api_recipe("https://reader.example/book/7", recipe, toc_complete=True,
        novel_identity_verified=False, quality_score=0.99) is False
    assert memory.promote_api_recipe("https://reader.example/book/7", recipe, toc_complete=True,
        novel_identity_verified=True, quality_score=0.70) is False
    assert memory.promote_api_recipe("https://reader.example/book/7", recipe, toc_complete=True,
        novel_identity_verified=True, quality_score=0.95) is True
    loaded = DomainMemoryManager(recipes_dir=tmp_path).get_recipe("reader.example")
    assert loaded.api_recipes[0].endpoint == recipe.endpoint
