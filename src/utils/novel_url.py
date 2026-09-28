"""Use domain recipes as URL shapes without navigating to another novel's samples."""

from typing import Literal, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


PageKind = Literal["TOC", "CHAPTER"]
_NAVIGATION_PARAMS = {"chapter", "episode", "part", "page", "p", "offset", "limit", "sort", "order"}


def _segments(url: str) -> tuple[str, ...]:
    return tuple(part for part in urlparse(url).path.split("/") if part)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _shared_path_length(toc_url: str, chapter_url: str) -> int:
    toc_parts, chapter_parts = _segments(toc_url), _segments(chapter_url)
    return next(
        (index for index, (toc, chapter) in enumerate(zip(toc_parts, chapter_parts)) if toc != chapter),
        min(len(toc_parts), len(chapter_parts)),
    )


def _identity_params(toc_url: str, chapter_url: str) -> set[str]:
    toc_params = parse_qs(urlparse(toc_url).query)
    chapter_params = parse_qs(urlparse(chapter_url).query)
    return {
        key for key, value in toc_params.items()
        if key.lower() not in _NAVIGATION_PARAMS and chapter_params.get(key) == value
    }


def _same_site(*urls: str) -> bool:
    hosts = [_host(url) for url in urls]
    return bool(hosts[0]) and all(host == hosts[0] for host in hosts)


def recipe_page_kind(url: str, sample_toc_url: str, sample_chapter_url: str) -> Optional[PageKind]:
    """Infer page type only when its path/query shape matches a saved recipe."""
    if not sample_toc_url or not sample_chapter_url or not _same_site(url, sample_toc_url, sample_chapter_url):
        return None

    current = _segments(url)
    toc = _segments(sample_toc_url)
    chapter = _segments(sample_chapter_url)
    common = _shared_path_length(sample_toc_url, sample_chapter_url)
    current_params = parse_qs(urlparse(url).query)
    toc_params = parse_qs(urlparse(sample_toc_url).query)
    chapter_params = parse_qs(urlparse(sample_chapter_url).query)
    chapter_only_keys = chapter_params.keys() - toc_params.keys()
    identity_keys = _identity_params(sample_toc_url, sample_chapter_url)

    if identity_keys and not identity_keys.issubset(current_params):
        return None
    if len(current) == len(toc) and current[common:] == toc[common:]:
        if not chapter_only_keys.intersection(current_params):
            return "TOC"
    if len(current) != len(chapter) or (toc == chapter and not chapter_only_keys):
        return None
    if common == 0 and not identity_keys:
        return None
    if current[common:-1] != chapter[common:-1]:
        return None
    if chapter == toc and not chapter_only_keys.issubset(current_params):
        return None
    # A terminal filename is a structural route, not a chapter identifier.
    if chapter and "." in chapter[-1] and current[-1] != chapter[-1]:
        return None
    return "CHAPTER"


def toc_url_from_recipe(url: str, sample_toc_url: str, sample_chapter_url: str) -> Optional[str]:
    """Derive a TOC from the current chapter, never from a previous novel's ID."""
    if recipe_page_kind(url, sample_toc_url, sample_chapter_url) != "CHAPTER":
        return None

    toc = _segments(sample_toc_url)
    common = _shared_path_length(sample_toc_url, sample_chapter_url)
    identity_keys = _identity_params(sample_toc_url, sample_chapter_url)
    if toc[common:] and common < 2 and not identity_keys:
        return None

    sample_params = parse_qs(urlparse(sample_toc_url).query)
    if sample_params.keys() - identity_keys:
        return None
    current_params = parse_qs(urlparse(url).query)
    if any(key not in current_params for key in identity_keys):
        return None

    current = urlparse(url)
    path = "/" + "/".join((*_segments(url)[:common], *toc[common:]))
    if urlparse(sample_toc_url).path.endswith("/") and not path.endswith("/"):
        path += "/"
    query = urlencode({key: current_params[key] for key in sample_params}, doseq=True)
    return urlunparse(current._replace(path=path, params="", query=query, fragment=""))


def belongs_to_recipe_novel(
    page_url: str,
    chapter_url: str,
    sample_toc_url: str,
    sample_chapter_url: str,
) -> Optional[bool]:
    """Return None when the saved examples cannot establish a novel boundary."""
    if not _same_site(page_url, chapter_url):
        return False
    if not sample_toc_url or not sample_chapter_url or not _same_site(page_url, sample_toc_url, sample_chapter_url):
        return None

    toc = _segments(sample_toc_url)
    common = _shared_path_length(sample_toc_url, sample_chapter_url)
    current = _segments(page_url)
    path_scope = current[:common] if common >= 2 or (common == 1 and len(toc) == 1) else ()
    identity_keys = _identity_params(sample_toc_url, sample_chapter_url)
    current_params = parse_qs(urlparse(page_url).query)
    candidate_params = parse_qs(urlparse(chapter_url).query)
    query_scope = {key for key in identity_keys if key in current_params}

    if not path_scope and not query_scope:
        return None
    if path_scope and _segments(chapter_url)[:len(path_scope)] != path_scope:
        return False
    for key in query_scope:
        if key in candidate_params and candidate_params[key] != current_params[key]:
            return False
        if not path_scope and key not in candidate_params:
            return False
    return True
