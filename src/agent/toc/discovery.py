"""Evidence-bound browser actions and navigation discovery."""
import json
import asyncio
from dataclasses import dataclass, field
import re
from typing import Literal, Optional
from urllib.parse import urljoin, urlsplit, parse_qsl, urlencode, urlunsplit
from pydantic import BaseModel, Field
from bs4 import BeautifulSoup
from src.agent.classifier import ChapterLink
from src.agent.instructions import agent_system_instruction
from src.scraper.runtime import canonical_url
from src.scraper.recovery import before_deadline, current_scope, catalog_soup
from src.scraper.telemetry import stage_enabled
from src.agent.api_recipes import ApiRecipe, capture_api_recipe, sanitize_api_request


def capture_observed_api_recipe(*, method: str, url: str, headers: Optional[dict] = None,
                                request_body=None, response_body=None, page_url: str,
                                mapping: Optional[dict[str, str]] = None,
                                representation: str = "json", handler=None) -> Optional[ApiRecipe]:
    """Create a replay recipe from a witnessed exchange, honoring handler trust.

    GET requests are eligible directly. POST requests require the matching
    platform handler to explicitly declare this exact request read-only.
    """
    method = (method or "GET").upper()
    trusted = False
    if method == "POST" and handler is not None:
        body = request_body
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except (ValueError, TypeError):
                return None
        try:
            trusted = bool(handler.is_read_only_api_request(method, url, headers or {}, body))
        except Exception:
            trusted = False
    return capture_api_recipe(method=method, url=url, headers=headers,
        request_body=request_body, response_body=response_body, page_url=page_url,
        mapping=mapping, representation=representation, trusted_read_only=trusted)


def infer_api_response_mapping(response_body):
    """Infer a narrow mapping from observed catalog or chapter-shaped JSON."""
    title_keys = ("title", "chapterTitle", "name")
    url_keys = ("url", "href", "chapterUrl")
    content_keys = ("content", "body", "text", "chapterContent", "html")
    stack = [(response_body, [])]
    while stack:
        value, path = stack.pop()
        if isinstance(value, dict):
            for key in reversed(list(value)):
                stack.append((value[key], path + [str(key)]))
        elif isinstance(value, list):
            dicts = [item for item in value if isinstance(item, dict)]
            if dicts:
                sample = dicts[0]
                title_key = next((key for key in title_keys if isinstance(sample.get(key), str)), None)
                url_key = next((key for key in url_keys if isinstance(sample.get(key), str)), None)
                if title_key and url_key:
                    prefix = ".".join(path)
                    return {"mapping": {"chapters": prefix, "title": title_key, "url": url_key},
                            "representation": "json"}
        if isinstance(value, dict):
            title_key = next((key for key in title_keys if isinstance(value.get(key), str)), None)
            content_key = next((key for key in content_keys if isinstance(value.get(key), (str, list))), None)
            if title_key and content_key:
                content = value[content_key]
                representation = ("paragraphs" if isinstance(content, list) and all(isinstance(p, str) for p in content)
                                  else "html" if isinstance(content, str) and re.search(r"</?[a-z][^>]*>", content, re.I)
                                  else "text" if isinstance(content, str) else "json")
                return {"mapping": {"title": ".".join(path + [title_key]),
                                    "content": ".".join(path + [content_key])},
                        "representation": representation}
    return None


def capture_api_recipe_candidates(responses: list, page_url: str):
    """Convert witnessed JSON exchanges into sanitized recipe candidates."""
    from src.handlers import registry
    handler = registry.find_handler(page_url)
    candidates, seen = [], set()
    for observed in responses:
        observed_page = observed.get("source_page_url")
        if observed_page and canonical_url(observed_page) != canonical_url(page_url):
            continue
        data = observed.get("data")
        mapping_info = infer_api_response_mapping(data)
        if not mapping_info:
            continue
        recipe = capture_observed_api_recipe(
            method=observed.get("method", "GET"), url=observed.get("url", ""),
            headers=observed.get("headers"), request_body=observed.get("post_data"),
            response_body=data, page_url=observed.get("source_page_url") or page_url,
            mapping=mapping_info["mapping"], representation=mapping_info["representation"],
            handler=handler)
        if recipe:
            identity = (recipe.method, recipe.endpoint, json.dumps(recipe.request_body, sort_keys=True))
            if identity not in seen:
                seen.add(identity)
                candidates.append(recipe)
    return candidates


def capture_chapter_api_candidates(responses: list, page_url: str):
    """Return witnessed, sanitized API recipes whose mapping yields chapter prose.

    The returned ApiRecipe.page_evidence is the exact browser page from which
    the request was observed; callers should promote only after chapter review.
    """
    return [recipe for recipe in capture_api_recipe_candidates(responses, page_url)
            if "title" in recipe.response_mapping and "content" in recipe.response_mapping]


def replay_api_catalog(recipe: ApiRecipe, response_body, base_url: str):
    """Apply an observed catalog mapping; return None on stale/invalid shapes for browser fallback."""
    try:
        mapping = recipe.response_mapping
        chapter_path = mapping.get("chapters")
        title_key, url_key = mapping.get("title"), mapping.get("url")
        if not chapter_path or not title_key or not url_key:
            return None
        items = _api_path(response_body, chapter_path)
        if not isinstance(items, list) or not items:
            return None
        chapters, seen = [], set()
        for item in items:
            title = _api_path(item, title_key)
            href = _api_path(item, url_key)
            if not isinstance(title, str) or not title.strip() or not isinstance(href, str):
                return None
            target = canonical_url(urljoin(base_url, href))
            if urlsplit(target).netloc != urlsplit(base_url).netloc or target in seen:
                continue
            seen.add(target)
            chapters.append(ChapterLink(index=len(chapters) + 1, title=title.strip(), url=target))
        return chapters or None
    except (TypeError, ValueError, KeyError):
        return None


def _api_path(value, path):
    current = value
    for part in path.split(".") if path else []:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def network_pagination_requests(responses: list):
    """Create distinct observed request frontiers for cursor-based GET/POST APIs."""
    found = []
    cursor_names = {"cursor", "after", "page", "pagenumber", "offset", "pagetoken"}
    next_names = {"nextcursor", "next_cursor", "nextpage", "next_page", "nextpagetoken", "next_page_token"}

    def find_next(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in next_names and item not in (None, ""):
                    return item
            for item in value.values():
                result = find_next(item)
                if result is not None:
                    return result
        elif isinstance(value, list):
            for item in value:
                result = find_next(item)
                if result is not None:
                    return result
        return None

    def request_slots(value, path=()):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower().replace("_", "") in {name.replace("_", "") for name in cursor_names}:
                    yield path + (key,)
                yield from request_slots(item, path + (key,))

    for response in responses:
        next_value = find_next(response.get("data"))
        if next_value is None:
            continue
        method = (response.get("method") or "GET").upper()
        url = response.get("url")
        if not isinstance(url, str):
            continue
        body = response.get("post_data")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except (ValueError, TypeError):
                continue
        if method == "GET":
            parts = urlsplit(url)
            query = parse_qsl(parts.query, keep_blank_values=True)
            slots = [index for index, (key, _) in enumerate(query)
                     if key.lower().replace("_", "") in {name.replace("_", "") for name in cursor_names}]
            if len(slots) != 1:
                continue
            index = slots[0]
            key = query[index][0]
            query[index] = (key, str(next_value))
            next_url = urlunsplit(parts._replace(query=urlencode(query, doseq=True)))
            safe = sanitize_api_request(method="GET", url=next_url, headers=response.get("headers"),
                page_url=response.get("source_page_url") or response.get("url"))
            if safe:
                found.append(safe)
            continue
        if method != "POST" or not response.get("trusted_read_only") or not isinstance(body, dict):
            continue
        slots = list(request_slots(body))
        if len(slots) != 1:
            continue
        next_body = json.loads(json.dumps(body))
        target = next_body
        for part in slots[0][:-1]:
            target = target[part]
        target[slots[0][-1]] = next_value
        safe = sanitize_api_request(method="POST", url=url, headers=response.get("headers"),
            request_body=next_body, page_url=response.get("source_page_url") or url,
            trusted_read_only=True)
        if safe:
            found.append(safe)
    return found


class BrowserAction(BaseModel):
    action: Literal["click", "select", "scroll", "stop"]
    selector: Optional[str] = None
    value: Optional[str] = None


class BrowserActions(BaseModel):
    actions: list[BrowserAction] = Field(default_factory=list, max_length=8)


@dataclass
class ExpansionResult:
    html: str
    chapters: list
    next_urls: list
    exhausted: bool
    pagination_requests: list = field(default_factory=list)
    api_recipe_candidates: list = field(default_factory=list)
    api_chapter_urls: list = field(default_factory=list)
    pending_controls: list = field(default_factory=list)
    issues: list = field(default_factory=list)

    def __iter__(self):
        return iter((self.html, self.chapters, self.next_urls))


def witnessed_selector(element):
    parts = []
    while element and element.name != "[document]":
        if element.get("id"):
            parts.append(f'[id={json.dumps(element["id"], ensure_ascii=False)}]')
            break
        position = 1 + sum(getattr(sibling, "name", None) == element.name for sibling in element.previous_siblings)
        parts.append(f"{element.name}:nth-of-type({position})")
        element = element.parent
    return " > ".join(reversed(parts))


def catalog_controls(html):
    """Only witnessed, enabled controls; option values never come from a model."""
    soup = catalog_soup(html)
    controls = []
    for node in soup.select('button, [role=button], [role=tab], [aria-expanded=false], a[href^="#"], select'):
        label = node.get_text(" ", strip=True) or node.get("aria-label", "")
        if (node.has_attr("disabled") or node.get("aria-disabled") == "true" or
                node.find_parent("form") or node.find_parent(attrs={"hidden": True}) or
                node.has_attr("hidden") or re.search(r"buy|purchase|subscribe|delete|sign in|log in|checkout|add to list|\brate\b|bookmark|follow|share|report|comment", label, re.I)):
            continue
        item = {"selector": witnessed_selector(node), "label": label, "action": "click"}
        if node.name == "select":
            options = [option for option in node.select("option") if not option.has_attr("disabled")
                       and not option.find_parent("optgroup", disabled=True)]
            ranges = sum(bool(re.fullmatch(r"\s*\d+\s*[-–—〜~]\s*\d+\s*", option.get_text())) for option in options)
            context = " ".join(str(node.get(attr, "")) for attr in ("id", "class", "aria-label", "name"))
            if ranges < 2 and not re.search(r"chapter|episode|catalog|toc|pagination", context, re.I):
                continue
            item.update(action="select", options=[{"value": option.get("value", option.get_text()),
                "label": option.get_text(" ", strip=True), "selected": option.has_attr("selected")} for option in options])
            if item["options"] and not any(option["selected"] for option in item["options"]):
                item["options"][0]["selected"] = True
        else:
            item["automatic"] = bool(node.get("aria-expanded") == "false" or re.search(
                r"load more|show more|show all chapters|all chapters|もっと見る|つづきを表示|展開|加载更多|เพิ่มเติม|\d+\s*[〜～-]\s*\d+", label, re.I)
                or ((node.get("role") == "tab" or node.name == "a") and re.fullmatch(r"chapters|contents|table of contents|episodes", label, re.I)
                    and node.get("aria-selected") != "true"))
        controls.append(item)
    return controls


def pending_catalog_actions(controls, clicked, selected):
    actions = []
    for control in controls:
        if control["action"] == "select":
            for option in control["options"]:
                marker = (control["selector"], option["value"])
                if marker not in selected:
                    actions.append(BrowserAction(action="select", selector=control["selector"], value=option["value"]))
        elif control["automatic"] and control["selector"] not in clicked:
            actions.append(BrowserAction(action="click", selector=control["selector"]))
    return actions


def observed_navigation(html: str, url: str):
    soup = BeautifulSoup(html, "lxml")
    result = {}
    patterns = {
        "toc": r"table\s*of\s*contents|contents|目次|目录|สารบัญ|index",
        "next": r"next\s*(chapter|episode)|次[のへ]?話|下一章|ตอนถัดไป",
        "previous": r"prev(ious)?\s*(chapter|episode)|前[のへ]?話|上一章|ตอนก่อน",
    }
    for link in soup.select("a[href]"):
        href = link.get("href", "")
        absolute = canonical_url(urljoin(url, href))
        if urlsplit(absolute).netloc != urlsplit(url).netloc or urlsplit(absolute).scheme not in {"http", "https"}:
            continue
        label = link.get_text(" ", strip=True)
        for role, pattern in patterns.items():
            if re.search(pattern, label, re.I) or role in link.get("rel", []):
                result.setdefault(role, absolute)
    return result


def network_chapters(responses: list, base_url: str):
    chapters, next_urls = [], []
    seen = set()
    def visit(value, context=""):
        if isinstance(value, list):
            for item in value:
                visit(item, context)
        elif isinstance(value, dict):
            href = value.get("url") or value.get("href") or value.get("chapterUrl")
            title = value.get("title") or value.get("name")
            if isinstance(href, str) and isinstance(title, str) and re.search("chapter|episode|toc|catalog", context, re.I):
                absolute = canonical_url(urljoin(base_url, href))
                if urlsplit(absolute).netloc == urlsplit(base_url).netloc and absolute not in seen:
                    seen.add(absolute)
                    chapters.append(ChapterLink(index=len(chapters) + 1, title=title, url=absolute))
            for key, item in value.items():
                if isinstance(item, str) and key.lower() in {"next", "next_url", "nexturl", "nextpageurl"}:
                    absolute = canonical_url(urljoin(base_url, item))
                    if urlsplit(absolute).netloc == urlsplit(base_url).netloc:
                        next_urls.append(absolute)
                visit(item, context + "/" + key)
    for response in responses:
        visit(response.get("data", {}))
        data = response.get("data")
        if isinstance(data, dict) and data.get("nextCursor") is not None and response.get("url"):
            parts = urlsplit(response["url"])
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            if "cursor" in query:
                query["cursor"] = str(data["nextCursor"])
                next_urls.append(urlunsplit(parts._replace(query=urlencode(query))))
    return chapters, list(dict.fromkeys(next_urls))


async def expand_catalog(url, obscura, extract, llm=None):
    """Accumulate every visible catalog, including virtualized intermediate views."""
    chapters = {}
    if not stage_enabled("browser"):
        raise RuntimeError("Catalog expansion is beyond the active recovery ceiling")
    limits = obscura.limits
    deadline = min(getattr(obscura, "discovery_deadline", float("inf")),
                   current_scope().deadline if current_scope() else float("inf"))
    page_context = getattr(obscura, "get_catalog_page", obscura.get_playwright_page)
    async with page_context(origin=urlsplit(url).netloc) as page:
        responses = []
        api_candidates = {}
        api_chapter_urls = set()
        pending = set()
        async def capture(response):
            if urlsplit(response.url).netloc == urlsplit(url).netloc and "json" in response.headers.get("content-type", ""):
                try:
                    raw = await response.body()
                    if len(raw) <= 2_000_000 and len(responses) < 100:
                        data = json.loads(raw)
                        request = response.request
                        try:
                            request_body = request.post_data
                            request_body = json.loads(request_body) if request_body else None
                        except (ValueError, TypeError):
                            request_body = getattr(request, "post_data", None)
                        try:
                            request_headers = await request.all_headers()
                        except Exception:
                            request_headers = request.headers
                        from src.core.obscura_client import _safe_network_headers
                        safe_headers = _safe_network_headers(request_headers)
                        mapping_info = infer_api_response_mapping(data)
                        if mapping_info and "content" in mapping_info["mapping"]:
                            api_chapter_urls.add(canonical_url(url))
                        from src.handlers import registry
                        handler = registry.find_handler(url)
                        candidate_mapping = mapping_info["mapping"] if mapping_info else {"observed": ""}
                        candidate = capture_observed_api_recipe(
                            method=request.method, url=response.url, headers=safe_headers,
                            request_body=request_body, response_body=data, page_url=url,
                            mapping=candidate_mapping,
                            representation=mapping_info["representation"] if mapping_info else "json",
                            handler=handler)
                        if mapping_info and candidate:
                            identity = (candidate.method, candidate.endpoint,
                                        json.dumps(candidate.request_body, sort_keys=True))
                            api_candidates[identity] = candidate
                        responses.append({"url": response.url, "data": data,
                            "method": request.method, "post_data": request_body,
                            "headers": safe_headers, "source_page_url": url,
                            "trusted_read_only": bool(candidate and candidate.trusted_read_only)})
                except Exception:
                    pass
        def observe(response):
            task = asyncio.create_task(capture(response))
            pending.add(task)
            task.add_done_callback(pending.discard)
        page.on("response", observe)
        await obscura.pace(url)
        await before_deadline(page.goto(url, wait_until="domcontentloaded", timeout=30000), deadline)
        async def visible_controls(snapshot):
            controls = catalog_controls(snapshot)
            selectors = json.dumps([control["selector"] for control in controls])
            # One DOM read avoids a browser round trip for every control.
            visible = await before_deadline(page.evaluate("() => " + selectors + ".filter(selector => { const e = document.querySelector(selector); return e && (e.matches('select') || (e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')); }).map(selector => ({selector, value: document.querySelector(selector).value ?? null}))"), deadline)
            if not isinstance(visible, list):
                return controls
            values = {item["selector"]: item.get("value") for item in visible if isinstance(item, dict)}
            witnessed = set(values) | {item for item in visible if isinstance(item, str)}
            for control in controls:
                if control["action"] == "select" and control["selector"] in values:
                    for option in control["options"]:
                        option["selected"] = option["value"] == values[control["selector"]]
            return [control for control in controls if control["selector"] in witnessed]
        previous, stagnant, clicked = None, 0, set()
        catalog_links_js = r"""() => Array.from(document.querySelectorAll('a[href]'))
            .filter(a => !a.closest('.comments, #comments, .comment-list, .related, .recommendations') &&
                /(?:chapter|episode|prologue|epilogue|第|ตอน|\/read\/)/i.test((a.textContent || '') + ' ' + a.getAttribute('href')))
            .map(a => a.getAttribute('href'))"""
        selected, attempted = set(), set()
        failed_controls = set()
        failed_action = False
        action_issues = []
        exhausted = False
        visual_calls = 0
        for _ in range(limits.toc_cycles):
            if asyncio.get_running_loop().time() >= deadline:
                break
            html = await before_deadline(page.content(), deadline)
            if current_scope():
                await current_scope().observe_catalog(url, html, obscura)
            for chapter in extract(html, url):
                chapters.setdefault(canonical_url(chapter.url), chapter)
            signature = len(chapters)
            stagnant = stagnant + 1 if signature == previous else 0
            previous = signature
            controls = await visible_controls(html)
            for control in controls:
                for option in control.get("options", []):
                    if option["selected"]:
                        selected.add((control["selector"], option["value"]))
            pending_actions = pending_catalog_actions(controls, clicked, selected)
            actions = [action for action in pending_actions if (action.selector, action.value) not in failed_controls]
            saved_state = getattr(obscura, "last_toc_state", {}).copy()
            retained = {canonical_url(ch.url): ch for ch in saved_state.get("extracted_chapters", [])}
            retained.update(chapters)
            saved_state.update(extracted_chapters=list(retained.values()), is_complete=False,
                               pending_catalog_controls=[action.model_dump() for action in pending_actions])
            obscura.last_toc_state = saved_state
            if stagnant >= limits.no_progress_attempts and not actions:
                from src.agent.toc.tools import TocAuditor
                exhausted = not failed_action and not TocAuditor.audit(list(chapters.values()), None, html, check_pagination=False, check_expansion=False)[2]
                break
            progress = getattr(obscura, "on_progress", None)
            if progress:
                progress({"type": "progress", "stage": "discovery", "current": len(chapters), "total": saved_state.get("claimed_chapter_count") or 0,
                          "message": f"Observed {len(chapters)} chapters; {len(actions)} pending catalog controls"})
            if not actions and llm and llm.is_available and stage_enabled("model"):
                if controls:
                    proposed = await before_deadline(llm.generate_json(prompt_text=json.dumps(controls, ensure_ascii=False), schema=BrowserActions,
                        system_instruction=agent_system_instruction("toc")), deadline)
                    if (not proposed.actions or all(action.action == "stop" for action in proposed.actions)) and getattr(llm, "supports_vision", False) and visual_calls < limits.visual_attempts:
                        screenshot = await before_deadline(page.screenshot(type="png", full_page=False), deadline)
                        visual_calls += 1
                        if hasattr(obscura, "metrics"):
                            obscura.metrics["visual_calls"] = visual_calls
                        proposed = await before_deadline(llm.generate_json(
                            prompt_text="Use this screenshot only to choose from these witnessed catalog controls. Return supplied selectors, scroll, or stop.\n" + json.dumps(controls, ensure_ascii=False),
                            images=[screenshot], schema=BrowserActions,
                            system_instruction=agent_system_instruction("toc")), deadline)
                    allowed = {control["selector"]: control for control in controls}
                    actions = [a for a in proposed.actions if a.action in {"stop", "scroll"} or
                               (a.selector in allowed and a.action == allowed[a.selector]["action"] and
                                (a.action != "select" or a.value in {o["value"] for o in allowed[a.selector]["options"]}))]
            for action in actions[:1]:
                if action.action == "stop":
                    break
                if action.action in {"click", "select"} and action.selector:
                    try:
                        fresh = await before_deadline(page.content(), deadline)
                        current = next((control for control in catalog_controls(fresh) if control["selector"] == action.selector), None)
                        if not current or current["action"] != action.action or (action.action == "select" and action.value not in {o["value"] for o in current["options"]}):
                            failed_action = True
                            continue
                        before_links = await before_deadline(page.evaluate(catalog_links_js), deadline)
                        if not isinstance(before_links, list):
                            before_links = [chapter.url for chapter in extract(fresh, url)]
                        marker = (action.action, action.selector, action.value, json.dumps(before_links, sort_keys=True))
                        if marker in attempted:
                            failed_action = True
                            break
                        attempted.add(marker)
                        if action.action == "select":
                            # Styled dropdowns hide their native select. The fresh
                            # witnessed control/option checks above still apply.
                            try:
                                await before_deadline(page.locator(action.selector).first.select_option(action.value, timeout=1500, force=True), deadline)
                            except Exception as native_error:
                                # Some CDP engines lack option.label, which the
                                # Playwright helper reads even for value selection.
                                changed = await before_deadline(page.evaluate("""({selector, value}) => {
                                    const node = document.querySelector(selector);
                                    if (!node || node.tagName.toLowerCase() !== 'select' ||
                                        node.hasAttribute('disabled') || node.getAttribute('aria-disabled') === 'true') return false;
                                    const options = Array.from(node.querySelectorAll('option'));
                                    const option = options.find(o => (o.getAttribute('value') ?? o.textContent) === value);
                                    if (!option || option.hasAttribute('disabled') || option.closest('optgroup[disabled]')) return false;
                                    options.forEach(o => {
                                        o.selected = o === option;
                                        if (o === option) o.setAttribute('selected', ''); else o.removeAttribute('selected');
                                    });
                                    node.value = value;
                                    node.selectedIndex = options.indexOf(option);
                                    node.dispatchEvent(new Event('input', {bubbles: true}));
                                    node.dispatchEvent(new Event('change', {bubbles: true}));
                                    return true;
                                }""", {"selector": action.selector, "value": action.value}), deadline)
                                if changed is not True:
                                    raise native_error
                            selected.add((action.selector, action.value))
                        else:
                            await before_deadline(page.locator(action.selector).first.click(timeout=1500), deadline)
                        if progress:
                            label = next((option["label"] for option in current.get("options", []) if option["value"] == action.value), current["label"])
                            progress({"type": "progress", "stage": "discovery", "current": len(chapters), "total": 0,
                                      "message": f"Opened catalog range: {label[:80]}" if action.action == "select" else f"Opened catalog control: {label[:80]}"})
                        clicked.add(action.selector)
                        try:
                            await before_deadline(page.wait_for_function("before => JSON.stringify((" + catalog_links_js + ")()) !== JSON.stringify(before)", arg=before_links, timeout=5000), deadline)
                        except Exception:
                            pass
                        interim = await before_deadline(page.content(), deadline)
                        if current_scope():
                            await current_scope().observe_catalog(url, interim, obscura)
                        for chapter in extract(interim, url):
                            chapters.setdefault(canonical_url(chapter.url), chapter)
                        if action.action == "click" and re.search(r"load more|show more|もっと見る|加载更多|เพิ่มเติม", current["label"], re.I) and interim != fresh:
                            clicked.discard(action.selector)
                    except Exception as error:
                        failed_action = True
                        failed_controls.add((action.selector, action.value))
                        from src.scraper.runtime import safe_error
                        message = "Catalog control failed: " + safe_error(error)
                        if message not in action_issues:
                            action_issues.append(message)
                        if progress:
                            progress({"type": "progress", "stage": "discovery", "message": message,
                                      "current": len(chapters), "total": 0})
                        continue
            if actions and actions[0].action == "stop":
                break
            if actions and actions[0].action in {"click", "select"}:
                continue  # Re-observe mutated DOM before selecting another control.
            before_links = [anchor.get("href") for anchor in BeautifulSoup(await before_deadline(page.content(), deadline), "lxml").select("a[href]")]
            await before_deadline(page.evaluate("() => window.scrollBy(0, Math.max(300, window.innerHeight * 0.8))"), deadline)
            try:
                await before_deadline(page.wait_for_function("before => JSON.stringify(Array.from(document.querySelectorAll('a[href]'), a => a.getAttribute('href'))) !== JSON.stringify(before)", arg=before_links, timeout=1500), deadline)
            except Exception:
                pass
        if pending:
            try:
                await before_deadline(asyncio.gather(*list(pending), return_exceptions=True), deadline)
            except asyncio.TimeoutError:
                for task in pending:
                    task.cancel()
        network, next_urls = network_chapters(responses, url)
        next_requests = network_pagination_requests(responses)
        for chapter in network:
            chapters.setdefault(chapter.url, chapter)
        try:
            html = await before_deadline(page.content(), deadline)
        except asyncio.TimeoutError:
            exhausted = False
        for chapter in extract(html, url):
            chapters.setdefault(canonical_url(chapter.url), chapter)
        controls = await visible_controls(html)
        for control in controls:
            for option in control.get("options", []):
                if option["selected"]:
                    selected.add((control["selector"], option["value"]))
        pending_controls = [action.model_dump() for action in pending_catalog_actions(controls, clicked, selected)]
        if pending_controls or failed_action:
            exhausted = False
        elif not exhausted:
            from src.agent.toc.tools import ClaimInspector, TocAuditor
            claimed = ClaimInspector.inspect(html, url)[0]
            if claimed:
                exhausted = TocAuditor.audit(list(chapters.values()), claimed, html,
                    check_pagination=False, check_expansion=False)[0]
        return ExpansionResult(html, list(chapters.values()), next_urls, exhausted,
                               pagination_requests=next_requests,
                               api_recipe_candidates=list(api_candidates.values()),
                               api_chapter_urls=sorted(api_chapter_urls),
                               pending_controls=pending_controls, issues=action_issues)
