"""Job-local discovery evidence and deadlines, independent of saved recipes."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import re
import math
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from src.scraper.runtime import canonical_url


_scope = ContextVar("novel_recovery_scope", default=None)
_operation_deadline = ContextVar("novel_operation_deadline", default=float("inf"))


def operation_deadline():
    return min(_operation_deadline.get(), current_scope().deadline if current_scope() else float("inf"))


@contextmanager
def operation_scope(deadline):
    token = _operation_deadline.set(min(deadline, operation_deadline()))
    try:
        yield
    finally:
        _operation_deadline.reset(token)
CATALOG_NOISE = "nav:not([role=doc-toc]), aside, footer, form, #comments, .comments, .comment-list, .related, .recommendations, #actionbar, .sharedaddy"


def catalog_soup(html):
    soup = BeautifulSoup(html, "lxml")
    for element in soup.select(CATALOG_NOISE):
        if element.parent is not None:
            element.decompose()
    return soup


@dataclass(frozen=True)
class FetchRecord:
    requested_url: str
    final_url: str
    status: int
    headers: dict
    redirects: tuple = ()
    transport: str = "http"
    error: str = ""


def wordpress_resource(html, url, headers=None):
    soup = BeautifulSoup(html, "lxml")
    resource = soup.select_one('link[rel=alternate][type="application/json"][href]')
    if resource:
        target = urljoin(url, resource["href"])
        return target if host(target) == host(url) else None
    link_header = (headers or {}).get("link", "")
    for value in link_header.split(","):
        match = re.search(r"<([^>]+)>", value)
        if match and 'application/json' in value and 'alternate' in value:
            target = urljoin(url, match[1])
            if host(target) == host(url):
                return target
    return None


def host(url):
    return (urlsplit(url).hostname or "").casefold().removeprefix("www.")


def catalog_links(html, url):
    """Collect witnessed chapter labels, excluding navigation and comments."""
    soup = catalog_soup(html)
    roots = soup.select(".chapter-list, .entry-content, [itemprop=articleBody], article, main") or [soup]
    links = {}
    for anchor in (anchor for root in roots for anchor in root.select("a[href]")):
        label = anchor.get_text(" ", strip=True)
        if re.search(r"(?:chapter|episode|prologue|epilogue|第|ตอน)\s*[\d:：]|^(?:prologue|epilogue)\b", label, re.I):
            target = canonical_url(urljoin(url, anchor["href"]))
            if urlsplit(target).scheme in {"http", "https"}:
                links.setdefault(target, label)
    return links


def _chapter_matches(label, html):
    soup = BeautifulSoup(html, "lxml")
    headings = [element.get_text(" ", strip=True) for element in soup.select("h1, h2, .reader-header-chapter, .chapter-title")]
    normalize = lambda text: re.sub(r"\W+", "", text).casefold()
    expected = normalize(label)
    return any(normalize(heading) == expected or
               (len(expected) > 12 and expected in normalize(heading)) for heading in headings)


@dataclass
class NovelScope:
    root: str
    catalogs: set = field(default_factory=set)
    approved_links: set = field(default_factory=set)
    unresolved: dict = field(default_factory=dict)
    relationships: list = field(default_factory=list)
    observed: set = field(default_factory=set)
    observed_links: dict = field(default_factory=dict)
    deadline: float = float("inf")

    def __post_init__(self):
        self.catalogs.add(canonical_url(self.root))

    def allows(self, source, candidate):
        return (canonical_url(source) in self.catalogs | self.approved_links and
                canonical_url(candidate) in self.catalogs | self.approved_links)

    def note_redirect(self, requested, final, redirects):
        requested, final = canonical_url(requested), canonical_url(final)
        if requested == final:
            return
        if requested in self.catalogs:
            self.catalogs.add(final)
        elif requested in self.approved_links:
            self.approved_links.add(final)
        else:
            return
        proof = {"kind": "redirect", "requested_url": requested, "final_url": final,
                 "redirects": list(redirects)}
        if proof not in self.relationships:
            self.relationships.append(proof)

    async def observe_catalog(self, url, html, fetcher):
        from src.scraper.runtime import belongs_to_observed_novel
        import hashlib
        marker = (canonical_url(url), hashlib.sha256(html.encode()).hexdigest())
        if marker in self.observed:
            return
        self.observed.add(marker)
        self.catalogs.add(canonical_url(url))
        links = catalog_links(html, url)
        self.observed_links.update(links)
        foreign = {}
        for target, label in links.items():
            if host(target) == host(url):
                if belongs_to_observed_novel(url, target):
                    self.approved_links.add(target)
            elif target not in self.approved_links:
                foreign.setdefault(host(target), []).append((target, label))
                self.unresolved[target] = "Unverified cross-domain chapter link"
        for entries in list(foreign.values())[:4]:
            # Verify first, middle and last witnesses; admit exact listed URLs only.
            samples = list(dict.fromkeys([0, len(entries) // 2, len(entries) - 1]))
            proofs = []
            for position in samples:
                target, label = entries[position]
                try:
                    page = await before_deadline(fetcher.fetch_html(target), self.deadline)
                    soup = BeautifulSoup(page, "lxml")
                    backlinks = [canonical_url(urljoin(target, a["href"])) for a in soup.select("a[href]")
                                 if re.search(r"contents|目次|目录|สารบัญ|\btoc\b|\bindex\b", a.get_text(" ", strip=True), re.I)]
                    matching = [link for link in backlinks if link in self.catalogs or
                                (host(link) == host(target) and urlsplit(link).path == urlsplit(url).path)]
                    if not matching or not _chapter_matches(label, page):
                        raise ValueError("Chapter heading or reciprocal catalog identity did not match")
                    proofs.append({"url": target, "catalog_backlink": matching[0]})
                except (Exception, asyncio.TimeoutError) as error:
                    for candidate, _ in entries:
                        self.unresolved[candidate] = str(error)
                    break
            else:
                self.approved_links.update(target for target, _ in entries)
                self.catalogs.update(proof["catalog_backlink"] for proof in proofs)
                for target, _ in entries:
                    self.unresolved.pop(target, None)
                self.relationships.append({"kind": "catalog_backlinks", "catalog": url,
                                           "links": [target for target, _ in entries], "samples": proofs})

    def export(self):
        return {"catalogs": sorted(self.catalogs), "approved_links": sorted(self.approved_links),
                "relationships": self.relationships, "unresolved": dict(self.unresolved),
                "observed_links": dict(self.observed_links)}


def current_scope():
    return _scope.get()


@contextmanager
def recovery_scope(url, fetcher=None):
    existing = current_scope()
    scope = existing or NovelScope(url)
    if fetcher is not None:
        scope.deadline = min(scope.deadline, getattr(fetcher, "discovery_deadline", float("inf")))
        fetcher.novel_scope = scope
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


async def before_deadline(awaitable, deadline):
    """Bound cancellation as well as work; unlike wait_for, never await it forever."""
    task = asyncio.ensure_future(awaitable)
    remaining = max(0, deadline - time.monotonic())
    try:
        done, _ = await asyncio.wait({task}, timeout=remaining if math.isfinite(remaining) else None)
    except BaseException:
        task.cancel()
        task.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        raise
    if task in done:
        return task.result()
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=0.1)
    task.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
    raise asyncio.TimeoutError("Discovery deadline exhausted")
