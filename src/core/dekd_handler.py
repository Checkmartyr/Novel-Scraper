"""Backward compatibility shim for src.handlers.dekd."""

from src.handlers.dekd import (
    DekDHandler,
    is_dekd_url,
    parse_dekd_url,
    fetch_dekd_toc_data,
    build_dekd_toc_html,
    handle_dekd_url,
    DEKD_DOMAIN_REGEX,
    _dekd_cache,
)

__all__ = [
    "DekDHandler",
    "is_dekd_url",
    "parse_dekd_url",
    "fetch_dekd_toc_data",
    "build_dekd_toc_html",
    "handle_dekd_url",
    "DEKD_DOMAIN_REGEX",
    "_dekd_cache",
]
