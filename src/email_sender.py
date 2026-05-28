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
    generated_str = generated_at.strftime("%d %B %Y, %H:%M")

    errors_block = ""
    if metrics_errors:
        items = "".join(f"<li>{e}</li>" for e in metrics_errors)
        errors_block = f"""
        <tr><td style="padding:0 32px 16px;">
          <p style="margin:0 0 6px;font-size:13px;color:#856404;background:#fff3cd;
                    border-left:4px solid #ffc107;padding:10px 14px;border-radius:4px;">
            <strong>Note:</strong> Some metrics could not be collected:
          </p>
          <ul style="margin:4px 0 0 20px;font-size:13px;color:#856404;">{items}</ul>
        </td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/></head>
<body style="margin:0;padding:0;background:#f4f6fb;font-family:'Segoe UI',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6fb;padding:32px 0;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0"
           style="background:#ffffff;border-radius:10px;overflow:hidden;
                  box-shadow:0 2px 8px rgba(0,0,0,0.08);">

      <!-- Header bar -->
      <tr>
        <td style="background:linear-gradient(135deg,#4361ee,#7209b7);
                   padding:28px 32px;text-align:center;">
          <p style="margin:0;font-size:11px;color:rgba(255,255,255,0.7);
                    text-transform:uppercase;letter-spacing:0.08em;">
            {company + " &mdash; " if company else ""}{label} Report
          </p>
          <h1 style="margin:6px 0 0;font-size:20px;font-weight:700;color:#ffffff;">
            Server Monitoring Report
          </h1>
          <p style="margin:6px 0 0;font-size:12px;color:rgba(255,255,255,0.65);">
            Generated {generated_str}
          </p>
        </td>
      </tr>

      <!-- Greeting -->
      <tr><td style="padding:28px 32px 8px;">
        <p style="margin:0;font-size:15px;color:#1a1a2e;">Hello Team,</p>
      </td></tr>

      <!-- Body text -->
      <tr><td style="padding:12px 32px 8px;">
        <p style="margin:0;font-size:14px;color:#333;line-height:1.6;">
          Please find the automated monitoring report attached.
        </p>
      </td></tr>

      <!-- Bullet list -->
      <tr><td style="padding:8px 32px 16px;">
        <p style="margin:0 0 10px;font-size:14px;color:#333;">This report includes:</p>
        <table cellpadding="0" cellspacing="0">
          <tr><td style="padding:4px 0;font-size:14px;color:#333;">
            <span style="color:#4361ee;font-weight:700;margin-right:8px;">&#8226;</span>
            Grafana Dashboards
          </td></tr>
          <tr><td style="padding:4px 0;font-size:14px;color:#333;">
            <span style="color:#4361ee;font-weight:700;margin-right:8px;">&#8226;</span>
            CPU, memory, disk and network metrics summary
          </td></tr>
          <tr><td style="padding:4px 0;font-size:14px;color:#333;">
            <span style="color:#4361ee;font-weight:700;margin-right:8px;">&#8226;</span>
            Server uptime statistics
          </td></tr>
        </table>
      </td></tr>

      {errors_block}

      <!-- Divider -->
      <tr><td style="padding:0 32px;">
        <hr style="border:none;border-top:1px solid #e8ecff;margin:0;"/>
      </td></tr>

      <!-- Sign-off -->
      <tr><td style="padding:20px 32px 8px;">
        <p style="margin:0;font-size:14px;color:#333;">Regards,</p>
        <p style="margin:4px 0 0;font-size:14px;font-weight:600;color:#1a1a2e;">
          Grafana Reporting Service
        </p>
      </td></tr>

      <!-- Footer -->
      <tr><td style="padding:12px 32px 28px;">
        <p style="margin:0;font-size:11px;color:#aaa;font-style:italic;">
          This is an automated message. Do not reply.
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


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
                "contentType": "HTML",
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
