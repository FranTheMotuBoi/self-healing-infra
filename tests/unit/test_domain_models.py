"""
tests/unit/test_domain_models.py
Tests for domain models, config validation, and business logic.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from src.models.domain import (
    AppConfig, EndpointConfig, ThresholdConfig, HealingConfig,
    AlertingConfig, HealthStatus, HealAction, CircuitState,
    CheckResult, HealResult, HealthReport, HealReport,
)


# ── EndpointConfig ────────────────────────────────────────────────

class TestEndpointConfig:

    def test_valid_http_endpoint(self):
        ep = EndpointConfig(name="test", url="http://example.com/health")
        assert ep.name == "test"
        assert ep.expected_status == 200
        assert ep.enabled is True
        assert ep.method == "GET"

    def test_valid_https_endpoint(self):
        ep = EndpointConfig(name="test", url="https://example.com/health")
        assert ep.url.startswith("https://")

    def test_invalid_url_no_scheme(self):
        with pytest.raises(ValidationError) as exc:
            EndpointConfig(name="test", url="example.com/health")
        assert "http://" in str(exc.value)

    def test_invalid_method(self):
        with pytest.raises(ValidationError):
            EndpointConfig(name="test", url="http://example.com", method="DELETE")

    def test_timeout_bounds(self):
        with pytest.raises(ValidationError):
            EndpointConfig(name="test", url="http://example.com", timeout_seconds=0)
        with pytest.raises(ValidationError):
            EndpointConfig(name="test", url="http://example.com", timeout_seconds=61)

    def test_method_normalised_to_uppercase(self):
        ep = EndpointConfig(name="test", url="http://example.com", method="get")
        assert ep.method == "GET"


# ── ThresholdConfig ───────────────────────────────────────────────

class TestThresholdConfig:

    def test_defaults_are_sensible(self):
        t = ThresholdConfig()
        assert t.response_time_warn_ms < t.response_time_critical_ms
        assert t.consecutive_failures_before_heal >= 1

    def test_warn_must_be_less_than_critical(self):
        with pytest.raises(ValidationError):
            ThresholdConfig(
                response_time_warn_ms=5000,
                response_time_critical_ms=2000,
            )

    def test_equal_warn_and_critical_invalid(self):
        with pytest.raises(ValidationError):
            ThresholdConfig(
                response_time_warn_ms=3000,
                response_time_critical_ms=3000,
            )


# ── AppConfig ─────────────────────────────────────────────────────

class TestAppConfig:

    def _base(self, **kwargs):
        return {
            "project_name": "test-project",
            "environment": "dev",
            **kwargs,
        }

    def test_valid_minimal_config(self):
        cfg = AppConfig(**self._base())
        assert cfg.project_name == "test-project"
        assert cfg.environment == "dev"
        assert cfg.endpoints == []

    def test_invalid_environment(self):
        with pytest.raises(ValidationError):
            AppConfig(**self._base(environment="production"))

    def test_prod_requires_alerting(self):
        with pytest.raises(ValidationError) as exc:
            AppConfig(**self._base(
                environment="prod",
                alerting={"alert_on_unhealthy": True},  # No SNS or Slack
            ))
        assert "alert channel" in str(exc.value).lower()

    def test_prod_dry_run_blocked(self):
        with pytest.raises(ValidationError):
            AppConfig(**self._base(
                environment="prod",
                alerting={"sns_topic_arn": "arn:aws:sns:us-east-1:123:test"},
                healing={"dry_run": True},
            ))

    def test_prod_with_alerting_valid(self):
        cfg = AppConfig(**self._base(
            environment="prod",
            alerting={"sns_topic_arn": "arn:aws:sns:us-east-1:123:test"},
            healing={"dry_run": False},
        ))
        assert cfg.environment == "prod"

    def test_endpoints_list(self):
        cfg = AppConfig(**self._base(endpoints=[
            {"name": "api", "url": "http://api.example.com/health"},
            {"name": "web", "url": "https://web.example.com/", "method": "HEAD"},
        ]))
        assert len(cfg.endpoints) == 2
        assert cfg.endpoints[1].method == "HEAD"


# ── CheckResult ───────────────────────────────────────────────────

class TestCheckResult:

    def test_healthy_check(self):
        r = CheckResult("api", HealthStatus.HEALTHY, 45.2)
        assert r.is_healthy is True
        assert r.severity.value == "low"

    def test_unhealthy_severity_high(self):
        r = CheckResult("api", HealthStatus.UNHEALTHY, 0,
                        consecutive_failures=1)
        assert r.severity.value == "high"

    def test_unhealthy_severity_critical_after_3_failures(self):
        r = CheckResult("api", HealthStatus.UNHEALTHY, 0,
                        consecutive_failures=3)
        assert r.severity.value == "critical"

    def test_degraded_severity_medium(self):
        r = CheckResult("api", HealthStatus.DEGRADED, 6000)
        assert r.severity.value == "medium"

    def test_with_consecutive_failures_immutable(self):
        r = CheckResult("api", HealthStatus.UNHEALTHY, 0)
        r2 = r.with_consecutive_failures(3)
        assert r.consecutive_failures == 0  # Original unchanged
        assert r2.consecutive_failures == 3

    def test_to_dict_contains_severity(self):
        r = CheckResult("api", HealthStatus.UNHEALTHY, 0, consecutive_failures=5)
        d = r.to_dict()
        assert "severity" in d
        assert d["severity"] == "critical"


# ── HealthReport ──────────────────────────────────────────────────

class TestHealthReport:

    def _make(self, statuses: list[HealthStatus]) -> HealthReport:
        checks = [CheckResult(f"check-{i}", s, 100.0)
                  for i, s in enumerate(statuses)]
        overall = (
            HealthStatus.UNHEALTHY if any(s == HealthStatus.UNHEALTHY for s in statuses)
            else HealthStatus.DEGRADED if any(s == HealthStatus.DEGRADED for s in statuses)
            else HealthStatus.HEALTHY
        )
        return HealthReport(
            environment="test", overall_status=overall,
            timestamp="2024-01-01T00:00:00Z", checks=checks,
        )

    def test_summary_counts_correct(self):
        r = self._make([
            HealthStatus.HEALTHY, HealthStatus.HEALTHY,
            HealthStatus.DEGRADED, HealthStatus.UNHEALTHY,
        ])
        assert r.summary["healthy"] == 2
        assert r.summary["degraded"] == 1
        assert r.summary["unhealthy"] == 1

    def test_failing_checks_property(self):
        r = self._make([HealthStatus.HEALTHY, HealthStatus.UNHEALTHY, HealthStatus.DEGRADED])
        assert len(r.failing_checks) == 2

    def test_actionable_failures_filters_by_consecutive(self):
        checks = [
            CheckResult("ok", HealthStatus.HEALTHY, 100),
            CheckResult("bad", HealthStatus.UNHEALTHY, 0, consecutive_failures=2),
            CheckResult("transient", HealthStatus.UNHEALTHY, 0, consecutive_failures=0),
        ]
        r = HealthReport(environment="test", overall_status=HealthStatus.UNHEALTHY,
                         timestamp="2024-01-01T00:00:00Z", checks=checks)
        actionable = r.actionable_failures
        assert len(actionable) == 1
        assert actionable[0].name == "bad"

    def test_correlation_id_auto_generated(self):
        r = self._make([HealthStatus.HEALTHY])
        assert len(r.correlation_id) == 36  # UUID format


# ── HealResult ────────────────────────────────────────────────────

class TestHealResult:

    def test_successful_heal(self):
        r = HealResult(HealAction.RESTART_SERVICE, True, "i-abc123",
                       duration_ms=1230.5)
        assert r.success is True
        assert r.duration_ms == 1230.5
        d = r.to_dict()
        assert d["action"] == "restart_service"

    def test_failed_heal_has_error(self):
        r = HealResult(HealAction.REPLACE_INSTANCE, False, "i-abc123",
                       error="Permission denied")
        assert r.success is False
        assert "Permission denied" in r.to_dict()["error"]


# ── HealReport ────────────────────────────────────────────────────

class TestHealReport:

    def test_overall_success_all_pass(self):
        actions = [
            HealResult(HealAction.RESTART_SERVICE, True, "i-1"),
            HealResult(HealAction.SCALE_UP_ASG, True, "asg-1"),
        ]
        r = HealReport("INC-001", "corr-001", "dev", "2024-01-01T00:00:00Z",
                       "unhealthy", actions, overall_success=True)
        assert r.overall_success is True

    def test_to_dict_serializable(self):
        import json
        actions = [HealResult(HealAction.RESTART_SERVICE, True, "i-1")]
        r = HealReport("INC-001", "corr-001", "dev", "2024-01-01T00:00:00Z",
                       "unhealthy", actions, overall_success=True, duration_seconds=3.14)
        d = r.to_dict()
        # Should be JSON-serializable
        json.dumps(d)
        assert d["incident_id"] == "INC-001"
        assert d["duration_seconds"] == 3.14


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
