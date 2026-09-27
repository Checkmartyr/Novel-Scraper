"""Platform-specific handlers and registry for dedicated web novel sites."""

from src.handlers.base import BasePlatformHandler
from src.handlers.registry import HandlerRegistry, registry
from src.handlers.dekd import (
    DekDHandler,
    is_dekd_url,
    parse_dekd_url,
    fetch_dekd_toc_data,
    build_dekd_toc_html,
    handle_dekd_url,
)
from src.handlers.nekopost import (
    NekopostHandler,
    is_nekopost_url,
    parse_nekopost_url,
    fetch_nekopost_toc_data,
    fetch_nekopost_chapter_content,
    build_nekopost_toc_html,
    build_nekopost_chapter_html,
    handle_nekopost_url,
)
from src.handlers.webnovel import (
    WebNovelHandler,
    is_webnovel_url,
    parse_webnovel_url,
    fetch_webnovel_toc_data,
    build_webnovel_toc_html,
    handle_webnovel_url,
)

# Register default platform handlers in the global registry
registry.register(DekDHandler())
registry.register(NekopostHandler())
registry.register(WebNovelHandler())

__all__ = [
    "BasePlatformHandler",
    "HandlerRegistry",
    "registry",
    # Dek-D
    "DekDHandler",
    "is_dekd_url",
    "parse_dekd_url",
    "fetch_dekd_toc_data",
    "build_dekd_toc_html",
    "handle_dekd_url",
    # Nekopost
    "NekopostHandler",
    "is_nekopost_url",
    "parse_nekopost_url",
    "fetch_nekopost_toc_data",
    "fetch_nekopost_chapter_content",
    "build_nekopost_toc_html",
    "build_nekopost_chapter_html",
    "handle_nekopost_url",
    # WebNovel
    "WebNovelHandler",
    "is_webnovel_url",
    "parse_webnovel_url",
    "fetch_webnovel_toc_data",
    "build_webnovel_toc_html",
    "handle_webnovel_url",
]
