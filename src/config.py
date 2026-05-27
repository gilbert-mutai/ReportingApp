"""
Configuration loader — reads from .env file and environment variables.
All settings can be overridden at runtime via env vars.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: int) -> int:
    raw = os.getenv(key, "")
    if raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            raise EnvironmentError(f"Environment variable '{key}' must be an integer, got: {raw!r}")
    return default


def _bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _list(key: str, default: str = "") -> List[str]:
    raw = os.getenv(key, default)
    return [v.strip() for v in raw.split(",") if v.strip()]


@dataclass
class GrafanaConfig:
    url: str
    token: str
    user: str
    password: str
    org_id: int
    theme: str              # light | dark
    width: int
    height: int
    render_timeout: int     # seconds
    screenshot_delay: int   # ms to wait after page load


@dataclass
class PrometheusConfig:
    url: str
    timeout: int            # request timeout seconds
    step: str               # default query_range step e.g. "5m"


@dataclass
class EmailConfig:
    """Microsoft Graph API (OAuth2 client credentials) email config."""
    sender: str             # EMAIL_HOST_USER — the mailbox to send from
    client_id: str          # Azure app registration client ID
    client_secret: str      # Azure app registration client secret
    tenant_id: str          # Azure AD tenant ID
    timeout: int            # HTTP request timeout seconds


@dataclass
class ReportConfig:
    period: str             # weekly | monthly | custom
    output_dir: Path
    dashboard_uids: List[str]
    title: str
    company_name: str
    include_html: bool
    notes: str


@dataclass
class Config:
    grafana: GrafanaConfig
    prometheus: PrometheusConfig
    email: EmailConfig
    report: ReportConfig
    email_to: List[str]
    log_level: str

    @classmethod
    def load(cls, env_file: str = ".env") -> "Config":
        env_path = Path(env_file)
        if env_path.exists():
            load_dotenv(env_path, override=False)
            logger.debug("Loaded env from %s", env_path)
        else:
            logger.debug("No .env file found at %s; using environment only", env_path)

        grafana = GrafanaConfig(
            url=_require("GRAFANA_URL").rstrip("/"),
            token=_optional("GRAFANA_TOKEN"),
            user=_optional("GRAFANA_USER", "admin"),
            password=_optional("GRAFANA_PASSWORD", "admin"),
            org_id=_int("GRAFANA_ORG_ID", 1),
            theme=_optional("GRAFANA_THEME", "light"),
            width=_int("GRAFANA_WIDTH", 1920),
            height=_int("GRAFANA_HEIGHT", 1080),
            render_timeout=_int("GRAFANA_RENDER_TIMEOUT", 60),
            screenshot_delay=_int("GRAFANA_SCREENSHOT_DELAY_MS", 3000),
        )

        prometheus = PrometheusConfig(
            url=_require("PROMETHEUS_URL").rstrip("/"),
            timeout=_int("PROMETHEUS_TIMEOUT", 30),
            step=_optional("PROMETHEUS_STEP", "5m"),
        )

        email = EmailConfig(
            sender=_require("EMAIL_HOST_USER"),
            client_id=_require("OAUTH2_CLIENT_ID"),
            client_secret=_require("OAUTH2_CLIENT_SECRET"),
            tenant_id=_require("OAUTH2_TENANT_ID"),
            timeout=_int("EMAIL_TIMEOUT", 30),
        )

        output_dir = Path(_optional("REPORT_OUTPUT_DIR", "/reports"))
        output_dir.mkdir(parents=True, exist_ok=True)

        report = ReportConfig(
            period=_optional("REPORT_PERIOD", "weekly").lower(),
            output_dir=output_dir,
            dashboard_uids=_list("DASHBOARD_UIDS"),
            title=_optional("REPORT_TITLE", "Server Monitoring Report"),
            company_name=_optional("COMPANY_NAME", ""),
            include_html=_bool("INCLUDE_HTML_REPORT", False),
            notes=_optional("REPORT_NOTES", ""),
        )

        if not report.dashboard_uids:
            uid = _optional("DASHBOARD_UID")
            if uid:
                report.dashboard_uids = [uid]

        email_to = _list("EMAIL_TO")
        if not email_to:
            raise EnvironmentError("EMAIL_TO must contain at least one recipient address.")

        return cls(
            grafana=grafana,
            prometheus=prometheus,
            email=email,
            report=report,
            email_to=email_to,
            log_level=_optional("LOG_LEVEL", "INFO").upper(),
        )
