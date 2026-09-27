"""Backward compatibility shim for src.handlers.nekopost."""

from src.handlers.nekopost import (
    NekopostHandler,
    is_nekopost_url,
    parse_nekopost_url,
    evp_bytes_to_key,
    decrypt_cryptojs_aes,
    fetch_nekopost_toc_data,
    fetch_nekopost_chapter_content,
    build_nekopost_toc_html,
    build_nekopost_chapter_html,
    handle_nekopost_url,
    NEKOPOST_KEY,
    NEKOPOST_URL_REGEX,
    _project_cache,
)

__all__ = [
    "NekopostHandler",
    "is_nekopost_url",
    "parse_nekopost_url",
    "evp_bytes_to_key",
    "decrypt_cryptojs_aes",
    "fetch_nekopost_toc_data",
    "fetch_nekopost_chapter_content",
    "build_nekopost_toc_html",
    "build_nekopost_chapter_html",
    "handle_nekopost_url",
    "NEKOPOST_KEY",
    "NEKOPOST_URL_REGEX",
    "_project_cache",
]
