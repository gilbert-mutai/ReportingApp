"""
Prometheus metrics collection module.

Queries the Prometheus HTTP API (/api/v1/query and /api/v1/query_range)
to fetch server health metrics for the report period.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import PrometheusConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class MetricSample:
    timestamp: float
    value: float


@dataclass
class MetricSeries:
    name: str
    labels: Dict[str, str]
    samples: List[MetricSample] = field(default_factory=list)

    @property
    def values(self) -> List[float]:
        return [s.value for s in self.samples]

    @property
    def avg(self) -> Optional[float]:
        vals = self.values
        return sum(vals) / len(vals) if vals else None

    @property
    def max_val(self) -> Optional[float]:
        return max(self.values) if self.values else None

    @property
    def min_val(self) -> Optional[float]:
        return min(self.values) if self.values else None


@dataclass
class ServerMetrics:
    cpu_avg: float = 0.0
    cpu_max: float = 0.0
    memory_avg: float = 0.0
    memory_max: float = 0.0
    disk_usage: float = 0.0
    uptime_pct: float = 100.0
    load_avg_1m: float = 0.0
    load_avg_15m: float = 0.0
    network_rx_bytes: float = 0.0
    network_tx_bytes: float = 0.0
    period_label: str = "weekly"
    errors: List[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        logger.warning("Metrics collection warning: %s", msg)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PrometheusClient:
    """Thin, resilient wrapper around the Prometheus HTTP API."""

    def __init__(self, cfg: PrometheusConfig):
        self._cfg = cfg
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------
    # Core query methods
    # ------------------------------------------------------------------

    def query(self, promql: str, at: Optional[float] = None) -> List[Dict[str, Any]]:
        """Execute an instant query. Returns a list of result dicts."""
        params: Dict[str, Any] = {"query": promql}
        if at is not None:
            params["time"] = at

        url = f"{self._cfg.url}/api/v1/query"
        try:
            resp = self._session.get(url, params=params, timeout=self._cfg.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Prometheus query failed (%s): %s", promql, exc)
            return []

        data = resp.json()
        if data.get("status") != "success":
            logger.error("Prometheus error for %s: %s", promql, data.get("error"))
            return []

        return data["data"]["result"]

    def query_range(
        self,
        promql: str,
        start: float,
        end: float,
        step: Optional[str] = None,
    ) -> List[MetricSeries]:
        """Execute a range query. Returns MetricSeries objects."""
        step = step or self._cfg.step
        params = {
            "query": promql,
            "start": start,
            "end": end,
            "step": step,
        }
        url = f"{self._cfg.url}/api/v1/query_range"
        try:
            resp = self._session.get(url, params=params, timeout=self._cfg.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Prometheus range query failed (%s): %s", promql, exc)
            return []

        data = resp.json()
        if data.get("status") != "success":
            logger.error("Prometheus range error for %s: %s", promql, data.get("error"))
            return []

        series_list: List[MetricSeries] = []
        for result in data["data"]["result"]:
            series = MetricSeries(
                name=promql,
                labels=result.get("metric", {}),
                samples=[
                    MetricSample(timestamp=float(ts), value=float(val))
                    for ts, val in result.get("values", [])
                ],
            )
            series_list.append(series)
        return series_list

    def scalar(self, promql: str, at: Optional[float] = None) -> Optional[float]:
        """Return the first scalar result of an instant query, or None."""
        results = self.query(promql, at=at)
        if not results:
            return None
        try:
            return float(results[0]["value"][1])
        except (IndexError, KeyError, ValueError):
            return None

    # ------------------------------------------------------------------
    # High-level metrics collection
    # ------------------------------------------------------------------

    def collect_server_metrics(self, period: str = "weekly") -> ServerMetrics:
        """
        Gather all metrics for a report period.
        Attempts best-effort: individual failures populate ServerMetrics.errors
        without stopping the whole collection.
        """
        now = time.time()
        duration_seconds = 30 * 86400 if period == "monthly" else 7 * 86400
        start = now - duration_seconds

        metrics = ServerMetrics(period_label=period)

        self._collect_cpu(metrics, start, now)
        self._collect_memory(metrics, start, now)
        self._collect_disk(metrics)
        self._collect_uptime(metrics, start, now, duration_seconds)
        self._collect_load(metrics)
        self._collect_network(metrics, start, now)

        return metrics

    # ------------------------------------------------------------------
    # Individual metric collectors
    # ------------------------------------------------------------------

    def _collect_cpu(self, m: ServerMetrics, start: float, end: float) -> None:
        # Node exporter: CPU usage across all modes except idle
        promql = (
            '100 - (avg by(instance) '
            '(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
        )
        series = self.query_range(promql, start, end)
        if series:
            all_vals = [v for s in series for v in s.values]
            m.cpu_avg = round(sum(all_vals) / len(all_vals), 2) if all_vals else 0.0
            m.cpu_max = round(max(all_vals), 2) if all_vals else 0.0
        else:
            m.add_error("CPU metrics unavailable")

    def _collect_memory(self, m: ServerMetrics, start: float, end: float) -> None:
        promql = (
            "100 * (1 - "
            "(node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))"
        )
        series = self.query_range(promql, start, end)
        if series:
            all_vals = [v for s in series for v in s.values]
            m.memory_avg = round(sum(all_vals) / len(all_vals), 2) if all_vals else 0.0
            m.memory_max = round(max(all_vals), 2) if all_vals else 0.0
        else:
            m.add_error("Memory metrics unavailable")

    def _collect_disk(self, m: ServerMetrics) -> None:
        promql = (
            '100 - (node_filesystem_avail_bytes{mountpoint="/"} '
            '/ node_filesystem_size_bytes{mountpoint="/"} * 100)'
        )
        val = self.scalar(promql)
        if val is not None:
            m.disk_usage = round(val, 2)
        else:
            m.add_error("Disk usage metrics unavailable")

    def _collect_uptime(
        self,
        m: ServerMetrics,
        start: float,
        end: float,
        duration_seconds: float,
    ) -> None:
        # Count how many scrape samples show the node was up
        up_series = self.query_range("up", start, end, step="1m")
        if up_series:
            all_samples = [s for series in up_series for s in series.samples]
            if all_samples:
                up_count = sum(1 for s in all_samples if s.value == 1.0)
                m.uptime_pct = round(up_count / len(all_samples) * 100, 3)
                return
        # Fallback: check node_time_seconds
        boot_val = self.scalar("node_boot_time_seconds")
        if boot_val is not None:
            uptime_s = time.time() - boot_val
            # If the server booted within the period, uptime% < 100
            m.uptime_pct = round(min(uptime_s / duration_seconds * 100, 100), 3)
        else:
            m.add_error("Uptime metrics unavailable")

    def _collect_load(self, m: ServerMetrics) -> None:
        val1 = self.scalar("node_load1")
        val15 = self.scalar("node_load15")
        if val1 is not None:
            m.load_avg_1m = round(val1, 2)
        else:
            m.add_error("Load average (1m) unavailable")
        if val15 is not None:
            m.load_avg_15m = round(val15, 2)

    def _collect_network(self, m: ServerMetrics, start: float, end: float) -> None:
        rx_promql = 'sum(rate(node_network_receive_bytes_total{device!="lo"}[5m]))'
        tx_promql = 'sum(rate(node_network_transmit_bytes_total{device!="lo"}[5m]))'

        rx_series = self.query_range(rx_promql, start, end)
        tx_series = self.query_range(tx_promql, start, end)

        if rx_series:
            vals = [v for s in rx_series for v in s.values]
            m.network_rx_bytes = round(sum(vals) / len(vals), 2) if vals else 0.0
        else:
            m.add_error("Network RX metrics unavailable")

        if tx_series:
            vals = [v for s in tx_series for v in s.values]
            m.network_tx_bytes = round(sum(vals) / len(vals), 2) if vals else 0.0
        else:
            m.add_error("Network TX metrics unavailable")
