"""
src/metrics/publisher.py

Publishes custom CloudWatch metrics with proper dimensions, units, and
high-resolution (1-second) support. Batches up to 20 per API call.

Metrics namespace: {project}/HealthChecker and {project}/Healer
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

Unit = Literal["Count","Milliseconds","Seconds","Percent","Bytes","None"]


@dataclass
class Metric:
    name: str
    value: float
    unit: Unit          = "Count"
    dimensions: dict    = field(default_factory=dict)
    high_resolution: bool = False   # 1-second resolution (costs more)


class MetricsPublisher:
    """
    Thread-safe metrics publisher with local buffer.
    Call flush() to ship to CloudWatch — do this at end of Lambda handler.
    """

    def __init__(self, namespace: str, region: str, environment: str):
        self._namespace  = namespace
        self._region     = region
        self._environment = environment
        self._buffer: list[Metric] = []
        self._cw = None

    @property
    def cw(self):
        if not self._cw:
            self._cw = boto3.client("cloudwatch", region_name=self._region)
        return self._cw

    # ── Public API ────────────────────────────────────────────────

    def count(self, name: str, value: float = 1.0, **dims) -> None:
        self._add(Metric(name=name, value=value, unit="Count",
                         dimensions={"Environment": self._environment, **dims}))

    def timing(self, name: str, ms: float, **dims) -> None:
        self._add(Metric(name=name, value=round(ms, 2), unit="Milliseconds",
                         dimensions={"Environment": self._environment, **dims}))

    def gauge(self, name: str, value: float, **dims) -> None:
        self._add(Metric(name=name, value=value, unit="None",
                         dimensions={"Environment": self._environment, **dims}))

    def record_check(self, check_name: str, status: str, response_time_ms: float) -> None:
        self.count("CheckRun",      CheckName=check_name)
        self.timing("ResponseTime", response_time_ms, CheckName=check_name)
        self.count(f"Check.{status.capitalize()}", CheckName=check_name)

    def record_heal(self, action: str, success: bool, target: str) -> None:
        self.count("HealAttempt",   Action=action)
        self.count("HealSuccess" if success else "HealFailure", Action=action)

    def record_circuit_state(self, resource_id: str, state: str) -> None:
        self.gauge("CircuitBreakerOpen",
                   1.0 if state == "OPEN" else 0.0,
                   Resource=resource_id[:256])

    # ── Flush ─────────────────────────────────────────────────────

    def flush(self) -> int:
        """Ship buffered metrics to CloudWatch. Returns number of metrics sent."""
        if not self._buffer:
            return 0

        batch, self._buffer = self._buffer[:], []
        sent = 0

        for i in range(0, len(batch), 20):
            chunk = batch[i:i+20]
            metric_data = []
            for m in chunk:
                datum = {
                    "MetricName": m.name,
                    "Value":      m.value,
                    "Unit":       m.unit,
                    "Timestamp":  datetime.now(timezone.utc),
                    "Dimensions": [{"Name": k, "Value": str(v)[:256]}
                                   for k, v in m.dimensions.items()],
                }
                if m.high_resolution:
                    datum["StorageResolution"] = 1
                metric_data.append(datum)

            try:
                self.cw.put_metric_data(Namespace=self._namespace, MetricData=metric_data)
                sent += len(chunk)
            except ClientError as e:
                logger.warning(f"CloudWatch metrics flush failed: {e}")

        logger.debug(f"Flushed {sent} metrics to {self._namespace}")
        return sent

    def _add(self, metric: Metric) -> None:
        self._buffer.append(metric)
        # Auto-flush if buffer is getting large
        if len(self._buffer) >= 200:
            self.flush()


class NullMetricsPublisher:
    """No-op publisher for local/test environments."""
    def count(self, *a, **kw): pass
    def timing(self, *a, **kw): pass
    def gauge(self, *a, **kw): pass
    def record_check(self, *a, **kw): pass
    def record_heal(self, *a, **kw): pass
    def record_circuit_state(self, *a, **kw): pass
    def flush(self) -> int: return 0


def get_publisher(project: str, environment: str,
                  region: str = "us-east-1") -> MetricsPublisher | NullMetricsPublisher:
    if environment == "local":
        return NullMetricsPublisher()
    return MetricsPublisher(
        namespace=f"{project}/HealthChecker",
        region=region,
        environment=environment,
    )
