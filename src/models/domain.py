"""
src/models/domain.py

Single source of truth for all domain types.
Both health_checker and healer import from here — no duplication.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Enums ─────────────────────────────────────────────────────────

class HealthStatus(str, Enum):
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN   = "unknown"

class Severity(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"

class HealAction(str, Enum):
    RESTART_SERVICE  = "restart_service"
    REPLACE_INSTANCE = "replace_instance"
    SCALE_UP_ASG     = "scale_up_asg"
    RDS_FAILOVER     = "rds_failover"
    NO_ACTION        = "no_action"
    DRY_RUN          = "dry_run"
    CIRCUIT_OPEN     = "circuit_open"
    COOLDOWN_ACTIVE  = "cooldown_active"

class CircuitState(str, Enum):
    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"


# ── Value Objects (immutable dataclasses) ─────────────────────────

@dataclass(frozen=True)
class CheckResult:
    name: str
    status: HealthStatus
    response_time_ms: float
    timestamp: str        = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    details: dict         = field(default_factory=dict)
    error: str | None     = None
    attempt: int          = 1
    consecutive_failures: int = 0

    @property
    def is_healthy(self) -> bool:
        return self.status == HealthStatus.HEALTHY

    @property
    def severity(self) -> Severity:
        if self.status == HealthStatus.UNHEALTHY:
            return Severity.CRITICAL if self.consecutive_failures >= 3 else Severity.HIGH
        if self.status == HealthStatus.DEGRADED:
            return Severity.MEDIUM
        return Severity.LOW

    def with_consecutive_failures(self, count: int) -> "CheckResult":
        """Return a new CheckResult with updated consecutive_failures."""
        return CheckResult(
            name=self.name, status=self.status, response_time_ms=self.response_time_ms,
            timestamp=self.timestamp, details=self.details, error=self.error,
            attempt=self.attempt, consecutive_failures=count,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name, "status": self.status, "response_time_ms": self.response_time_ms,
            "timestamp": self.timestamp, "details": self.details, "error": self.error,
            "attempt": self.attempt, "consecutive_failures": self.consecutive_failures,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class HealResult:
    action: HealAction
    success: bool
    target: str
    timestamp: str        = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    details: dict         = field(default_factory=dict)
    error: str | None     = None
    duration_ms: float    = 0.0

    def to_dict(self) -> dict:
        return {
            "action": self.action, "success": self.success, "target": self.target,
            "timestamp": self.timestamp, "details": self.details,
            "error": self.error, "duration_ms": self.duration_ms,
        }


@dataclass
class HealthReport:
    environment: str
    overall_status: HealthStatus
    timestamp: str
    checks: list[CheckResult]
    correlation_id: str   = field(default_factory=lambda: str(uuid.uuid4()))
    summary: dict         = field(default_factory=dict)
    duration_ms: float    = 0.0

    def __post_init__(self):
        self.summary = {s.value: sum(1 for c in self.checks if c.status == s) for s in HealthStatus}

    @property
    def failing_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.is_healthy]

    @property
    def actionable_failures(self) -> list[CheckResult]:
        """Failures that have passed the consecutive-failure threshold."""
        return [c for c in self.failing_checks if c.consecutive_failures > 0]


@dataclass
class HealReport:
    incident_id: str
    correlation_id: str
    environment: str
    timestamp: str
    trigger_reason: str
    actions_taken: list[HealResult]
    overall_success: bool
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "incident_id": self.incident_id, "correlation_id": self.correlation_id,
            "environment": self.environment, "timestamp": self.timestamp,
            "trigger_reason": self.trigger_reason,
            "actions_taken": [a.to_dict() for a in self.actions_taken],
            "overall_success": self.overall_success, "duration_seconds": self.duration_seconds,
        }


# ── Pydantic Config Models ────────────────────────────────────────

class EndpointConfig(BaseModel):
    name: str
    url: str
    method: str                          = "GET"
    expected_status: int                 = 200
    expected_body_contains: str | None   = None
    timeout_seconds: int                 = Field(default=10, ge=1, le=60)
    verify_ssl: bool                     = True
    headers: dict[str, str]              = Field(default_factory=dict)
    enabled: bool                        = True
    tags: list[str]                      = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def url_must_have_scheme(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"URL must start with http:// or https://: {v}")
        return v

    @field_validator("method")
    @classmethod
    def method_must_be_valid(cls, v: str) -> str:
        allowed = {"GET", "HEAD", "POST"}
        if v.upper() not in allowed:
            raise ValueError(f"method must be one of {allowed}")
        return v.upper()


class ThresholdConfig(BaseModel):
    response_time_warn_ms: int           = Field(default=2000, ge=100)
    response_time_critical_ms: int       = Field(default=5000, ge=500)
    healthy_host_minimum: int            = Field(default=2, ge=1)
    consecutive_failures_before_heal: int = Field(default=2, ge=1)
    heal_cooldown_minutes: int           = Field(default=5, ge=1)
    max_heals_per_hour: int              = Field(default=10, ge=1)

    @model_validator(mode="after")
    def warn_less_than_critical(self) -> "ThresholdConfig":
        if self.response_time_warn_ms >= self.response_time_critical_ms:
            raise ValueError("response_time_warn_ms must be < response_time_critical_ms")
        return self


class HealingConfig(BaseModel):
    dry_run: bool                        = False
    restart_service_name: str            = "app"
    scale_up_increment: int              = Field(default=1, ge=1)
    max_restart_attempts: int            = Field(default=2, ge=1)
    enable_rds_failover: bool            = True
    enable_instance_replacement: bool    = True
    enable_scale_up: bool                = True
    idempotency_table: str | None        = None


class AlertingConfig(BaseModel):
    slack_webhook_ssm_path: str | None   = None
    pagerduty_key_ssm_path: str | None   = None
    sns_topic_arn: str | None            = None
    alert_on_degraded: bool              = True
    alert_on_unhealthy: bool             = True
    send_resolve_alert: bool             = True
    suppress_duplicate_minutes: int      = Field(default=30, ge=0)


class LogConfig(BaseModel):
    level: str                           = "INFO"
    s3_bucket: str | None                = None
    s3_prefix: str                       = "logs"
    cloudwatch_log_group: str | None     = None
    json_format: bool                    = True

    @field_validator("level")
    @classmethod
    def valid_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"level must be one of {allowed}")
        return v.upper()


class AppConfig(BaseModel):
    project_name: str
    environment: str
    aws_region: str                      = "us-east-1"
    endpoints: list[EndpointConfig]      = Field(default_factory=list)
    asg_names: list[str]                 = Field(default_factory=list)
    target_group_arns: list[str]         = Field(default_factory=list)
    rds_identifiers: list[str]           = Field(default_factory=list)
    thresholds: ThresholdConfig          = Field(default_factory=ThresholdConfig)
    healing: HealingConfig               = Field(default_factory=HealingConfig)
    alerting: AlertingConfig             = Field(default_factory=AlertingConfig)
    logging: LogConfig                   = Field(default_factory=LogConfig)

    @field_validator("environment")
    @classmethod
    def valid_env(cls, v: str) -> str:
        allowed = {"local", "dev", "staging", "prod"}
        if v not in allowed:
            raise ValueError(f"environment must be one of {allowed}")
        return v

    @model_validator(mode="after")
    def prod_must_have_alerting(self) -> "AppConfig":
        if self.environment == "prod":
            if not self.alerting.sns_topic_arn and not self.alerting.slack_webhook_ssm_path:
                raise ValueError("Production requires at least one alert channel")
            if self.healing.dry_run:
                raise ValueError("dry_run must be false in production")
        return self

    @classmethod
    def from_yaml(cls, path: str) -> "AppConfig":
        import os, re
        import yaml

        with open(path) as f:
            raw = yaml.safe_load(f)

        # Substitute ${ENV_VAR} patterns
        def sub(obj: Any) -> Any:
            if isinstance(obj, str):
                return re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), obj)
            if isinstance(obj, dict):
                return {k: sub(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [sub(i) for i in obj]
            return obj

        return cls.model_validate(sub(raw))

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Minimal config from Lambda environment variables."""
        endpoints = []
        if alb := os.environ.get("ALB_DNS_NAME"):
            endpoints.append(EndpointConfig(name="alb-health", url=f"http://{alb}/health"))

        import os
        return cls.model_validate({
            "project_name": os.environ["PROJECT_NAME"],
            "environment":  os.environ["ENVIRONMENT"],
            "aws_region":   os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            "endpoints":    [e.model_dump() for e in endpoints],
            "asg_names":    [n for n in [os.environ.get("ASG_NAME")] if n],
            "healing":      {"dry_run": os.environ.get("DRY_RUN", "false").lower() == "true",
                             "idempotency_table": os.environ.get("IDEMPOTENCY_TABLE")},
            "alerting":     {"sns_topic_arn": os.environ.get("SNS_TOPIC_ARN"),
                             "slack_webhook_ssm_path": os.environ.get("SLACK_WEBHOOK_SSM")},
            "logging":      {"s3_bucket": os.environ.get("LOGS_BUCKET"),
                             "level": os.environ.get("LOG_LEVEL", "INFO")},
        })
