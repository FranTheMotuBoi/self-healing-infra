"""
src/config/manager.py

Centralised configuration with:
- Pydantic validation (fail fast on bad config)
- SSM Parameter Store + Secrets Manager integration
- Environment-specific overrides
- Config hot-reload without Lambda cold start
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


# ─── Config Models (Pydantic v2) ─────────────────────────────────

class EndpointConfig(BaseModel):
    name: str
    url: str
    expected_status: int = 200
    expected_body_contains: str | None = None
    timeout_seconds: int = 10
    verify_ssl: bool = True
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def url_must_have_scheme(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"URL must start with http:// or https://: {v}")
        return v


class ThresholdConfig(BaseModel):
    response_time_warn_ms: int = 2000
    response_time_critical_ms: int = 5000
    healthy_host_minimum: int = 2
    consecutive_failures_before_heal: int = 2   # Avoid flapping
    heal_cooldown_minutes: int = 5              # Don't re-heal within this window
    max_heals_per_hour: int = 10               # Circuit breaker


class HealingConfig(BaseModel):
    dry_run: bool = False
    restart_service_name: str = "app"
    scale_up_increment: int = 1
    max_restart_attempts: int = 2
    enable_rds_failover: bool = True
    enable_instance_replacement: bool = True
    enable_scale_up: bool = True


class AlertingConfig(BaseModel):
    slack_webhook_ssm_path: str | None = None
    pagerduty_key_ssm_path: str | None = None
    sns_topic_arn: str | None = None
    alert_on_degraded: bool = True
    alert_on_unhealthy: bool = True
    send_resolve_alert: bool = True
    suppress_duplicate_minutes: int = 30


class LoggingConfig(BaseModel):
    level: str = "INFO"
    s3_bucket: str | None = None
    s3_prefix: str = "health-checks/"
    cloudwatch_log_group: str | None = None
    include_request_body: bool = False   # PII concern in prod


class AppConfig(BaseModel):
    project_name: str
    environment: str
    aws_region: str = "us-east-1"
    endpoints: list[EndpointConfig] = Field(default_factory=list)
    asg_names: list[str] = Field(default_factory=list)
    target_group_arns: list[str] = Field(default_factory=list)
    rds_identifiers: list[str] = Field(default_factory=list)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    healing: HealingConfig = Field(default_factory=HealingConfig)
    alerting: AlertingConfig = Field(default_factory=AlertingConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @field_validator("environment")
    @classmethod
    def valid_environment(cls, v: str) -> str:
        if v not in ("local", "dev", "staging", "prod"):
            raise ValueError(f"environment must be local/dev/staging/prod, got: {v}")
        return v

    @model_validator(mode="after")
    def prod_must_have_alerting(self) -> "AppConfig":
        if self.environment == "prod":
            if not self.alerting.sns_topic_arn and not self.alerting.slack_webhook_ssm_path:
                raise ValueError("Production environment requires at least one alert channel configured")
        return self


# ─── Config Loader ────────────────────────────────────────────────

class ConfigManager:
    """
    Loads config from YAML + resolves secrets from AWS SSM/Secrets Manager.
    Uses LRU cache so Lambda warm invocations don't re-fetch SSM every time.
    """

    _instance: ConfigManager | None = None

    def __init__(self, region: str | None = None):
        self._region = region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        self._ssm: Any = None
        self._secrets: Any = None

    @classmethod
    def get_instance(cls) -> "ConfigManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def load_from_yaml(self, path: str) -> AppConfig:
        import yaml
        with open(path) as f:
            raw = yaml.safe_load(f)
        # Substitute env vars like ${VAR_NAME}
        raw = self._substitute_env_vars(raw)
        config = AppConfig.model_validate(raw)
        logger.info(f"Config loaded: {config.project_name} / {config.environment}")
        return config

    def load_from_env(self) -> AppConfig:
        """Load minimal config from environment variables (Lambda mode)."""
        endpoints = []
        alb_dns = os.environ.get("ALB_DNS_NAME")
        if alb_dns:
            endpoints.append(EndpointConfig(
                name="alb-health",
                url=f"http://{alb_dns}/health",
                expected_status=200,
                expected_body_contains="healthy",
            ))

        raw = {
            "project_name": os.environ["PROJECT_NAME"],
            "environment": os.environ["ENVIRONMENT"],
            "aws_region": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            "endpoints": [e.model_dump() for e in endpoints],
            "asg_names": [n for n in [os.environ.get("ASG_NAME")] if n],
            "healing": {"dry_run": os.environ.get("DRY_RUN", "false").lower() == "true"},
            "alerting": {
                "sns_topic_arn": os.environ.get("SNS_TOPIC_ARN"),
                "slack_webhook_ssm_path": os.environ.get("SLACK_WEBHOOK_SSM"),
            },
            "logging": {
                "s3_bucket": os.environ.get("LOGS_BUCKET"),
                "cloudwatch_log_group": os.environ.get("CW_LOG_GROUP"),
            },
        }
        return AppConfig.model_validate(raw)

    @lru_cache(maxsize=32)
    def get_secret(self, ssm_path: str) -> str | None:
        """Fetch a SecureString from SSM Parameter Store. Cached per Lambda container."""
        if not ssm_path:
            return None
        try:
            if self._ssm is None:
                self._ssm = boto3.client("ssm", region_name=self._region)
            resp = self._ssm.get_parameter(Name=ssm_path, WithDecryption=True)
            return resp["Parameter"]["Value"]
        except ClientError as e:
            logger.warning(f"SSM parameter not found: {ssm_path} — {e}")
            return None

    @lru_cache(maxsize=16)
    def get_secrets_manager_value(self, secret_arn: str) -> dict:
        """Fetch a JSON secret from Secrets Manager. Cached per Lambda container."""
        try:
            if self._secrets is None:
                self._secrets = boto3.client("secretsmanager", region_name=self._region)
            resp = self._secrets.get_secret_value(SecretId=secret_arn)
            return json.loads(resp["SecretString"])
        except ClientError as e:
            logger.error(f"Failed to fetch secret {secret_arn}: {e}")
            return {}

    def _substitute_env_vars(self, obj: Any) -> Any:
        """Recursively replace ${VAR} patterns with env var values."""
        import re
        if isinstance(obj, str):
            return re.sub(
                r"\$\{([^}]+)\}",
                lambda m: os.environ.get(m.group(1), m.group(0)),
                obj,
            )
        elif isinstance(obj, dict):
            return {k: self._substitute_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._substitute_env_vars(i) for i in obj]
        return obj
