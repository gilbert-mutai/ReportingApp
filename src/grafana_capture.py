"""
Grafana dashboard screenshot capture using Playwright (headless Chromium).

Two capture modes:
  1. render_api  — uses Grafana's /render endpoint (requires grafana-image-renderer plugin)
  2. playwright  — logs in via the web UI and takes a full-page screenshot (no plugins needed)

The module auto-detects which mode to use at startup.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlencode

import requests
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .config import GrafanaConfig

logger = logging.getLogger(__name__)

_RENDER_PROBE_PATH = "/api/plugins/grafana-image-renderer/settings"


def _render_api_available(cfg: GrafanaConfig) -> bool:
    """Check whether the Grafana image renderer plugin is installed."""
    try:
        headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
        resp = requests.get(
            f"{cfg.url}{_RENDER_PROBE_PATH}",
            headers=headers,
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


class GrafanaCapture:
    """
    Captures Grafana dashboard screenshots.

    Usage:
        async with GrafanaCapture(cfg) as cap:
            paths = await cap.capture_dashboards(["uid1", "uid2"], output_dir, period)
    """

    def __init__(self, cfg: GrafanaConfig):
        self._cfg = cfg
        self._use_render_api: Optional[bool] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._pw = None

    async def __aenter__(self) -> "GrafanaCapture":
        self._use_render_api = _render_api_available(self._cfg)
        if self._use_render_api:
            logger.info("Grafana image renderer plugin detected — using render API")
        else:
            logger.info("Image renderer not available — using Playwright headless capture")
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )
            self._context = await self._browser.new_context(
                viewport={"width": self._cfg.width, "height": self._cfg.height},
                ignore_https_errors=True,
            )
            if self._cfg.token:
                # Inject token as a cookie Grafana recognises (grafana_session approach
                # doesn't work for tokens — we log in with credentials instead).
                pass
            await self._playwright_login()
        return self

    async def __aexit__(self, *_) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def capture_dashboards(
        self,
        dashboard_uids: List[str],
        output_dir: Path,
        period: str = "weekly",
    ) -> List[Path]:
        """Capture screenshots for all provided dashboard UIDs."""
        if not dashboard_uids:
            logger.warning("No dashboard UIDs configured — skipping capture")
            return []

        output_dir.mkdir(parents=True, exist_ok=True)
        results: List[Path] = []

        for uid in dashboard_uids:
            try:
                path = await self._capture_one(uid, output_dir, period)
                results.append(path)
                logger.info("Captured dashboard %s → %s", uid, path)
            except Exception:
                logger.exception("Failed to capture dashboard %s", uid)

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _capture_one(self, uid: str, output_dir: Path, period: str) -> Path:
        ts = int(time.time())
        filename = output_dir / f"dashboard_{uid}_{period}_{ts}.png"

        if self._use_render_api:
            self._render_via_api(uid, filename, period)
        else:
            await self._render_via_playwright(uid, filename, period)

        return filename

    def _time_range(self, period: str):
        if period == "monthly":
            return "now-30d", "now"
        return "now-7d", "now"  # weekly default

    def _render_via_api(self, uid: str, output_path: Path, period: str) -> None:
        """Use Grafana render API to get PNG (requires image renderer plugin)."""
        from_ts, to_ts = self._time_range(period)
        params = {
            "orgId": self._cfg.org_id,
            "from": from_ts,
            "to": to_ts,
            "width": self._cfg.width,
            "height": self._cfg.height,
            "tz": "UTC",
            "theme": self._cfg.theme,
            "timeout": self._cfg.render_timeout,
        }
        url = f"{self._cfg.url}/render/d/{uid}?{urlencode(params)}"
        headers = {"Authorization": f"Bearer {self._cfg.token}"} if self._cfg.token else {}

        logger.debug("Render API request: %s", url)
        resp = requests.get(
            url,
            headers=headers,
            timeout=self._cfg.render_timeout + 10,
            stream=True,
        )
        resp.raise_for_status()

        with open(output_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=8192):
                fh.write(chunk)

    async def _playwright_login(self) -> None:
        """Log into Grafana using username / password via the web UI."""
        login_url = f"{self._cfg.url}/login"
        page: Page = await self._context.new_page()
        try:
            logger.debug("Navigating to Grafana login: %s", login_url)
            await page.goto(login_url, wait_until="networkidle", timeout=30_000)

            # Fill login form
            await page.fill('input[name="user"]', self._cfg.user)
            await page.fill('input[name="password"]', self._cfg.password)
            await page.click('button[type="submit"]')

            # Wait for redirect away from /login
            await page.wait_for_url(lambda url: "/login" not in url, timeout=20_000)
            logger.info("Grafana login successful as %s", self._cfg.user)
        except Exception:
            logger.exception("Grafana login failed")
            raise
        finally:
            await page.close()

    async def _render_via_playwright(
        self, uid: str, output_path: Path, period: str
    ) -> None:
        """Screenshot a Grafana dashboard using Playwright (already logged in)."""
        from_ts, to_ts = self._time_range(period)
        params = {
            "orgId": self._cfg.org_id,
            "from": from_ts,
            "to": to_ts,
            "theme": self._cfg.theme,
            "kiosk": "",          # hides navbar for cleaner screenshot
        }
        url = f"{self._cfg.url}/d/{uid}?{urlencode(params)}"

        page: Page = await self._context.new_page()
        try:
            logger.debug("Navigating to dashboard: %s", url)
            await page.goto(url, wait_until="networkidle", timeout=60_000)

            # Dismiss any Grafana modals / announcement popups (press Escape)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)

            # Close "What's new" or similar overlay buttons if present
            for selector in [
                'button[aria-label="Close"]',
                '[data-testid="whats-new-button"]',
                'button:has-text("Got it")',
                'button:has-text("Dismiss")',
            ]:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click()
                        await page.wait_for_timeout(300)
                except Exception:
                    pass

            # Detect "dashboard not found" — fail early with a clear message
            page_title = await page.title()
            current_url = page.url
            if "not-found" in current_url or "Not Found" in page_title:
                raise RuntimeError(
                    f"Dashboard '{uid}' not found. Check that the reporter user "
                    "has Viewer access to the folder containing this dashboard "
                    "(Grafana → Administration → Folders → <folder> → Permissions)."
                )

            # Wait for panels to finish loading (Grafana renders async)
            await page.wait_for_timeout(self._cfg.screenshot_delay)

            # Wait for loading spinners to disappear
            try:
                await page.wait_for_selector(
                    ".panel-loading", state="detached", timeout=30_000
                )
            except Exception:
                pass  # No loading indicators present — that's fine

            # Expand to full content height
            content_height = await page.evaluate(
                "() => document.body.scrollHeight"
            )
            await page.set_viewport_size(
                {"width": self._cfg.width, "height": max(content_height, self._cfg.height)}
            )

            await page.screenshot(
                path=str(output_path),
                full_page=True,
                type="png",
            )
        finally:
            await page.close()
