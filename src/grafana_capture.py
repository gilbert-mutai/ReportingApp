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

    async def _dismiss_modals(self, page: Page) -> None:
        """Close any Grafana modal dialogs or announcement popups."""
        # Press Escape up to 3 times — closes most overlay dialogs
        for _ in range(3):
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)

        # Explicitly click close/dismiss buttons for known Grafana modals
        close_selectors = [
            # Generic close buttons
            'button[aria-label="Close"]',
            'button[aria-label="close"]',
            # "What's new" / Grafana Assistant announcement dialog
            '[data-testid="whats-new-dialog"] button',
            'div[role="dialog"] button[aria-label="Close"]',
            # Buttons with text labels
            'button:has-text("Got it")',
            'button:has-text("Dismiss")',
            'button:has-text("Maybe later")',
            'button:has-text("No thanks")',
            'button:has-text("Skip")',
        ]
        for selector in close_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=800):
                    await btn.click()
                    await page.wait_for_timeout(400)
                    logger.debug("Dismissed modal via selector: %s", selector)
            except Exception:
                pass

        # Final fallback: hide any remaining overlay via JavaScript
        await page.evaluate("""
            () => {
                // Remove modal backdrops and dialogs
                document.querySelectorAll(
                    '[role="dialog"], .modal-backdrop, [class*="backdrop"]'
                ).forEach(el => el.remove());
            }
        """)
        await page.wait_for_timeout(300)

    async def _render_via_playwright(
        self, uid: str, output_path: Path, period: str
    ) -> None:
        """Screenshot a Grafana dashboard using Playwright (already logged in)."""
        from_ts, to_ts = self._time_range(period)

        # Build base params — kiosk must be appended without a value (?kiosk not ?kiosk=)
        # urlencode would produce kiosk= which Grafana ignores, so we append manually
        params = urlencode({
            "orgId": self._cfg.org_id,
            "from": from_ts,
            "to": to_ts,
            "theme": self._cfg.theme,
        })
        url = f"{self._cfg.url}/d/{uid}?{params}&kiosk"

        page: Page = await self._context.new_page()
        try:
            logger.debug("Navigating to dashboard: %s", url)
            await page.goto(url, wait_until="networkidle", timeout=60_000)

            # Detect "dashboard not found" or wrong page — fail with a clear message
            current_url = page.url
            if f"/d/{uid}" not in current_url:
                raise RuntimeError(
                    f"Dashboard '{uid}' did not load — landed on: {current_url}\n"
                    "Most likely cause: the Grafana user does not have Viewer "
                    "permission on the folder containing this dashboard."
                )

            # Dismiss all modals aggressively before screenshotting
            await self._dismiss_modals(page)

            # Wait for panels to finish loading (Grafana renders async)
            await page.wait_for_timeout(self._cfg.screenshot_delay)

            # Wait for loading spinners to disappear
            try:
                await page.wait_for_selector(
                    ".panel-loading", state="detached", timeout=30_000
                )
            except Exception:
                pass  # No loading indicators present — that's fine

            # Best-effort: hide known nav chrome by element type / role / class fragments.
            # Grafana 10 uses emotion-generated class names so this may not catch everything;
            # the crop step below handles whatever remains.
            await page.evaluate("""
                () => {
                    document.querySelectorAll(
                        'nav, header, aside, ' +
                        '[role="navigation"], [role="banner"], ' +
                        '[class*="sidemenu"], [class*="SideMenu"], [class*="sidebar"], ' +
                        '[class*="navbar"], [class*="NavBar"], [class*="nav-bar"], ' +
                        '[class*="topnav"], [class*="MegaMenu"], ' +
                        '[class*="page-toolbar"], [class*="toolbar"], ' +
                        '[data-testid="nav-menu-portal"]'
                    ).forEach(el => {
                        el.style.setProperty('display', 'none', 'important');
                    });
                    document.querySelectorAll('body *').forEach(el => {
                        const s = window.getComputedStyle(el);
                        const r = el.getBoundingClientRect();
                        if ((s.position === 'fixed' || s.position === 'sticky') &&
                                r.top < 120 && r.height > 0 && r.height < 120) {
                            el.style.setProperty('display', 'none', 'important');
                        }
                    });
                }
            """)
            await page.wait_for_timeout(300)

            # First viewport expansion — makes Grafana render off-screen panels
            content_height = await page.evaluate("() => document.body.scrollHeight")
            page_height = max(content_height, self._cfg.height)
            await page.set_viewport_size({"width": self._cfg.width, "height": page_height})

            # Wait for any newly-visible panels to finish rendering, then
            # re-measure — Grafana lazy-loads panels as the viewport grows so
            # the first scrollHeight is always smaller than the final one.
            await page.wait_for_timeout(1500)
            content_height = await page.evaluate("() => document.body.scrollHeight")
            if content_height > page_height:
                page_height = content_height
                await page.set_viewport_size({"width": self._cfg.width, "height": page_height})
                await page.wait_for_timeout(800)

            # Final height after all panels are visible
            page_height = await page.evaluate("() => document.body.scrollHeight")

            # Find the dashboard grid origin to crop out residual nav chrome.
            crop = await page.evaluate("""
                () => {
                    for (const sel of [
                        '[class*="react-grid-layout"]',
                        '[class*="panel-container"]',
                        '[class*="dashboard-row"]',
                        '[class*="dashboard-content"]',
                    ]) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const r = el.getBoundingClientRect();
                            const x = Math.max(0, Math.floor(r.left) - 8);
                            const y = Math.max(0, Math.floor(r.top) - 8);
                            if (x > 20 || y > 20) return { x, y };
                        }
                    }
                    return null;
                }
            """)

            if crop:
                # full_page=True captures all content; clip removes the nav strip
                await page.screenshot(
                    path=str(output_path),
                    full_page=True,
                    type="png",
                    clip={
                        "x": crop["x"],
                        "y": crop["y"],
                        "width": self._cfg.width - crop["x"],
                        "height": page_height - crop["y"],
                    },
                )
            else:
                await page.screenshot(
                    path=str(output_path),
                    full_page=True,
                    type="png",
                )
        finally:
            await page.close()
