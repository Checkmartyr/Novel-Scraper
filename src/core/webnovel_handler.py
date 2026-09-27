"""Backward compatibility shim for src.handlers.webnovel."""

from src.handlers.webnovel import (
    WebNovelHandler,
    is_webnovel_url,
    parse_webnovel_url,
    fetch_webnovel_toc_data,
    build_webnovel_toc_html,
    handle_webnovel_url,
    WEBNOVEL_DOMAIN_REGEX,
    WEBNOVEL_HEADERS,
    _webnovel_cache,
)

__all__ = [
    "WebNovelHandler",
    "is_webnovel_url",
    "parse_webnovel_url",
    "fetch_webnovel_toc_data",
    "build_webnovel_toc_html",
    "handle_webnovel_url",
    "WEBNOVEL_DOMAIN_REGEX",
    "WEBNOVEL_HEADERS",
    "_webnovel_cache",
]
