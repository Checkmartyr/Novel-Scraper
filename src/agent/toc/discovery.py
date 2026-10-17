"""Evidence-bound browser actions and navigation discovery."""
import json
import asyncio
from dataclasses import dataclass
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


class BrowserAction(BaseModel):
    action: Literal["click", "scroll", "stop"]
    selector: Optional[str] = None


class BrowserActions(BaseModel):
    actions: list[BrowserAction] = Field(default_factory=list, max_length=8)


@dataclass
class ExpansionResult:
    html: str
    chapters: list
    next_urls: list
    exhausted: bool

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
    async with obscura.get_playwright_page(origin=urlsplit(url).netloc) as page:
        responses = []
        pending = set()
        async def capture(response):
            if urlsplit(response.url).netloc == urlsplit(url).netloc and "json" in response.headers.get("content-type", ""):
                try:
                    raw = await response.body()
                    if len(raw) <= 2_000_000 and len(responses) < 100:
                        data = json.loads(raw)
                        responses.append({"url": response.url, "data": data})
                        # Replay only an observed cursor field on an observed API URL.
                        if isinstance(data, dict) and data.get("nextCursor") is not None:
                            parts = urlsplit(response.url)
                            query = dict(parse_qsl(parts.query))
                            if "cursor" in query:
                                query["cursor"] = str(data["nextCursor"])
                                responses.append({"data": {"next_url": urlunsplit(parts._replace(query=urlencode(query)))}})
                except Exception:
                    pass
        def observe(response):
            task = asyncio.create_task(capture(response))
            pending.add(task)
            task.add_done_callback(pending.discard)
        page.on("response", observe)
        await obscura.pace(url)
        await before_deadline(page.goto(url, wait_until="domcontentloaded", timeout=30000), deadline)
        previous, stagnant, clicked = None, 0, set()
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
            if stagnant >= limits.no_progress_attempts:
                from src.agent.toc.tools import TocAuditor
                exhausted = not TocAuditor.audit(list(chapters.values()), None, html, check_pagination=False)[2]
                break
            previous = signature
            soup = catalog_soup(html)
            actions = []
            for button in soup.select("button, [role=button], [aria-expanded=false]"):
                label = button.get_text(" ", strip=True)
                if button.has_attr("disabled") or button.has_attr("hidden") or button.get("aria-disabled") == "true" or button.find_parent("form") or re.search(r"buy|purchase|subscribe|delete|sign in|log in|checkout", label, re.I):
                    continue
                if button.get("aria-expanded") == "false" or re.search(r"load more|show more|もっと見る|つづきを表示|展開|加载更多|เพิ่มเติม|\d+\s*[〜～-]\s*\d+", label, re.I):
                    selector = witnessed_selector(button)
                    if selector not in clicked or button.get("aria-expanded") == "false" or re.search(r"load more|show more|もっと見る|つづきを表示|加载更多|เพิ่มเติม", label, re.I):
                        actions.append(BrowserAction(action="click", selector=selector))
            if not actions and llm and llm.is_available and stage_enabled("model"):
                controls = [{"selector": witnessed_selector(b), "label": b.get_text(" ", strip=True) or b.get("aria-label", "")} for b in soup.select("button, [role=button]") if not b.find_parent("form") and not b.has_attr("disabled") and not b.has_attr("hidden") and b.get("aria-disabled") != "true" and not re.search(r"buy|purchase|subscribe|delete|sign in|log in|checkout", b.get_text(" ", strip=True), re.I)]
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
                    allowed = {control["selector"] for control in controls}
                    actions = [a for a in proposed.actions if a.action != "click" or a.selector in allowed]
            for action in actions[:1]:
                if action.action == "stop":
                    break
                if action.action == "click" and action.selector:
                    try:
                        before_links = [anchor.get("href") for anchor in BeautifulSoup(await before_deadline(page.content(), deadline), "lxml").select("a[href]")]
                        await before_deadline(page.locator(action.selector).first.click(timeout=1500), deadline)
                        clicked.add(action.selector)
                        try:
                            await before_deadline(page.wait_for_function("before => JSON.stringify(Array.from(document.querySelectorAll('a[href]'), a => a.getAttribute('href'))) !== JSON.stringify(before)", arg=before_links, timeout=1500), deadline)
                        except Exception:
                            pass
                        interim = await before_deadline(page.content(), deadline)
                        if current_scope():
                            await current_scope().observe_catalog(url, interim, obscura)
                        for chapter in extract(interim, url):
                            chapters.setdefault(canonical_url(chapter.url), chapter)
                    except Exception:
                        continue
            if actions and actions[0].action == "stop":
                break
            if actions and actions[0].action == "click":
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
        for chapter in network:
            chapters.setdefault(chapter.url, chapter)
        try:
            html = await before_deadline(page.content(), deadline)
        except asyncio.TimeoutError:
            exhausted = False
        return ExpansionResult(html, list(chapters.values()), next_urls, exhausted)
