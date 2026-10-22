import asyncio
import subprocess
import time
import re
import os
import socket
import json
from urllib.parse import urlsplit
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import httpx
from src.scraper.runtime import ScraperLimits, access_error
from src.scraper.recovery import FetchRecord, before_deadline, current_scope, wordpress_resource, operation_deadline
from pathlib import Path
from typing import Optional, Dict, Any
import hashlib
from contextlib import asynccontextmanager
from functools import wraps
from src.scraper.telemetry import stage, stage_enabled

from playwright.async_api import async_playwright, Browser, Page, Playwright
from bs4 import BeautifulSoup

import logging
from src.core.binary_manager import ensure_obscura
from src.core.response_cache import ResponseCache
from src.config import NAVIGATION_TIMEOUT, WAIT_UNTIL

logger = logging.getLogger("core.obscura_client")


def _safe_network_headers(headers):
    """Keep useful representation metadata while dropping credential-bearing headers."""
    sensitive = ("authorization", "auth", "cookie", "token", "secret", "api-key", "apikey",
                 "proxy-auth", "credential", "bearer", "signature", "jwt", "session")
    return {name: value for name, value in (headers or {}).items()
            if not any(marker in name.lower() for marker in sensitive)}


def measured_fetch(function):
    @wraps(function)
    async def wrapped(self, url, *args, **kwargs):
        with stage("browser" if kwargs.get("force_browser") else "retrieval") as measurement:
            progress = getattr(self, "on_progress", None)
            if progress:
                progress({"type": "progress", "stage": "discovery", "current": 0, "total": 0,
                          "message": "Rendering source" if kwargs.get("force_browser") else "Retrieving source"})
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
                if progress:
                    progress({"type": "progress", "stage": "discovery", "current": 0, "total": 0,
                              "message": "Retrieval failed: " + safe_error(error)})
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

    def __init__(self, bin_path: Optional[Path] = None, cdp_port: Optional[int] = None,
                 state_dir: Optional[Path] = None):
        self.bin_path = bin_path
        if cdp_port is None:
            with socket.socket() as candidate:
                candidate.bind(("127.0.0.1", 0))
                cdp_port = candidate.getsockname()[1]
        self.cdp_port = cdp_port
        self._server_process: Optional[asyncio.subprocess.Process] = None
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._catalog_browser: Optional[Browser] = None
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
        self._response_cache = ResponseCache(Path(state_dir) / "response-cache") if state_dir else None

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

    async def request_api_json(self, method: str, url: str, *, headers=None, json_body=None,
                               timeout: int = 15, trusted_read_only: bool = False):
        """Replay one witnessed GET or declared read-only POST with the current session."""
        method = (method or "").upper()
        if method not in {"GET", "POST"} or (method == "POST" and not trusted_read_only):
            raise ValueError("Only observed GET and trusted read-only POST requests can be replayed")
        from src.agent.api_recipes import sanitize_api_request
        safe_request = sanitize_api_request(method=method, url=url, headers=headers,
            request_body=json_body if method == "POST" else None,
            page_url=url, trusted_read_only=trusted_read_only)
        if not safe_request or safe_request["url"] != url:
            raise ValueError("API request contains unsupported or secret-bearing values")
        safe_headers = safe_request["headers"]
        json_body = safe_request["body"]
        client = await self.http_client()
        await self.pace(url)
        self.metrics["requests"] += 1
        response = await before_deadline(client.request(method, url, headers=safe_headers,
            json=json_body if method == "POST" else None), self._deadline(timeout))
        self._record_fetch(url, str(response.url), response.status_code, response.headers,
                           [str(item.url) for item in response.history], transport="api")
        response.raise_for_status()
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("Observed API response is no longer valid JSON") from error

    async def replay_api_recipe(self, recipe):
        """Return replayed API JSON, or None so callers can fall back to browser extraction."""
        try:
            from src.agent.api_recipes import ApiRecipe
            candidate = ApiRecipe.model_validate(recipe)
            return await self.request_api_json(candidate.method, candidate.endpoint,
                headers=candidate.request_headers, json_body=candidate.request_body,
                trusted_read_only=candidate.trusted_read_only)
        except Exception as error:
            logger.debug("API recipe replay failed; browser fallback remains available: %s", error)
            return None

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
                self._catalog_browser = None
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

    @asynccontextmanager
    async def get_catalog_page(self, origin="default"):
        """Use standard DOM events when a CDP engine lacks native select APIs."""
        async with self.get_playwright_page(origin=origin) as page:
            try:
                supported = await before_deadline(page.evaluate("""() => {
                    const select = document.createElement('select');
                    const option = document.createElement('option');
                    return typeof option.label === 'string' && typeof select.options === 'object';
                }"""), self._deadline(5))
            except Exception:
                supported = False
            if supported is True:
                yield page
                return
        try:
            async with self._lock:
                if self._catalog_browser is None:
                    errors = []
                    # Only existing browser installations; never download one.
                    for channel in ("chrome", "msedge", None):
                        try:
                            self._catalog_browser = await before_deadline(
                                self._playwright.chromium.launch(channel=channel, headless=True, timeout=10000),
                                self._deadline(10))
                            break
                        except Exception as error:
                            errors.append(type(error).__name__)
                    if self._catalog_browser is None:
                        raise RuntimeError("No installed standards browser is available: " + ", ".join(errors))
                    self.metrics["browser_launches"] += 1
                key = "catalog:" + origin
                if key not in self._contexts:
                    self._contexts[key] = await self._catalog_browser.new_context(storage_state=self._storage_state)
                context = self._contexts[key]
        except Exception as error:
            logger.warning("Standard catalog browser unavailable: %s", type(error).__name__)
            async with self.get_playwright_page(origin=origin) as page:
                yield page
            return
        if getattr(self, "on_progress", None):
            self.on_progress({"type": "progress", "stage": "discovery", "current": 0, "total": 0,
                              "message": "Using installed browser for interactive catalog controls"})
        async with self._page_limits[origin]:
            page = await context.new_page()
            try:
                yield page
            finally:
                try:
                    await before_deadline(page.close(), time.monotonic() + self.limits.cleanup_seconds)
                except (Exception, asyncio.CancelledError):
                    logger.warning("Catalog browser page cleanup exceeded its budget")

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
        refresh: bool = False,
    ) -> str:
        """Fetch page HTML using Playwright over Obscura CDP, with automatic Cloudflare fallback."""
        refresh = bool(refresh or getattr(self, "refresh_mode", False))
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
            request_headers = dict(client.headers)
            session_material = "\n".join(sorted(f"{cookie.domain}|{cookie.path}|{cookie.name}|{cookie.value}"
                                                   for cookie in client.cookies.jar))
            session_material += "|browser-cookies:" + json.dumps(self._cached_cookies, sort_keys=True)
            for name, value in client.headers.items():
                if name.lower() in {"authorization", "proxy-authorization"}:
                    session_material += f"|{name.lower()}:{hashlib.sha256(value.encode()).hexdigest()}"
            if self._storage_state:
                # Include the configured browser-state identity without persisting its contents.
                session_material += "|" + str(Path(self._storage_state).resolve())
            session = hashlib.sha256(session_material.encode("utf-8")).hexdigest()
            cached = self._response_cache.load(url, session, request_headers) if refresh and self._response_cache else None
            conditional = {}
            if cached:
                if cached.headers.get("etag"):
                    conditional["If-None-Match"] = cached.headers["etag"]
                if cached.headers.get("last-modified"):
                    conditional["If-Modified-Since"] = cached.headers["last-modified"]
            for attempt in range(self.limits.request_attempts):
                try:
                    await self.pace(url)
                    self.metrics["requests"] += 1
                    resp = await before_deadline(client.get(url, headers=conditional or None), self._deadline(timeout))
                    self._record_fetch(url, str(resp.url), resp.status_code, resp.headers,
                                       [str(item.url) for item in resp.history])
                    if resp.status_code == 304:
                        validated = (self._response_cache.revalidate_304(url, session, request_headers, resp.headers)
                                     if self._response_cache and cached else None)
                        if validated:
                            text = httpx.Response(200, content=validated.body,
                                                  headers=validated.headers).text
                            return text
                        # An unmatched 304 has no usable representation; retry without validators.
                        conditional = {}
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
                            if self._response_cache:
                                self._response_cache.store(url, session, request_headers, resp.headers, resp.content)
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
        if getattr(self, "on_progress", None):
            self.on_progress({"type": "progress", "stage": "discovery", "current": 0, "total": 0,
                              "message": "HTTP evidence unavailable; trying browser fallback"})
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
                    request = response.request
                    try:
                        response_data = json.loads(body)
                    except (ValueError, UnicodeDecodeError):
                        return
                    try:
                        post_data = request.post_data
                    except Exception:
                        post_data = None
                    try:
                        request_headers_observed = await request.all_headers()
                    except Exception:
                        request_headers_observed = request.headers
                    try:
                        request_body = json.loads(post_data) if post_data else None
                    except (ValueError, TypeError):
                        request_body = post_data
                    trusted_read_only = False
                    if request.method.upper() == "POST":
                        try:
                            from src.handlers import registry
                            handler = registry.find_handler(url)
                            trusted_read_only = bool(handler and handler.is_read_only_api_request(
                                request.method, response.url, _safe_network_headers(request_headers_observed),
                                request_body))
                        except Exception:
                            trusted_read_only = False
                    self.network_responses.append({
                        "url": response.url,
                        "data": response_data,
                        "method": request.method,
                        "headers": _safe_network_headers(request_headers_observed),
                        "post_data": request_body,
                        "source_page_url": url,
                        "trusted_read_only": trusted_read_only,
                    })
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
        if self._catalog_browser:
            await self._catalog_browser.close()
            self._catalog_browser = None
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
