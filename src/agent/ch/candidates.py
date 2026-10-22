"""Rank witnessed story regions and collect evidence before model removals."""
from collections import Counter
from dataclasses import dataclass
import math
import re
import copy
import json
from functools import lru_cache

from bs4 import BeautifulSoup
import soupsieve
from src.core.page_analysis import PageAnalysis


AD_CONTAINER_SELECTORS = ".ad-banner, .adsbygoogle, [data-ad-slot], .adsterra-banner-container"
NOISE = "script, style, noscript, nav, header, footer, aside, form, button, #comments, .comments, .comment-list, .cheer-widget, .nav-bottom, .site-nav, .sharedaddy, .c-announce, .l-foot-contents, .entry-meta, .jp-relatedposts, .post-views, " + AD_CONTAINER_SELECTORS
STORY = re.compile(r"chapter.?content|story|reader.?content|novel.?body|novel_honbun|entry.?content|episode.?body|p-novel__body|cha-words", re.I)


def selector_for(element, soup):
    if element.get("id"):
        selector = f'{element.name}#{soupsieve.escape(element["id"])}'
        if len(soup.select(selector)) == 1:
            return selector
    classes = element.get("class", [])
    if classes:
        selector = element.name + "".join("." + soupsieve.escape(value) for value in classes[:3])
        if len(soup.select(selector)) == 1:
            return selector
    parts = []
    while element and element.name != "[document]":
        index = 1 + sum(getattr(sibling, "name", None) == element.name for sibling in element.previous_siblings)
        parts.append(f"{element.name}:nth-of-type({index})")
        element = element.parent
    return " > ".join(reversed(parts))


def title_selector(soup):
    preferred = soup.select_one(".reader-header-chapter, .chapter-title, .episode-title, .widget-episodeTitle, .p-novel__title")
    headings = soup.select("h1, h2, h3")
    if preferred is None:
        preferred = next((element for element in headings if re.search(
            r"chapter|episode|prologue|epilogue|第|ตอน", element.get_text(" ", strip=True), re.I)), None)
    if preferred is None:
        preferred = next(iter(headings), soup.title)
    return selector_for(preferred, soup) if preferred is not None else "h1"


def _text_excluding(element, excluded_ids, separator=" "):
    """Read prose without changing the witnessed source tree."""
    chunks = []
    def visit(node):
        if id(node) in excluded_ids:
            return
        for child in getattr(node, "children", ()):
            if getattr(child, "name", None) is None:
                value = str(child).strip()
                if value:
                    chunks.append(value)
            elif child.name == "br":
                chunks.append(" ")
            else:
                visit(child)
    visit(element)
    return separator.join(chunks)


def _paragraphs_excluding(element, excluded_ids):
    """Extract paragraph-like blocks from the original tree, excluding noise."""
    from src.agent.ch.code_generator import extract_paragraphs
    # Build a small isolated copy only when excluded descendants exist. This
    # keeps the source evidence independent while avoiding serializing every
    # candidate subtree during ranking.
    if not any(id(node) in excluded_ids for node in element.descendants if getattr(node, "name", None)):
        return extract_paragraphs(element)
    clone = BeautifulSoup(str(element), "lxml")
    copied = clone.find(element.name)
    for node in list(copied.select(NOISE)) if copied else []:
        node.decompose()
    return extract_paragraphs(copied) if copied else []


def region_signature(element, *, page_analysis=None):
    text = element.get_text(" ", strip=True)
    select = page_analysis.select_from if page_analysis is not None else lambda root, selector: root.select(selector)
    links = sum(len(link.get_text(" ", strip=True)) for link in select(element, "a"))
    lengths = sorted(len(p.get_text(" ", strip=True)) for p in select(element, "p"))
    heading = element.find_previous(["h1", "h2", "h3"])
    semantic = lambda node: {key: str(node[key])[:100] for key in ("role", "itemprop", "aria-label") if node.get(key)}
    return {"tag": element.name, "classes": list(element.get("class", [])),
            "ancestors": [parent.name for parent in list(element.parents)[:3]],
            "ancestor_context": [{"tag": parent.name, "classes": parent.get("class", []),
                                  "semantic": semantic(parent)} for parent in list(element.parents)[:3]],
            "semantic": semantic(element),
            "neighbor_heading": {"tag": heading.name, "template": re.sub(r"\d+", "#", heading.get_text(" ", strip=True))[:100]} if heading else {},
            "paragraph_distribution": [lengths[0], lengths[len(lengths)//2], lengths[-1]] if lengths else [],
            "paragraphs": len(lengths), "link_density": round(links / max(1, len(text)), 3)}


def structural_signature(html, selector):
    try:
        element = BeautifulSoup(html, "lxml").select_one(selector)
        return region_signature(element) if element is not None else {}
    except Exception:
        return {}


@dataclass(frozen=True)
class ContentCandidate:
    selector: str
    title_selector: str
    score: float
    text: str
    signature: dict


def rank_candidates(html, limit=6, previous_signature=None, *, page_analysis=None):
    # Repeated plans audit identical source; retain only a small number of views.
    if page_analysis is not None:
        signature_json = json.dumps(previous_signature, sort_keys=True)
        ranked = page_analysis.derive(
            ("chapter_candidates", limit, signature_json),
            lambda: _rank_candidates_from_analysis(page_analysis, limit, signature_json))
        return copy.deepcopy(list(ranked))
    return copy.deepcopy(list(_rank_candidates(html, limit, json.dumps(previous_signature, sort_keys=True))))


@lru_cache(maxsize=12)
def _rank_candidates(html, limit, signature_json):
    return _rank_candidates_from_analysis(PageAnalysis(html), limit, signature_json)


def _rank_candidates_from_analysis(page_analysis, limit, signature_json):
    previous_signature = json.loads(signature_json)
    soup = page_analysis.soup
    title = title_selector(soup)
    noise_ids = {id(element) for element in soup.select(NOISE)}
    ranked = []
    for element in page_analysis.select("article, main, section, div, body"):
        if id(element) in noise_ids or any(id(parent) in noise_ids for parent in element.parents):
            continue
        local_noise = {id(node) for node in page_analysis.select_from(element, NOISE)}
        excluded = noise_ids | local_noise
        text = _text_excluding(element, excluded).strip()
        if not text or page_analysis.select_one_from(element, ".skeleton, [aria-busy=true]"):
            continue
        paragraphs = [node for node in page_analysis.select_from(element, "p") if id(node) not in excluded]
        signature = region_signature(element, page_analysis=page_analysis)
        prose = sum(len(p.get_text(" ", strip=True)) for p in paragraphs)
        metadata = " ".join([element.get("id", ""), *element.get("class", [])])
        score = math.log1p(len(text)) + min(len(paragraphs), 30) * 0.35
        score += 5 if STORY.search(metadata) else 0
        score += 1 if element.name in {"article", "main"} else 0
        score += min(1, prose / max(1, len(text)))
        score += 0.5 if element.find_previous(["h1", "h2", "h3"]) else 0
        score -= 12 * signature["link_density"]
        score -= 4 if element.name == "body" or element.get("id") in {"root", "app", "__next"} else 0
        # Prefer complete story wrappers to nested subsections with identical prose.
        if any(STORY.search(" ".join([parent.get("id", ""), *parent.get("class", [])]))
               for parent in element.parents if getattr(parent, "attrs", None)):
            score -= 2
        if previous_signature:
            score += 1.5 if signature["tag"] == previous_signature.get("tag") else 0
            score += len(set(signature["classes"]) & set(previous_signature.get("classes", [])))
            score += 1 if signature["ancestors"] == previous_signature.get("ancestors") else 0
            score += 1 if signature["semantic"] and signature["semantic"] == previous_signature.get("semantic") else 0
            score += 1 if signature["neighbor_heading"] and signature["neighbor_heading"] == previous_signature.get("neighbor_heading") else 0
            score += 1 if signature["ancestor_context"] == previous_signature.get("ancestor_context") else 0
        ranked.append(ContentCandidate(selector_for(element, soup), title, score, text, signature))
    ranked.sort(key=lambda candidate: candidate.score, reverse=True)
    return tuple(ranked[:limit])


def independent_evidence(html, extracted_text, *, page_analysis=None):
    """Use ranked source regions, never the plan's removal selectors, as evidence."""
    from src.agent.ch.code_generator import extract_paragraphs
    page_analysis = page_analysis or PageAnalysis(html)
    candidates = rank_candidates(html, limit=1, page_analysis=page_analysis)
    if not candidates:
        return "", [], ["Source story is empty, inaccessible, or still loading"]
    soup = page_analysis.soup
    region = page_analysis.select_one(candidates[0].selector)
    # Inspect adjacent prose in the nearest article/story wrapper before any
    # proposed removal rules. Never cross another article or a chapter heading.
    for parent in list(region.parents)[:3]:
        if parent.name not in {"article", "main", "section", "div"} or parent.get("id") in {"root", "app", "__next"}:
            break
        if page_analysis.select_one_from(parent, "article") and region.name != "article":
            break
        excluded = {id(node) for node in page_analysis.select_from(parent, NOISE)}
        content = _text_excluding(parent, excluded).strip()
        linked = sum(len(anchor.get_text(" ", strip=True)) for anchor in page_analysis.select_from(parent, "a") if id(anchor) not in excluded)
        headings = [heading for heading in page_analysis.select_from(parent, "h1, h2") if id(heading) not in excluded and re.search(r"chapter|episode|prologue|epilogue|第|ตอน", heading.get_text(), re.I)]
        if len(headings) > 1 or linked / max(1, len(content)) > 0.2:
            break
        adjacent = _paragraphs_excluding(parent, excluded)
        if adjacent and len("\n\n".join(adjacent)) > len(region.get_text(" ", strip=True)):
            region = parent
        if parent.name in {"article", "main"}:
            break
    excluded = {id(node) for node in page_analysis.select_from(region, NOISE)}
    source = _paragraphs_excluding(region, excluded)
    normalized = lambda value: re.sub(r"\s+", " ", value).strip()
    extracted = [normalized(part) for part in extracted_text.split("\n\n") if normalized(part)]
    expected = [normalized(part) for part in source if normalized(part)]
    combined = normalized(extracted_text)
    issues = []
    missing = [part for part in expected if part not in combined]
    if missing:
        issues.append(f"Source coverage omits {len(missing)} observed prose blocks")
    if expected and expected[-1] not in combined:
        issues.append("The actual source ending is missing")
    position = 0
    for paragraph in expected:
        found = combined.find(paragraph, position)
        if found < 0:
            if paragraph in combined:
                issues.append("Source prose order or repeated passages changed")
            break
        position = found + len(paragraph)
    expected_counts = Counter(expected)
    source_combined = normalized("\n\n".join(expected))
    if any(part not in source_combined for part in extracted):
        issues.append("Extraction includes prose outside the confirmed story boundaries")
    if any(count > expected_counts[part] for part, count in Counter(extracted).items() if part in expected_counts):
        issues.append("Extraction duplicates source prose blocks")
    return selector_for(region, soup), source, list(dict.fromkeys(issues))


def readiness_selector(html, proposed=None, *, page_analysis=None):
    soup = page_analysis.soup if page_analysis is not None else BeautifulSoup(html, "lxml")
    # An unhydrated story can lose to visible navigation in a rejected plan.
    # Prefer witnessed story regions, even while their prose is still loading.
    for selector in (".reader-content", "#chapter-content", "#novel_honbun", ".entry-content", "[itemprop=articleBody]"):
        if soup.select_one(selector) is not None:
            return selector
    if proposed and proposed not in {"body", "html", "#root", "#app", "#__next"}:
        try:
            region = soup.select_one(proposed)
            navigation = soup.select("nav, header, footer, aside, .navbar, .site-nav")
            if region is not None and not any(region is node or node in region.parents for node in navigation):
                return proposed
        except Exception:
            pass
    return None
