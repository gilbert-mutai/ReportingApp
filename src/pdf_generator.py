"""
PDF report generator.

Converts the rendered HTML report to PDF using WeasyPrint.
WeasyPrint handles embedded base64 images and CSS styling natively.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import ReportConfig
from .html_generator import HtmlGenerator
from .prometheus_client import ServerMetrics

logger = logging.getLogger(__name__)


class PdfGenerator:
    """
    Produces a PDF report by:
      1. Rendering the HTML template (via HtmlGenerator)
      2. Converting HTML → PDF with WeasyPrint
    """

    def __init__(self, cfg: ReportConfig, templates_dir: Path):
        self._cfg = cfg
        self._html_gen = HtmlGenerator(cfg, templates_dir)

    def generate(
        self,
        metrics: ServerMetrics,
        screenshot_paths: List[Path],
        output_dir: Path,
        generated_at: Optional[datetime] = None,
    ) -> Path:
        if generated_at is None:
            generated_at = datetime.now(timezone.utc)

        ts = generated_at.strftime("%Y%m%d_%H%M")
        stem = f"report_{metrics.period_label}_{ts}"

        html_path = output_dir / f"{stem}.html"
        pdf_path = output_dir / f"{stem}.pdf"

        # Render HTML first
        self._html_gen.render(
            metrics=metrics,
            screenshot_paths=screenshot_paths,
            output_path=html_path,
            generated_at=generated_at,
        )

        # Convert HTML → PDF
        logger.info("Converting HTML → PDF: %s", pdf_path)
        try:
            from weasyprint import HTML, CSS  # type: ignore
            from weasyprint.text.fonts import FontConfiguration  # type: ignore

            font_config = FontConfiguration()
            HTML(filename=str(html_path)).write_pdf(
                str(pdf_path),
                font_config=font_config,
            )
        except ImportError:
            logger.error(
                "WeasyPrint is not installed. Install it with: pip install weasyprint"
            )
            raise
        except Exception:
            logger.exception("PDF generation failed for %s", html_path)
            raise

        logger.info("PDF report generated: %s (%s bytes)", pdf_path, pdf_path.stat().st_size)

        # Remove intermediate HTML unless explicitly requested
        if not self._cfg.include_html:
            try:
                html_path.unlink()
            except OSError:
                pass

        return pdf_path
