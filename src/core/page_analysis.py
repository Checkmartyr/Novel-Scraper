"""Reusable, in-memory analysis for one immutable HTML response snapshot.

Parsed trees are intentionally scoped to this object/cache and are never
serialized. Callers that need to remove nodes must work on ``copy_soup()``.
"""
from collections import OrderedDict
import inspect
from typing import Optional

from bs4 import BeautifulSoup


def call_with_page_analysis(function, *args, page_analysis, **kwargs):
    """Pass the internal snapshot argument when a callable supports it."""
    try:
        parameters = inspect.signature(function).parameters.values()
        accepts = any(parameter.name == "page_analysis" or parameter.kind == inspect.Parameter.VAR_KEYWORD
                      for parameter in parameters)
    except (TypeError, ValueError):
        accepts = False
    if accepts:
        kwargs["page_analysis"] = page_analysis
    return function(*args, **kwargs)


class PageAnalysis:
    """Memoize common DOM queries while preserving an untouched source tree."""

    def __init__(self, html: str):
        self.html = html
        self.metrics = {"tree_builds": 1, "selector_queries": 0, "selector_cache_hits": 0}
        self.soup = BeautifulSoup(html, "lxml")
        self._select_cache = {}
        self._select_one_cache = {}
        self._subtree_select_cache = {}
        self._text_cache = {}
        self._derived_cache = {}

    def select(self, selector: str):
        if selector not in self._select_cache:
            self.metrics["selector_queries"] += 1
            self._select_cache[selector] = tuple(self.soup.select(selector))
        else:
            self.metrics["selector_cache_hits"] += 1
        return self._select_cache[selector]

    def select_one(self, selector: str):
        if selector not in self._select_one_cache:
            self.metrics["selector_queries"] += 1
            self._select_one_cache[selector] = self.soup.select_one(selector)
        else:
            self.metrics["selector_cache_hits"] += 1
        return self._select_one_cache[selector]

    def select_from(self, root, selector: str):
        key = (id(root), selector)
        if key not in self._subtree_select_cache:
            self.metrics["selector_queries"] += 1
            self._subtree_select_cache[key] = tuple(root.select(selector))
        else:
            self.metrics["selector_cache_hits"] += 1
        return self._subtree_select_cache[key]

    def select_one_from(self, root, selector: str):
        matches = self.select_from(root, selector)
        return matches[0] if matches else None

    def text(self, selector: Optional[str] = None, separator: str = " ", strip: bool = True):
        key = (selector, separator, strip)
        if key not in self._text_cache:
            node = self.soup if selector is None else self.select_one(selector)
            self._text_cache[key] = node.get_text(separator, strip=strip) if node else ""
        return self._text_cache[key]

    def copy_soup(self):
        """Return an isolated working tree for destructive cleaning operations."""
        return BeautifulSoup(str(self.soup), "lxml")

    def derive(self, key, factory):
        """Memoize immutable analysis products for this one response snapshot."""
        if key not in self._derived_cache:
            self._derived_cache[key] = factory()
        return self._derived_cache[key]


class PageAnalysisCache:
    """Small LRU working set of response snapshots; never shared globally."""

    def __init__(self, max_snapshots: int = 12):
        if max_snapshots < 1:
            raise ValueError("max_snapshots must be positive")
        self.max_snapshots = min(int(max_snapshots), 12)
        self._items = OrderedDict()
        self.metrics = {"hits": 0, "misses": 0}

    def get(self, key: str, html: str) -> PageAnalysis:
        analysis = self._items.pop(key, None)
        if analysis is None or analysis.html != html:
            self.metrics["misses"] += 1
            analysis = PageAnalysis(html)
        else:
            self.metrics["hits"] += 1
        self._items[key] = analysis
        while len(self._items) > self.max_snapshots:
            self._items.popitem(last=False)
        return analysis

    def __len__(self):
        return len(self._items)
