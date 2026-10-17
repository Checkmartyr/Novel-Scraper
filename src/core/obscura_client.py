import asyncio
import subprocess
import time
import re
import os
import socket
from urllib.parse import urlsplit
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import httpx
from src.scraper.runtime import ScraperLimits, access_error
from src.scraper.recovery import FetchRecord, before_deadline, current_scope, wordpress_resource, operation_deadline
from pathlib import Path
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
from functools import wraps
from src.scraper.telemetry import stage, stage_enabled

from playwright.async_api import async_playwright, Browser, Page, Playwright
from bs4 import BeautifulSoup

import logging
from src.core.binary_manager import ensure_obscura
from src.config import NAVIGATION_TIMEOUT, WAIT_UNTIL

logger = logging.getLogger("core.obscura_client")


def measured_fetch(function):
    @wraps(function)
    async def wrapped(self, url, *args, **kwargs):
        with stage("browser" if kwargs.get("force_browser") else "retrieval") as measurement:
            try:
                timeout = kwargs.get("timeout", 60 if os.getenv("NOVEL_SCRAPER_RECOVERY_MODE") == "maximum" else NAVIGATION_TIMEOUT)
                html = await before_deadline(function(self, url, *args, **kwargs), self._deadline(timeout))
                measurement.update(outcome="observed", reason="Source retrieved; content awaits audit")
                return html
            except BaseException as error:
                from src.scraper.runtime import safe_error
                previous = self.fetch_records.get(url)
                self._record_fetch(url, previous.final_url if previous else url,
                    previous.status if previous else 0, previous.headers if previous else {},
                    previous.redirects if previous else (), "browser" if kwargs.get("force_browser") else "http",
                    safe_error(error))
                raise
    return wrapped


def is_cloudflare_challenge(html: str) -> bool:
    """Check if page content indicates an unresolved Cloudflare challenge."""
    if not html:
        return False
    lower = html.lower()
    return (
        "just a moment..." in lower
        or "cf-chl-widget" in lower
        or "challenges.cloudflare.com" in lower
        or "performing security verification" in lower
    )


async def _is_port_open(host: str, port: int) -> bool:
    """Check if a TCP port is open and accepting connections."""
    try:
        reader, writer = await asyncio.open_connection(host, port)
        writer.close()
        await writer.wait_closed()
        return True
    except OSError:
        return False


class ObscuraClient:
    """Client for driving Obscura via Playwright connected over CDP."""

    def __init__(self, bin_path: Optional[Path] = None, cdp_port: Optional[int] = None):
        self.bin_path = bin_path
        if cdp_port is None:
            with socket.socket() as candidate:
                candidate.bind(("127.0.0.1", 0))
                cdp_port = candidate.getsockname()[1]
        self.cdp_port = cdp_port
        self._server_process: Optional[asyncio.subprocess.Process] = None
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._browser_loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = asyncio.Lock()
        self._cached_cookies: Dict[str, str] = {}
        self._cached_user_agent: Optional[str] = None
        self.limits = ScraperLimits.from_env()
        self._http_client = None
        self._contexts = {}
        self._page_limits = {}
        self._origin_locks = {}
        self._last_request = {}
        self.network_responses = []
        self.fetch_records = {}
        self.fetch_history = []
        self.metrics = {"requests": 0, "rendered": 0, "browser_launches": 0, "recipe_hits": 0, "repair_calls": 0}
        self._storage_state = os.getenv("NOVEL_SCRAPER_STORAGE_STATE") or None

    def _record_fetch(self, requested, final, status=200, headers=None, redirects=(), transport="http", error=""):
        record = FetchRecord(requested, final, status, dict(headers or {}), tuple(redirects), transport, error)
        self.fetch_records[requested] = record
        self.fetch_history.append(record)
        if current_scope():
            current_scope().note_redirect(requested, final, redirects)

    def _deadline(self, seconds):
        return min(time.monotonic() + seconds, operation_deadline())

    async def fetch_advertised_document(self, url, html, headers=None):
        """Read a WordPress document only through its advertised JSON resource."""
        resource = wordpress_resource(html, url, headers)
        if not resource:
            return None
        client = await self.http_client()
        await self.pace(resource)
        self.metrics["requests"] += 1
        response = await before_deadline(client.get(resource), self._deadline(12))
        self._record_fetch(resource, str(response.url), response.status_code, response.headers,
                           [str(item.url) for item in response.history])
        response.raise_for_status()
        data = response.json()
        body = data.get("content", {}).get("rendered")
        title = data.get("title", {}).get("rendered", "")
        if not isinstance(body, str) or not body.strip():
            return None
        from html import escape
        document = f'<html><h1>{escape(BeautifulSoup(title, "lxml").get_text(" ", strip=True))}</h1><article class="entry-content">{body}</article></html>'
        canonical = data.get("link") or url
        if urlsplit(canonical).hostname != urlsplit(url).hostname:
            canonical = url
        self._record_fetch(url, canonical, response.status_code, response.headers, transport="wordpress_api")
        return document

    async def start_cdp_server(self, stealth: bool = True, allow_private_network: bool = False) -> None:
        """Start obscura serve in background if not already running."""
        # If port is already open, server is already running and ready
        if await _is_port_open("127.0.0.1", self.cdp_port):
            return

        if self._server_process and self._server_process.returncode is None:
            return

        cmd = [
            str(self.bin_path or ensure_obscura()),
            "serve",
            "-p", str(self.cdp_port),
            "--quiet",
        ]
        if allow_private_network:
            cmd.append("--allow-private-network")
        if stealth:
            cmd.append("--stealth")

        self._server_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.metrics["browser_launches"] += 1

        # Wait up to 5s until CDP server binds port and is ready
        for _ in range(50):
            if await _is_port_open("127.0.0.1", self.cdp_port):
                break
            await asyncio.sleep(0.1)

    async def stop_cdp_server(self) -> None:
        """Stop background CDP server."""
        if self._server_process and self._server_process.returncode is None:
            try:
                self._server_process.terminate()
                await asyncio.wait_for(self._server_process.wait(), timeout=3.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._server_process.kill()
                except ProcessLookupError:
                    pass
                await self._server_process.wait()
            finally:
                self._server_process = None

    async def _ensure_browser(self, stealth: bool = True, allow_private_network: bool = False) -> Browser:
        """Ensure Playwright is running and connected to Obscura CDP server."""
        async with self._lock:
            await self.start_cdp_server(stealth=stealth, allow_private_network=allow_private_network)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if self._browser and self._browser_loop == loop and self._browser.is_connected():
                return self._browser

            # Clean up old connection if loop changed or disconnected
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
                self._browser = None
                self._contexts.clear()
                self._page_limits.clear()

            self._playwright = await async_playwright().start()
            ws_endpoint = f"ws://127.0.0.1:{self.cdp_port}"
            self._browser = await self._playwright.chromium.connect_over_cdp(ws_endpoint)
            self._browser_loop = loop
            return self._browser

    @asynccontextmanager
    async def get_playwright_page(self, stealth: bool = True, allow_private_network: bool = False, origin: str = "default"):
        """Context manager to obtain a Playwright page connected to Obscura CDP."""
        browser = await self._ensure_browser(stealth=stealth, allow_private_network=allow_private_network)
        async with self._lock:
            if origin not in self._contexts:
                self._contexts[origin] = await browser.new_context(storage_state=self._storage_state)
                self._page_limits[origin] = asyncio.Semaphore(self.limits.browser_pages)
            context = self._contexts[origin]
        async with self._page_limits[origin]:
            page: Page = await context.new_page()
            try:
                yield page
            finally:
                try:
                    await before_deadline(page.close(), time.monotonic() + self.limits.cleanup_seconds)
                except (Exception, asyncio.CancelledError):
                    logger.warning("Browser page cleanup did not finish within its budget")

    async def pace(self, url: str):
        origin = urlsplit(url).netloc
        lock = self._origin_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            delay = float(os.getenv("MIN_DELAY_SECONDS", "0.5"))
            await asyncio.sleep(max(0, self._last_request.get(origin, 0) + delay - time.monotonic()))
            self._last_request[origin] = time.monotonic()

    async def http_client(self):
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(follow_redirects=True, timeout=12,
                headers={"User-Agent": self.get_user_agent() or "Mozilla/5.0", "Accept-Language": "en-US,ja,th;q=0.8"})
            if self._storage_state:
                import json
                state = json.loads(Path(self._storage_state).read_text(encoding="utf-8"))
                for cookie in state.get("cookies", []):
                    self._http_client.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""), path=cookie.get("path", "/"))
        return self._http_client

    def get_cookies(self) -> Dict[str, str]:
        """Return cached session cookies (e.g. cf_clearance) if available."""
        return dict(self._cached_cookies)

    def get_user_agent(self) -> Optional[str]:
        """Return cached user agent if available."""
        return self._cached_user_agent

    async def _fetch_with_chrome_fallback(self, url: str, timeout: int = 30) -> str:
        """Fallback to launching system Chrome in headed mode without automation flags to pass Turnstile."""
        logger.info(f"Cloudflare Turnstile challenge detected! Launching system Chrome fallback for {url}...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                channel="chrome",
                headless=False,
                ignore_default_args=["--enable-automation"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--window-size=1280,800",
                ]
            )
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                # Wait up to 12 seconds for Cloudflare challenge to auto-resolve
                for _ in range(12):
                    await asyncio.sleep(1)
                    try:
                        title = await page.title()
                        if not is_cloudflare_challenge(title) and title:
                            # Let redirect finish loading
                            await asyncio.sleep(1)
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=5000)
                            except Exception:
                                pass
                            break
                    except Exception:
                        continue
                
                content = ""
                for _ in range(5):
                    try:
                        content = await page.content()
                        if content and not is_cloudflare_challenge(content):
                            break
                    except Exception:
                        await asyncio.sleep(0.5)

                cookies = await context.cookies()
                self._cached_cookies = {c["name"]: c["value"] for c in cookies}
                try:
                    self._cached_user_agent = await page.evaluate("() => navigator.userAgent")
                except Exception:
                    pass
                try:
                    cur_title = await page.title()
                except Exception:
                    cur_title = "Unknown"
                logger.info(
                    f"System Chrome successfully resolved Cloudflare for {url}! "
                    f"Title: '{cur_title}', Cookies: {list(self._cached_cookies.keys())}"
                )
                return content
            finally:
                await browser.close()

    @measured_fetch
    async def fetch_html(
        self,
        url: str,
        stealth: bool = True,
        timeout: int = NAVIGATION_TIMEOUT,
        allow_private_network: bool = False,
        wait_until: str = WAIT_UNTIL,
        force_browser: bool = False,
        content_selector: Optional[str] = None,
        readiness: Optional[str] = None,
    ) -> str:
        """Fetch page HTML using Playwright over Obscura CDP, with automatic Cloudflare fallback."""
        if os.getenv("NOVEL_SCRAPER_RECOVERY_MODE") == "maximum" and timeout == NAVIGATION_TIMEOUT:
            timeout = max(timeout, 60)
        # Check platform handler registry for direct API or synthesized HTML bypass
        from src.handlers import registry
        handler = registry.find_handler(url)
        if handler and not force_browser:
            try:
                await self.pace(url)
                self.metrics["requests"] += 1
                result = await handler.handle(url, obscura_client=self)
                if result:
                    self._record_fetch(url, url, transport="handler")
                    return result
            except Exception as e:
                logger.warning(f"Platform handler error for {url}: {e}. Falling back to browser.")

        # Fast HTTP attempt (avoids browser overhead & ad context crashes on static novel pages)
        if not force_browser:
            client = await self.http_client()
            for attempt in range(self.limits.request_attempts):
                try:
                    await self.pace(url)
                    self.metrics["requests"] += 1
                    resp = await before_deadline(client.get(url), self._deadline(timeout))
                    self._record_fetch(url, str(resp.url), resp.status_code, resp.headers,
                                       [str(item.url) for item in resp.history])
                    if resp.status_code in (401, 403):
                        # Browser state can carry tokens in local storage instead of cookies.
                        break
                    if resp.status_code == 429 or resp.status_code >= 500:
                        retry = resp.headers.get("Retry-After", "")
                        try:
                            wait = float(retry)
                        except ValueError:
                            try:
                                wait = max(0, (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds())
                            except (TypeError, ValueError):
                                wait = 2 ** attempt
                        if attempt + 1 < self.limits.request_attempts:
                            # All requests for this origin observe the cooldown.
                            self._last_request[urlsplit(url).netloc] = time.monotonic() + min(wait, self.limits.discovery_seconds)
                            continue
                        raise RuntimeError(f"Source returned HTTP {resp.status_code} after bounded retries")
                    if resp.status_code == 200:
                        blocker = access_error(resp.text)
                        if blocker:
                            if self._storage_state:
                                break
                            raise PermissionError(blocker)
                        if len(resp.text) > 200 and not is_cloudflare_challenge(resp.text):
                            soup = BeautifulSoup(resp.text, "lxml")
                            story = soup.select_one(".entry-content, [itemprop=articleBody], article")
                            if wordpress_resource(resp.text, str(resp.url), resp.headers) and (not story or not story.get_text(strip=True)):
                                try:
                                    document = await self.fetch_advertised_document(str(resp.url), resp.text, resp.headers)
                                    if document:
                                        return document
                                except (httpx.HTTPError, ValueError):
                                    pass
                            return resp.text
                    break
                except PermissionError:
                    raise
                except httpx.HTTPError:
                    if attempt + 1 == self.limits.request_attempts:
                        break

        content: Optional[str] = None
        if not stage_enabled("browser"):
            raise RuntimeError("Browser retrieval is beyond the active recovery ceiling")
        await self.pace(url)
        self.metrics["requests"] += 1
        self.metrics["rendered"] += 1
        capture_tasks = set()
        async def capture(response):
            if urlsplit(response.url).netloc != urlsplit(url).netloc:
                return
            if "json" not in response.headers.get("content-type", ""):
                return
            try:
                body = await response.body()
                if len(body) <= 2_000_000 and len(self.network_responses) < 100:
                    import json
                    self.network_responses.append({"url": response.url, "data": json.loads(body)})
            except Exception:
                pass
        def observe(response):
            task = asyncio.create_task(capture(response))
            capture_tasks.add(task)
            task.add_done_callback(capture_tasks.discard)
        async with self.get_playwright_page(stealth=stealth, allow_private_network=allow_private_network, origin=urlsplit(url).netloc) as page:
            page.on("response", observe)
            # Block aggressive ad networks that crash frame execution contexts
            try:
                ad_re = re.compile(
                    r"\b(doubleclick|google-analytics|googletagservices|adnxs|adsystem|taboola|outbrain|disqus|criteo|pubmatic)\b",
                    re.I
                )
                await page.route(ad_re, lambda route: route.abort())
            except Exception:
                pass

            deadline = self._deadline(timeout)
            if readiness == "chapter" and not content_selector:
                content_selector = ".reader-content, #chapter-content, #novel_honbun, .entry-content, [itemprop=articleBody], article, main"
            expression = "selector => Array.from(document.querySelectorAll(selector)).some(element => element.textContent.trim().length > 0 && !element.matches('.skeleton, [aria-busy=true]') && !element.querySelector('.skeleton, [aria-busy=true]') && !/^(loading|please wait|加载中|読み込み中)[.\\s…]*$/i.test(element.textContent.trim()))"
            if readiness == "catalog":
                content_selector = content_selector or "body"
                expression = "selector => Array.from(document.querySelectorAll(selector)).some(element => Array.from(element.querySelectorAll('a[href]')).filter(a => /chapter|episode|第|ตอน/i.test(a.textContent)).length >= 2)"
            try:
                response = await before_deadline(page.goto(url, wait_until=wait_until, timeout=timeout * 1000), deadline)
                final = getattr(page, "url", url)
                self._record_fetch(url, final if isinstance(final, str) else url,
                    response.status if response and isinstance(response.status, int) else 0,
                    transport="browser")
                if content_selector:
                    await before_deadline(page.wait_for_function(
                        expression,
                        arg=content_selector,
                        timeout=timeout * 1000,
                    ), deadline)
                content = await before_deadline(page.content(), deadline)
            except Exception as e:
                # Preserve available source if navigation or hydration failed.
                try:
                    content = await before_deadline(page.evaluate("() => document.documentElement.outerHTML"), deadline)
                except Exception:
                    try:
                        content = await before_deadline(page.content(), deadline)
                    except Exception:
                        pass
                if content_selector:
                    try:
                        await before_deadline(page.wait_for_function(
                            expression,
                            arg=content_selector,
                            timeout=timeout * 1000,
                        ), deadline)
                        content = await before_deadline(page.content(), deadline)
                    except Exception as wait_error:
                        selected = BeautifulSoup(content or "", "lxml").select(content_selector)
                        if not any(element.get_text(strip=True) and not element.select_one('.skeleton, [aria-busy=true]')
                                   and element.get('aria-busy') != 'true' and not re.fullmatch(r'(loading|please wait|加载中|読み込み中)[.\s…]*', element.get_text(strip=True), re.I) for element in selected):
                            raise TimeoutError(
                                f"Timed out waiting for populated content selector: {content_selector}"
                            ) from wait_error
                if not content or (len(content) <= 300 and not is_cloudflare_challenge(content)):
                    if "evaluate" in str(e) or "TargetClosedError" in str(e):
                        raise PermissionError("Browser could not access source; user action required") from e
                    raise e

            if capture_tasks:
                try:
                    await before_deadline(asyncio.gather(*list(capture_tasks), return_exceptions=True), deadline)
                except asyncio.TimeoutError:
                    for task in capture_tasks:
                        task.cancel()
        blocker = access_error(content or "")
        if blocker or is_cloudflare_challenge(content or ""):
            raise PermissionError(blocker or "Security challenge requires user action")
        final_url = getattr(page, "url", url)
        previous = self.fetch_records.get(url)
        self._record_fetch(url, final_url if isinstance(final_url, str) else url,
                           previous.status if previous and previous.transport == "browser" else 0, transport="browser")
            
        return content or ""

    async def fetch_markdown(
        self,
        url: str,
        stealth: bool = True,
        timeout: int = NAVIGATION_TIMEOUT,
        allow_private_network: bool = False,
        wait_until: str = WAIT_UNTIL,
    ) -> str:
        """Fetch page converted to Markdown via Playwright HTML fetch and BeautifulSoup."""
        html = await self.fetch_html(
            url,
            stealth=stealth,
            timeout=timeout,
            allow_private_network=allow_private_network,
            wait_until=wait_until,
        )
        soup = BeautifulSoup(html, "lxml")
        return soup.get_text("\n\n", strip=True)

    async def close(self) -> None:
        with stage("cleanup") as measurement:
            await self._close_bounded()
            measurement.update(outcome="bounded" if self.metrics.get("cleanup_timeouts") else "completed",
                               reason="Owned browser cleanup budget enforced")

    async def _close_bounded(self) -> None:
        try:
            await before_deadline(self._close_resources(), time.monotonic() + self.limits.cleanup_seconds)
        except asyncio.TimeoutError:
            self.metrics["cleanup_timeouts"] = self.metrics.get("cleanup_timeouts", 0) + 1
            process = self._server_process
            if process and process.returncode is None:
                try:
                    process.kill()
                    await before_deadline(process.wait(), time.monotonic() + 1)
                except (ProcessLookupError, asyncio.TimeoutError):
                    pass
            self._server_process = self._browser = self._playwright = None
            self._contexts.clear()

    async def _close_resources(self) -> None:
        """Clean up Playwright browser, playwright context, and CDP server."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        await asyncio.gather(*(context.close() for context in self._contexts.values()), return_exceptions=True)
        self._contexts.clear()
        if self._browser and self._server_process:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        await self.stop_cdp_server()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
