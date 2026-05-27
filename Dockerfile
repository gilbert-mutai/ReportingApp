# syntax=docker/dockerfile:1
# -----------------------------------------------------------------------
# Grafana Reporting Service
# Base: python:3.12-slim | Playwright Chromium + WeasyPrint
# -----------------------------------------------------------------------

FROM python:3.12-slim AS base

LABEL org.opencontainers.image.title="grafana-reporting-service"
LABEL org.opencontainers.image.description="Automated Grafana + Prometheus PDF report generator"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/pw-browsers

# -----------------------------------------------------------------------
# System dependencies
# -----------------------------------------------------------------------
# WeasyPrint: Cairo, Pango, GDK-PixBuf (font rendering, SVG, PNG)
# Playwright: Chromium runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # WeasyPrint / Cairo stack
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    # Fonts for PDF rendering
    fonts-liberation \
    fonts-dejavu-core \
    # Network / TLS
    ca-certificates \
    curl \
    # Chromium runtime (Playwright)
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libfreetype6 \
    libfontconfig1 \
 && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------
# Python dependencies
# -----------------------------------------------------------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's Chromium browser (pinned to what the package expects)
RUN playwright install chromium --with-deps 2>/dev/null || \
    playwright install chromium

# -----------------------------------------------------------------------
# Application code
# -----------------------------------------------------------------------
COPY src/       ./src/
COPY templates/ ./templates/

# Output directory (override with -v /host/reports:/reports)
RUN mkdir -p /reports

# -----------------------------------------------------------------------
# Runtime
# -----------------------------------------------------------------------
# Run as non-root for security
RUN groupadd -r reporter && useradd -r -g reporter reporter && \
    chown -R reporter:reporter /app /reports /pw-browsers 2>/dev/null || true

USER reporter

ENTRYPOINT ["python", "-m", "src.main"]
CMD ["--period", "weekly"]
