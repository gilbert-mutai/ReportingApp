"""
HTML report generator.

Renders a Jinja2 template to produce a self-contained HTML report
embedding dashboard screenshots as base64 data-URIs.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import ReportConfig
from .prometheus_client import ServerMetrics

logger = logging.getLogger(__name__)


def _b64_image(path: Path) -> str:
    """Return a base64 data-URI for the given image file."""
    with open(path, "rb") as fh:
        data = base64.b64encode(fh.read()).decode()
    suffix = path.suffix.lower().lstrip(".")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(
        suffix, "image/png"
    )
    return f"data:{mime};base64,{data}"


def _bytes_to_human(nbytes: float) -> str:
    for unit in ["B/s", "KB/s", "MB/s", "GB/s"]:
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB/s"


def _bytes_to_gb(nbytes: float) -> str:
    """Format a byte count as GB or TB (for storage/RAM display)."""
    gb = nbytes / (1024 ** 3)
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    return f"{gb:.1f} GB"


class HtmlGenerator:
    def __init__(self, cfg: ReportConfig, templates_dir: Path):
        self._cfg = cfg
        env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(["html"]),
        )
        env.filters["b64image"] = lambda p: _b64_image(Path(p))
        env.filters["bytes_human"] = _bytes_to_human
        env.filters["bytes_gb"] = _bytes_to_gb
        self._template = env.get_template("report.html.j2")

    def render(
        self,
        metrics: ServerMetrics,
        screenshot_paths: List[Path],
        output_path: Path,
        generated_at: Optional[datetime] = None,
    ) -> Path:
        if generated_at is None:
            generated_at = datetime.now(timezone.utc)

        tz_name = generated_at.tzname() or "UTC"
        logo_b64 = _b64_image(Path(self._cfg.logo_path)) if self._cfg.logo_path and Path(self._cfg.logo_path).exists() else None

        context = {
            "title": self._cfg.title,
            "company": self._cfg.company_name,
            "period": self._cfg.period,
            "generated_at": generated_at.strftime(f"%Y-%m-%d %H:%M {tz_name}"),
            "metrics": metrics,
            "screenshots": [str(p) for p in screenshot_paths if p.exists()],
            "notes": self._cfg.notes,
            "logo_b64": logo_b64,
        }

        html = self._template.render(**context)
        output_path.write_text(html, encoding="utf-8")
        logger.info("HTML report written to %s", output_path)
        return output_path
