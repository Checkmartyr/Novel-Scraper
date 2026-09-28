"""Recipe examples describe URL shapes, never the current novel's identity."""

import pytest

from src.utils.novel_url import belongs_to_recipe_novel, recipe_page_kind, toc_url_from_recipe


@pytest.mark.parametrize(
    "sample_toc,sample_chapter,current_toc,current_chapter,foreign_chapter",
    [
        (
            "https://reader.example/works/old",
            "https://reader.example/works/old/episodes/1",
            "https://reader.example/works/new",
            "https://reader.example/works/new/episodes/2",
            "https://reader.example/works/old/episodes/1",
        ),
        (
            "https://reader.example/book/old/catalog",
            "https://reader.example/book/old/first-episode",
            "https://reader.example/book/new/catalog",
            "https://reader.example/book/new/second-episode",
            "https://reader.example/book/old/first-episode",
        ),
        (
            "https://reader.example/view.php?id=old",
            "https://reader.example/viewlongc.php?id=old&chapter=1",
            "https://reader.example/view.php?id=new",
            "https://reader.example/viewlongc.php?id=new&chapter=2",
            "https://reader.example/viewlongc.php?id=old&chapter=1",
        ),
        (
            "https://reader.example/nold/",
            "https://reader.example/nold/1/",
            "https://reader.example/nnew/",
            "https://reader.example/nnew/2/",
            "https://reader.example/nold/1/",
        ),
    ],
)
def test_recipe_scopes_novel_not_saved_example(
    sample_toc, sample_chapter, current_toc, current_chapter, foreign_chapter
):
    assert recipe_page_kind(current_toc, sample_toc, sample_chapter) == "TOC"
    assert recipe_page_kind(current_chapter, sample_toc, sample_chapter) == "CHAPTER"
    assert toc_url_from_recipe(current_chapter, sample_toc, sample_chapter) == current_toc
    assert belongs_to_recipe_novel(current_toc, current_chapter, sample_toc, sample_chapter) is True
    assert belongs_to_recipe_novel(current_toc, foreign_chapter, sample_toc, sample_chapter) is False


def test_ambiguous_recipe_cannot_supply_an_old_toc():
    toc = "https://reader.example/toc"
    chapter = "https://reader.example/c1"
    assert toc_url_from_recipe("https://reader.example/c2", toc, chapter) is None
    assert belongs_to_recipe_novel(toc, "https://reader.example/c2", toc, chapter) is None
    assert toc_url_from_recipe("https://reader.example/other", "", "") is None
