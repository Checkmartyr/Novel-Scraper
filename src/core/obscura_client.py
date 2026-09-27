import asyncio
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, Browser, Page, Playwright
from bs4 import BeautifulSoup

import logging
from src.core.binary_manager import ensure_obscura
from src.config import NAVIGATION_TIMEOUT, WAIT_UNTIL

logger = logging.getLogger("core.obscura_client")


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

    def __init__(self, bin_path: Optional[Path] = None, cdp_port: int = 9222):
        self.bin_path = bin_path or ensure_obscura()
        self.cdp_port = cdp_port
        self._server_process: Optional[asyncio.subprocess.Process] = None
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._browser_loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = asyncio.Lock()
        self._cached_cookies: Dict[str, str] = {}
        self._cached_user_agent: Optional[str] = None

    async def start_cdp_server(self, stealth: bool = True, allow_private_network: bool = True) -> None:
        """Start obscura serve in background if not already running."""
        # If port is already open, server is already running and ready
        if await _is_port_open("127.0.0.1", self.cdp_port):
            return

        if self._server_process and self._server_process.returncode is None:
            return

        cmd = [
            str(self.bin_path),
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
                self._server_process.kill()
            finally:
                self._server_process = None

    async def _ensure_browser(self, stealth: bool = True, allow_private_network: bool = True) -> Browser:
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

            self._playwright = await async_playwright().start()
            ws_endpoint = f"ws://127.0.0.1:{self.cdp_port}"
            self._browser = await self._playwright.chromium.connect_over_cdp(ws_endpoint)
            self._browser_loop = loop
            return self._browser

    @asynccontextmanager
    async def get_playwright_page(self, stealth: bool = True, allow_private_network: bool = True):
        """Context manager to obtain a Playwright page connected to Obscura CDP."""
        browser = await self._ensure_browser(stealth=stealth, allow_private_network=allow_private_network)
        context = await browser.new_context()
        page: Page = await context.new_page()
        try:
            yield page
        finally:
            try:
                await page.close()
            except Exception:
                pass
            try:
                await context.close()
            except Exception:
                pass

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

    async def fetch_html(
        self,
        url: str,
        stealth: bool = True,
        timeout: int = NAVIGATION_TIMEOUT,
        allow_private_network: bool = True,
        wait_until: str = WAIT_UNTIL,
    ) -> str:
        """Fetch page HTML using Playwright over Obscura CDP, with automatic Cloudflare fallback."""
        # Check platform handler registry for direct API or synthesized HTML bypass
        from src.handlers import registry
        handler = registry.find_handler(url)
        if handler:
            try:
                result = await handler.handle(url, obscura_client=self)
                if result:
                    return result
            except Exception as e:
                logger.warning(f"Platform handler error for {url}: {e}. Falling back to browser.")

        # Fast HTTP attempt (avoids browser overhead & ad context crashes on static novel pages)
        import httpx
        http_headers = {
            "User-Agent": self.get_user_agent() or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.8,th;q=0.7",
        }
        cookies = self.get_cookies()
        try:
            async with httpx.AsyncClient(headers=http_headers, cookies=cookies, follow_redirects=True, timeout=12.0) as http_client:
                for attempt in range(3):
                    resp = await http_client.get(url)
                    if resp.status_code == 429:
                        wait = 2.0 * (attempt + 1)
                        logger.warning(f"HTTP 429 Rate limited on {url}. Backing off {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status_code == 200:
                        text_html = resp.text
                        if len(text_html) > 800 and not is_cloudflare_challenge(text_html):
                            return text_html
                    break
        except Exception as e:
            logger.debug(f"Fast HTTP fetch bypassed for {url} ({e}). Falling back to browser.")

        content: Optional[str] = None
        async with self.get_playwright_page(stealth=stealth, allow_private_network=allow_private_network) as page:
            # Block aggressive ad networks that crash frame execution contexts
            try:
                ad_re = re.compile(
                    r"\b(doubleclick|google-analytics|googletagservices|adnxs|adsystem|taboola|outbrain|disqus|criteo|pubmatic)\b",
                    re.I
                )
                await page.route(ad_re, lambda route: route.abort())
            except Exception:
                pass

            try:
                await page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
                content = await page.content()
            except Exception as e:
                # If networkidle timed out or frame context was detached by ads
                try:
                    content = await page.evaluate("() => document.documentElement.outerHTML")
                except Exception:
                    try:
                        content = await page.content()
                    except Exception:
                        pass
                if not content or (len(content) <= 300 and not is_cloudflare_challenge(content)):
                    if "evaluate" in str(e) or "TargetClosedError" in str(e):
                        return await self._fetch_with_chrome_fallback(url, timeout=timeout)
                    raise e

        if content and is_cloudflare_challenge(content):
            return await self._fetch_with_chrome_fallback(url, timeout=timeout)
            
        return content or ""

    async def fetch_markdown(
        self,
        url: str,
        stealth: bool = True,
        timeout: int = NAVIGATION_TIMEOUT,
        allow_private_network: bool = True,
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
        """Clean up Playwright browser, playwright context, and CDP server."""
        if self._browser:
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
