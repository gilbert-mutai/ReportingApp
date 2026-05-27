"""
Email delivery via Microsoft Graph API (OAuth2 client credentials).

Flow:
  1. Request an access token from Azure AD using client_id + client_secret
  2. POST the email (with PDF attachment) to the Graph sendMail endpoint
     on behalf of the configured sender mailbox

No SMTP, no basic auth — works with modern Microsoft 365 tenants where
basic auth has been disabled.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests

from .config import EmailConfig

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_SEND_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
_SCOPE = "https://graph.microsoft.com/.default"


def _period_label(period: str) -> str:
    return "Monthly" if period == "monthly" else "Weekly"


def _build_subject(period: str, generated_at: datetime) -> str:
    label = _period_label(period)
    date_str = (
        generated_at.strftime("%B %Y")
        if period == "monthly"
        else generated_at.strftime("%d %B %Y")
    )
    return f"{label} Server Monitoring Report — {date_str}"


def _build_body(
    period: str,
    metrics_errors: List[str],
    generated_at: datetime,
    company: str = "",
) -> str:
    label = _period_label(period)
    header = f"{company} — " if company else ""
    lines = [
        f"{header}{label} Server Monitoring Report",
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "Please find the automated monitoring report attached.",
        "",
        "This report includes:",
        "  • Grafana dashboard screenshots",
        "  • CPU, memory, disk and network metrics summary",
        "  • Server uptime statistics",
        "",
    ]
    if metrics_errors:
        lines += [
            "Note: Some metrics could not be collected:",
            *[f"  - {e}" for e in metrics_errors],
            "",
        ]
    lines += [
        "--",
        "Grafana Reporting Service",
        "This is an automated message. Do not reply.",
    ]
    return "\n".join(lines)


class EmailSender:
    def __init__(self, cfg: EmailConfig):
        self._cfg = cfg
        self._session = requests.Session()

    def _get_access_token(self) -> str:
        """Fetch an OAuth2 access token using client credentials grant."""
        url = _TOKEN_URL.format(tenant_id=self._cfg.tenant_id)
        data = {
            "grant_type": "client_credentials",
            "client_id": self._cfg.client_id,
            "client_secret": self._cfg.client_secret,
            "scope": _SCOPE,
        }
        logger.debug("Requesting access token from Azure AD (tenant: %s)", self._cfg.tenant_id)
        resp = self._session.post(url, data=data, timeout=self._cfg.timeout)

        if resp.status_code != 200:
            logger.error(
                "Token request failed %d: %s",
                resp.status_code,
                resp.text[:500],
            )
            resp.raise_for_status()

        token = resp.json().get("access_token")
        if not token:
            raise ValueError("Azure AD token response missing access_token field")

        logger.debug("Access token obtained successfully")
        return token

    def send_report(
        self,
        report_path: Path,
        recipients: List[str],
        period: str,
        metrics_errors: Optional[List[str]] = None,
        company: str = "",
        generated_at: Optional[datetime] = None,
    ) -> None:
        if generated_at is None:
            generated_at = datetime.now(timezone.utc)
        if metrics_errors is None:
            metrics_errors = []
        if not report_path.exists():
            raise FileNotFoundError(f"Report file not found: {report_path}")

        subject = _build_subject(period, generated_at)
        body = _build_body(period, metrics_errors, generated_at, company)

        # Read and base64-encode the PDF attachment
        pdf_b64 = base64.b64encode(report_path.read_bytes()).decode()

        # Build the Graph API message payload
        message = {
            "subject": subject,
            "body": {
                "contentType": "Text",
                "content": body,
            },
            "toRecipients": [
                {"emailAddress": {"address": addr}} for addr in recipients
            ],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": report_path.name,
                    "contentType": "application/pdf",
                    "contentBytes": pdf_b64,
                }
            ],
        }

        token = self._get_access_token()
        send_url = _SEND_URL.format(sender=self._cfg.sender)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        logger.info(
            "Sending '%s' to %d recipient(s) via Microsoft Graph API",
            subject,
            len(recipients),
        )

        resp = self._session.post(
            send_url,
            json={"message": message, "saveToSentItems": "true"},
            headers=headers,
            timeout=self._cfg.timeout,
        )

        if resp.status_code == 202:
            logger.info("Report successfully sent to: %s", ", ".join(recipients))
        else:
            logger.error(
                "Graph API sendMail failed %d: %s",
                resp.status_code,
                resp.text[:500],
            )
            resp.raise_for_status()

    def close(self) -> None:
        self._session.close()
