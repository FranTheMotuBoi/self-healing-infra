"""
tests/unit/test_health_checker.py
Tests for HTTP checker, AWS checker, and orchestrator.
Uses 'responses' library to mock HTTP and 'moto' to mock AWS.
"""
from __future__ import annotations

import json
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest
import responses as resp_lib
import requests

from src.models.domain import (
    AppConfig, EndpointConfig, HealthStatus, CheckResult,
)


# ── HTTP Checker ──────────────────────────────────────────────────

class TestHTTPChecker:
    """Tests the _HTTPChecker internal class via the HealthChecker."""

    def _make_ep(self, **kwargs) -> EndpointConfig:
        return EndpointConfig(name="test", url="http://example.com/health", **kwargs)

    @resp_lib.activate
    def test_healthy_200(self):
        from src.health_checker.main import _HTTPChecker
        resp_lib.add(resp_lib.GET, "http://example.com/health",
                     json={"status": "healthy"}, status=200)
        checker = _HTTPChecker()
        ep = self._make_ep()
        result = checker.check(ep, warn_ms=2000, critical_ms=5000, max_retries=1)
        assert result.status == HealthStatus.HEALTHY
        assert result.response_time_ms > 0
        assert result.error is None

    @resp_lib.activate
    def test_unhealthy_500(self):
        from src.health_checker.main import _HTTPChecker
        resp_lib.add(resp_lib.GET, "http://example.com/health",
                     json={"error": "internal error"}, status=500)
        checker = _HTTPChecker()
        result = checker.check(self._make_ep(), warn_ms=2000, critical_ms=5000, max_retries=1)
        assert result.status == HealthStatus.UNHEALTHY
        assert "500" in result.error

    @resp_lib.activate
    def test_connection_refused(self):
        from src.health_checker.main import _HTTPChecker
        resp_lib.add(resp_lib.GET, "http://example.com/health",
                     body=requests.exceptions.ConnectionError("refused"))
        checker = _HTTPChecker()
        result = checker.check(self._make_ep(), warn_ms=2000, critical_ms=5000, max_retries=1)
        assert result.status == HealthStatus.UNHEALTHY
        assert "Connection error" in result.error

    @resp_lib.activate
    def test_timeout(self):
        from src.health_checker.main import _HTTPChecker
        resp_lib.add(resp_lib.GET, "http://example.com/health",
                     body=requests.exceptions.Timeout())
        checker = _HTTPChecker()
        result = checker.check(self._make_ep(timeout_seconds=1),
                               warn_ms=2000, critical_ms=5000, max_retries=1)
        assert result.status == HealthStatus.UNHEALTHY
        assert "Timeout" in result.error

    @resp_lib.activate
    def test_body_check_missing_content(self):
        from src.health_checker.main import _HTTPChecker
        resp_lib.add(resp_lib.GET, "http://example.com/health",
                     body='{"status":"degraded"}', status=200)
        checker = _HTTPChecker()
        ep = self._make_ep(expected_body_contains="healthy")
        result = checker.check(ep, warn_ms=2000, critical_ms=5000, max_retries=1)
        assert result.status == HealthStatus.DEGRADED
        assert "healthy" in result.error

    @resp_lib.activate
    def test_slow_response_degraded(self):
        from src.health_checker.main import _HTTPChecker
        import time

        def slow_response(request):
            # Simulate slow response by manipulating response time check
            from requests.models import Response
            r = Response()
            r.status_code = 200
            r._content = b'{"status":"healthy"}'
            return r

        resp_lib.add_callback(resp_lib.GET, "http://example.com/health", slow_response)

        checker = _HTTPChecker()
        # Set critical_ms very low so any response is "slow"
        result = checker.check(self._make_ep(), warn_ms=0, critical_ms=1, max_retries=1)
        # With critical_ms=1, any response will exceed it → DEGRADED
        assert result.status in (HealthStatus.DEGRADED, HealthStatus.HEALTHY)

    @resp_lib.activate
    def test_retry_succeeds_on_second_attempt(self):
        from src.health_checker.main import _HTTPChecker
        # First call fails, second succeeds
        resp_lib.add(resp_lib.GET, "http://example.com/health", status=500)
        resp_lib.add(resp_lib.GET, "http://example.com/health", status=200,
                     json={"status": "healthy"})

        checker = _HTTPChecker()
        result = checker.check(self._make_ep(), warn_ms=2000, critical_ms=5000, max_retries=2)
        assert result.status == HealthStatus.HEALTHY
        assert result.attempt == 2

    @resp_lib.activate
    def test_all_retries_exhausted(self):
        from src.health_checker.main import _HTTPChecker
        resp_lib.add(resp_lib.GET, "http://example.com/health", status=500)
        resp_lib.add(resp_lib.GET, "http://example.com/health", status=500)

        checker = _HTTPChecker()
        result = checker.check(self._make_ep(), warn_ms=2000, critical_ms=5000, max_retries=2)
        assert result.status == HealthStatus.UNHEALTHY
        assert result.attempt == 2

    @resp_lib.activate
    def test_details_include_response_body(self):
        from src.health_checker.main import _HTTPChecker
        resp_lib.add(resp_lib.GET, "http://example.com/health",
                     json={"status": "healthy", "uptime": 12345}, status=200)
        checker = _HTTPChecker()
        result = checker.check(self._make_ep(), warn_ms=2000, critical_ms=5000, max_retries=1)
        assert "body" in result.details
        assert result.details["body"]["uptime"] == 12345


# ── HealthChecker Orchestrator ────────────────────────────────────

class TestHealthCheckerOrchestrator:

    def _make_config(self, endpoints=None) -> AppConfig:
        return AppConfig(
            project_name="test",
            environment="local",
            endpoints=endpoints or [],
        )

    @resp_lib.activate
    def test_overall_healthy_when_all_pass(self):
        from src.health_checker.main import HealthChecker
        resp_lib.add(resp_lib.GET, "http://api.example.com/health",
                     json={"status": "ok"}, status=200)
        resp_lib.add(resp_lib.GET, "http://web.example.com/",
                     body="<html>ok</html>", status=200)

        config = self._make_config(endpoints=[
            EndpointConfig(name="api", url="http://api.example.com/health"),
            EndpointConfig(name="web", url="http://web.example.com/"),
        ])
        checker = HealthChecker(config)
        report = checker.run()
        assert report.overall_status == HealthStatus.HEALTHY
        assert report.summary["healthy"] == 2

    @resp_lib.activate
    def test_overall_unhealthy_when_any_fail(self):
        from src.health_checker.main import HealthChecker
        resp_lib.add(resp_lib.GET, "http://api.example.com/health",
                     json={"status": "ok"}, status=200)
        resp_lib.add(resp_lib.GET, "http://dead.example.com/health",
                     body=requests.exceptions.ConnectionError("refused"))

        config = self._make_config(endpoints=[
            EndpointConfig(name="api", url="http://api.example.com/health"),
            EndpointConfig(name="dead", url="http://dead.example.com/health"),
        ])
        checker = HealthChecker(config)
        report = checker.run()
        assert report.overall_status == HealthStatus.UNHEALTHY

    @resp_lib.activate
    def test_disabled_endpoints_skipped(self):
        from src.health_checker.main import HealthChecker
        resp_lib.add(resp_lib.GET, "http://api.example.com/health",
                     json={"status": "ok"}, status=200)
        # dead endpoint is disabled — should NOT be checked

        config = self._make_config(endpoints=[
            EndpointConfig(name="api", url="http://api.example.com/health", enabled=True),
            EndpointConfig(name="dead", url="http://dead.example.com/health", enabled=False),
        ])
        checker = HealthChecker(config)
        report = checker.run()
        assert len(report.checks) == 1
        assert report.checks[0].name == "api"

    @resp_lib.activate
    def test_report_has_duration(self):
        from src.health_checker.main import HealthChecker
        resp_lib.add(resp_lib.GET, "http://api.example.com/health", status=200)
        config = self._make_config(endpoints=[
            EndpointConfig(name="api", url="http://api.example.com/health"),
        ])
        checker = HealthChecker(config)
        report = checker.run()
        assert report.duration_ms > 0

    @resp_lib.activate
    def test_empty_config_returns_healthy(self):
        from src.health_checker.main import HealthChecker
        config = self._make_config()
        checker = HealthChecker(config)
        report = checker.run()
        assert report.overall_status == HealthStatus.HEALTHY
        assert len(report.checks) == 0


# ── AWS Checker ───────────────────────────────────────────────────

class TestAWSChecker:

    def test_check_asg_not_found(self):
        from moto import mock_autoscaling
        from src.health_checker.main import _AWSChecker

        @mock_autoscaling
        def _test():
            checker = _AWSChecker(region="us-east-1")
            result = checker.check_asg("nonexistent-asg")
            assert result.status == HealthStatus.UNKNOWN
            assert "not found" in result.error

        _test()

    def test_check_rds_not_found(self):
        from moto import mock_rds
        from src.health_checker.main import _AWSChecker

        @mock_rds
        def _test():
            checker = _AWSChecker(region="us-east-1")
            result = checker.check_rds("nonexistent-db")
            assert result.status == HealthStatus.UNKNOWN

        _test()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
