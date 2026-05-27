"""
Grafana Reporting Service — entry point.

Usage:
  python -m src.main [--period weekly|monthly] [--no-email] [--no-screenshot]
  python -m src.main --help
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Argument parsing (before heavy imports so --help is fast)
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate and email a Grafana/Prometheus monitoring report."
    )
    parser.add_argument(
        "--period",
        choices=["weekly", "monthly"],
        default=None,
        help="Override REPORT_PERIOD from env (default: weekly)",
    )
    parser.add_argument(
        "--env",
        default=".env",
        metavar="FILE",
        help="Path to .env file (default: .env)",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Generate report but do NOT send email",
    )
    parser.add_argument(
        "--no-screenshot",
        action="store_true",
        help="Skip Grafana screenshots (metrics-only report)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help="Override REPORT_OUTPUT_DIR",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config and exit without generating anything",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    """
    Orchestrates the full reporting pipeline:
      1. Load config
      2. Capture Grafana screenshots
      3. Query Prometheus metrics
      4. Generate PDF report
      5. Send email
    Returns exit code (0 = success).
    """
    from .config import Config
    from .email_sender import EmailSender
    from .grafana_capture import GrafanaCapture
    from .pdf_generator import PdfGenerator
    from .prometheus_client import PrometheusClient

    # --- Config -----------------------------------------------------------
    try:
        cfg = Config.load(args.env)
    except EnvironmentError as exc:
        logging.critical("Configuration error: %s", exc)
        return 2

    # Apply CLI overrides
    if args.period:
        cfg.report.period = args.period
    if args.output_dir:
        cfg.report.output_dir = Path(args.output_dir)
        cfg.report.output_dir.mkdir(parents=True, exist_ok=True)

    # Configure root logger after config is loaded
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    logger = logging.getLogger(__name__)
    logger.info(
        "Starting %s report | period=%s | output=%s",
        cfg.report.period,
        cfg.report.period,
        cfg.report.output_dir,
    )

    if args.dry_run:
        logger.info("Dry-run mode — configuration is valid. Exiting.")
        return 0

    # --- Idempotency check ------------------------------------------------
    tz = ZoneInfo(cfg.report.timezone)
    generated_at = datetime.now(tz)
    ts_str = generated_at.strftime("%Y%m%d_%H%M")
    safe_title = re.sub(r"[^\w\s-]", "", cfg.report.title).strip().replace(" ", "_")
    stem = f"{safe_title}_{ts_str}"
    pdf_candidate = cfg.report.output_dir / f"{stem}.pdf"

    if pdf_candidate.exists():
        logger.info(
            "Report %s already exists — skipping (idempotency). "
            "Delete the file to force regeneration.",
            pdf_candidate,
        )
        return 0

    # --- Grafana screenshots ----------------------------------------------
    screenshot_paths = []
    if not args.no_screenshot and cfg.report.dashboard_uids:
        try:
            async with GrafanaCapture(cfg.grafana) as cap:
                screenshot_paths = await cap.capture_dashboards(
                    cfg.report.dashboard_uids,
                    cfg.report.output_dir / "screenshots",
                    cfg.report.period,
                )
        except Exception:
            logger.exception(
                "Screenshot capture failed — continuing with metrics-only report"
            )
    else:
        if args.no_screenshot:
            logger.info("Screenshot capture disabled by --no-screenshot flag")
        elif not cfg.report.dashboard_uids:
            logger.warning("No DASHBOARD_UIDS configured — skipping screenshots")

    # --- Prometheus metrics -----------------------------------------------
    prom = PrometheusClient(cfg.prometheus)
    try:
        metrics = prom.collect_server_metrics(cfg.report.period)
    except Exception:
        logger.exception("Prometheus metrics collection failed")
        prom.close()
        return 1
    finally:
        prom.close()

    logger.info(
        "Metrics: CPU avg=%.1f%% max=%.1f%% | Mem avg=%.1f%% | "
        "Disk=%.1f%% | Uptime=%.2f%%",
        metrics.cpu_avg,
        metrics.cpu_max,
        metrics.memory_avg,
        metrics.disk_usage,
        metrics.uptime_pct,
    )

    # --- PDF generation ---------------------------------------------------
    templates_dir = Path(__file__).parent.parent / "templates"
    pdf_gen = PdfGenerator(cfg.report, templates_dir)
    try:
        pdf_path = pdf_gen.generate(
            metrics=metrics,
            screenshot_paths=screenshot_paths,
            output_dir=cfg.report.output_dir,
            generated_at=generated_at,
        )
    except Exception:
        logger.exception("PDF generation failed")
        return 1

    # --- HTML report (optional) -------------------------------------------
    if cfg.report.include_html:
        from .html_generator import HtmlGenerator

        html_gen = HtmlGenerator(cfg.report, templates_dir)
        html_path = cfg.report.output_dir / f"{stem}.html"
        try:
            html_gen.render(
                metrics=metrics,
                screenshot_paths=screenshot_paths,
                output_path=html_path,
                generated_at=generated_at,
            )
        except Exception:
            logger.exception("HTML report generation failed — continuing")

    # --- Email delivery ---------------------------------------------------
    if not args.no_email:
        sender = EmailSender(cfg.email)
        try:
            sender.send_report(
                report_path=pdf_path,
                recipients=cfg.email_to,
                period=cfg.report.period,
                metrics_errors=metrics.errors,
                company=cfg.report.company_name,
                generated_at=generated_at,
            )
        except Exception:
            logger.exception("Email delivery failed")
            # Report was generated successfully; don't fail the exit code
            logger.warning("Report saved at %s — manual delivery required", pdf_path)
            return 1

    logger.info("Report pipeline complete. Output: %s", pdf_path)
    return 0


def main() -> None:
    args = _parse_args()

    # Minimal logging before config is loaded
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )

    try:
        exit_code = asyncio.run(run(args))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Interrupted by user")
        exit_code = 130
    except Exception:
        logging.getLogger(__name__).exception("Unhandled exception in main pipeline")
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
