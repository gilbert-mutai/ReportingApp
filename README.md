# Grafana Reporting Service

Automated server monitoring report generator for **Grafana OSS + Prometheus**.

Captures dashboard screenshots, queries Prometheus metrics, and emails a professional PDF report on a configurable schedule — all without Grafana Enterprise.

---

## Features

- Grafana dashboard screenshots via Playwright (no plugins required)
- Automatic fallback to Grafana Image Renderer API if the plugin is installed
- Prometheus metric queries: CPU, memory, disk, network, uptime
- Professional PDF report via WeasyPrint (HTML → PDF)
- Optional HTML report output
- SMTP email delivery (Office365, Gmail, self-hosted)
- systemd timer scheduling (weekly + monthly)
- Docker-based — portable across projects
- CLI flags for manual / one-off runs
- Idempotent: skips re-generating a report that already exists for the same timestamp

---

## Prerequisites

- Docker + Docker Compose v2
- A host running systemd (for scheduled runs)
- Grafana OSS reachable from the Docker container
- Prometheus reachable from the Docker container
- An SMTP account (Office365 / Gmail / self-hosted)

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/yourorg/grafana-reporting-service.git
cd grafana-reporting-service

cp .env.example .env
$EDITOR .env           # fill in GRAFANA_URL, PROMETHEUS_URL, SMTP_*, etc.
```

### 2. Build the Docker image

```bash
docker compose build
```

### 3. Run a manual test

```bash
# Weekly report — generates PDF and sends email
docker compose run --rm reporter --period weekly

# Monthly report — no email, for testing
docker compose run --rm reporter --period monthly --no-email

# Validate config only (no network calls, no files written)
docker compose run --rm reporter --dry-run

# Screenshots only, no email
docker compose run --rm reporter --period weekly --no-email
```

Generated reports are saved to `./reports/` on the host.

---

## Configuration Reference

All settings are read from the `.env` file (or environment variables).

| Variable | Required | Default | Description |
|---|---|---|---|
| `GRAFANA_URL` | ✅ | — | Grafana base URL |
| `GRAFANA_TOKEN` | — | — | Service account token (API calls) |
| `GRAFANA_USER` | — | `admin` | Username for Playwright login |
| `GRAFANA_PASSWORD` | — | `admin` | Password for Playwright login |
| `GRAFANA_ORG_ID` | — | `1` | Grafana organisation ID |
| `GRAFANA_THEME` | — | `light` | Dashboard theme: `light` or `dark` |
| `GRAFANA_WIDTH` | — | `1920` | Screenshot width (px) |
| `GRAFANA_HEIGHT` | — | `1080` | Screenshot height (px) |
| `GRAFANA_SCREENSHOT_DELAY_MS` | — | `3000` | Wait after page load (ms) |
| `DASHBOARD_UIDS` | — | — | Comma-separated dashboard UIDs |
| `PROMETHEUS_URL` | ✅ | — | Prometheus base URL |
| `PROMETHEUS_TIMEOUT` | — | `30` | Request timeout (seconds) |
| `SMTP_HOST` | ✅ | — | SMTP server hostname |
| `SMTP_PORT` | ✅ | `587` | SMTP port |
| `SMTP_USER` | ✅ | — | SMTP username |
| `SMTP_PASSWORD` | ✅ | — | SMTP password |
| `SMTP_TLS` | — | `true` | Use STARTTLS |
| `SMTP_SSL` | — | `false` | Use SSL/TLS (port 465) |
| `EMAIL_FROM` | — | `SMTP_USER` | Sender address |
| `EMAIL_TO` | ✅ | — | Comma-separated recipients |
| `REPORT_PERIOD` | — | `weekly` | `weekly` or `monthly` |
| `REPORT_TITLE` | — | `Server Monitoring Report` | Report title |
| `COMPANY_NAME` | — | — | Company name on cover page |
| `REPORT_OUTPUT_DIR` | — | `/reports` | PDF output directory |
| `INCLUDE_HTML_REPORT` | — | `false` | Keep HTML alongside PDF |
| `REPORT_NOTES` | — | — | Free-text notes in report |
| `LOG_LEVEL` | — | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### Finding a Dashboard UID

Open the dashboard in Grafana. The UID is in the URL:

```
http://grafana:3000/d/<UID>/my-dashboard-name
                       ^^^
```

---

## systemd Scheduling

### Install

```bash
# Copy the project to the host
sudo mkdir -p /opt/grafana-reporting-service
sudo cp -r . /opt/grafana-reporting-service/
sudo cp .env /opt/grafana-reporting-service/.env

# Install systemd units
sudo cp systemd/grafana-report@.service  /etc/systemd/system/
sudo cp systemd/grafana-report-weekly.timer  /etc/systemd/system/
sudo cp systemd/grafana-report-monthly.timer /etc/systemd/system/

sudo systemctl daemon-reload

# Enable and start the timers
sudo systemctl enable --now grafana-report-weekly.timer
sudo systemctl enable --now grafana-report-monthly.timer
```

### Verify

```bash
# Show next scheduled run times
systemctl list-timers "grafana-report*"

# Watch status
systemctl status grafana-report-weekly.timer
systemctl status grafana-report@weekly.service

# Follow logs in real-time
journalctl -fu grafana-reporter-weekly

# View last run logs
journalctl -u grafana-report@weekly --no-pager -n 100
```

### Manual trigger

```bash
sudo systemctl start grafana-report@weekly.service
sudo systemctl start grafana-report@monthly.service
```

### Schedule summary

| Timer | Runs | Notes |
|---|---|---|
| `grafana-report-weekly.timer` | Every Monday 08:00 | Sends weekly report |
| `grafana-report-monthly.timer` | 1st of each month 08:00 | Sends monthly report |

Both timers have `Persistent=true` — if the system was offline at the scheduled time, the job runs within 5 minutes of the next boot.

---

## Gmail / Office365 SMTP notes

### Gmail
```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=you@gmail.com
SMTP_PASSWORD=<app-password>   # not your account password
```
Generate an [App Password](https://myaccount.google.com/apppasswords) if 2FA is enabled.

### Office 365
```
SMTP_HOST=smtp.office365.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=reports@yourorg.com
SMTP_PASSWORD=<password>
```

---

## Prometheus Queries Used

| Metric | PromQL |
|---|---|
| CPU usage | `100 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100` |
| Memory usage | `100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)` |
| Disk usage (root) | `100 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes * 100` |
| Uptime | Derived from `up` metric scrape continuity |
| Load avg | `node_load1`, `node_load15` |
| Network RX | `rate(node_network_receive_bytes_total{device!="lo"}[5m])` |
| Network TX | `rate(node_network_transmit_bytes_total{device!="lo"}[5m])` |

Requires [node_exporter](https://github.com/prometheus/node_exporter) to be running on target servers.

---

## Multi-client / Multi-project Reuse

Deploy separate instances with different `.env` files:

```bash
# Client A
GRAFANA_URL=http://client-a-grafana:3000 \
EMAIL_TO=ops@client-a.com \
docker compose run --rm reporter --period weekly

# Client B
--env-file /opt/client-b/.env
docker compose run --rm reporter --period weekly
```

Or use systemd template instances with different `EnvironmentFile` paths via drop-in overrides.

---

## Troubleshooting

### Screenshot is blank / login fails
- Check `GRAFANA_USER` / `GRAFANA_PASSWORD` are correct
- Increase `GRAFANA_SCREENSHOT_DELAY_MS` (try 5000)
- Set `LOG_LEVEL=DEBUG` and inspect Playwright output
- Ensure the container can reach `GRAFANA_URL` (try `curl` from within the container)

### PDF is empty / WeasyPrint error
- Verify WeasyPrint system libs are installed (`libcairo2`, `libpango*`)
- Test with `--no-screenshot` to rule out image issues

### SMTP authentication fails
- Confirm app password (not account password) for Gmail
- Check `SMTP_PORT` / `SMTP_TLS` / `SMTP_SSL` combination
- Test manually: `openssl s_client -connect smtp.office365.com:587 -starttls smtp`

### Timer not firing
```bash
journalctl -u grafana-report-weekly.timer --no-pager
systemctl list-timers --all | grep grafana
```

---

## Project Structure

```
.
├── src/
│   ├── __init__.py
│   ├── main.py              # CLI entry point & pipeline orchestrator
│   ├── config.py            # Config loader (.env + env vars)
│   ├── grafana_capture.py   # Playwright / render API screenshot capture
│   ├── prometheus_client.py # Prometheus HTTP API client
│   ├── pdf_generator.py     # HTML → PDF via WeasyPrint
│   ├── html_generator.py    # Jinja2 HTML report renderer
│   └── email_sender.py      # SMTP email delivery
├── templates/
│   └── report.html.j2       # Report HTML template
├── systemd/
│   ├── grafana-report@.service
│   ├── grafana-report-weekly.timer
│   └── grafana-report-monthly.timer
├── reports/                 # Output directory (gitignored)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```
