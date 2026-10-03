"""
tests/unit/test_healer.py
Tests for circuit breaker, idempotency, and healing actions.
"""
from __future__ import annotations

import json
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest
from unittest.mock import MagicMock, patch, call
from moto import mock_dynamodb, mock_autoscaling, mock_ec2

from src.models.domain import HealAction, CircuitState


# ── Circuit Breaker ───────────────────────────────────────────────

class TestNullCircuitBreaker:
    """NullCircuitBreaker always allows requests."""

    def test_always_allows(self):
        from src.circuit_breaker.breaker import NullCircuitBreaker
        cb = NullCircuitBreaker()
        assert cb.allow_request("any-resource") is True

    def test_record_does_nothing(self):
        from src.circuit_breaker.breaker import NullCircuitBreaker
        cb = NullCircuitBreaker()
        cb.record_success("r1")
        cb.record_failure("r1")
        cb.force_reset("r1")
        assert cb.get_state("r1") == CircuitState.CLOSED


class TestCircuitBreaker:

    def _make_table(self, ddb_resource):
        table = ddb_resource.create_table(
            TableName="test-circuit-breaker",
            KeySchema=[{"AttributeName": "resource_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "resource_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        return table

    @mock_dynamodb
    def test_starts_closed(self):
        import boto3
        from src.circuit_breaker.breaker import CircuitBreaker
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self._make_table(ddb)
        cb = CircuitBreaker("test-circuit-breaker", region="us-east-1", failure_threshold=3)
        assert cb.allow_request("resource-1") is True
        assert cb.get_state("resource-1") == CircuitState.CLOSED

    @mock_dynamodb
    def test_opens_after_threshold(self):
        import boto3
        from src.circuit_breaker.breaker import CircuitBreaker
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self._make_table(ddb)
        cb = CircuitBreaker("test-circuit-breaker", region="us-east-1", failure_threshold=3)

        cb.record_failure("r1")
        assert cb.allow_request("r1") is True  # Still closed
        cb.record_failure("r1")
        assert cb.allow_request("r1") is True  # Still closed
        cb.record_failure("r1")                 # Threshold reached
        assert cb.allow_request("r1") is False  # Now OPEN

    @mock_dynamodb
    def test_resets_after_success(self):
        import boto3
        from src.circuit_breaker.breaker import CircuitBreaker
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        self._make_table(ddb)
        cb = CircuitBreaker("test-circuit-breaker", region="us-east-1", failure_threshold=2)

        cb.record_failure("r1")
        cb.record_failure("r1")  # Now OPEN
        assert cb.allow_request("r1") is False

        cb.force_reset("r1")
        assert cb.allow_request("r1") is True


# ── Healer Actions ────────────────────────────────────────────────

class TestASGScaler:

    @mock_autoscaling
    def test_scale_up_increments_desired(self):
        import boto3
        from src.healer.main import _Scaler

        asg = boto3.client("autoscaling", region_name="us-east-1")
        asg.create_launch_configuration(
            LaunchConfigurationName="test-lc",
            ImageId="ami-12345678",
            InstanceType="t3.micro",
        )
        asg.create_auto_scaling_group(
            AutoScalingGroupName="test-asg",
            LaunchConfigurationName="test-lc",
            MinSize=1, MaxSize=5, DesiredCapacity=2,
            AvailabilityZones=["us-east-1a"],
        )

        scaler = _Scaler(region="us-east-1")
        result = scaler.scale_up("test-asg", increment=1)
        assert result.success is True
        assert result.action == HealAction.SCALE_UP_ASG
        assert result.details["new"] == 3
        assert result.details["previous"] == 2

    @mock_autoscaling
    def test_scale_up_respects_max(self):
        import boto3
        from src.healer.main import _Scaler

        asg = boto3.client("autoscaling", region_name="us-east-1")
        asg.create_launch_configuration(
            LaunchConfigurationName="test-lc",
            ImageId="ami-12345678", InstanceType="t3.micro",
        )
        asg.create_auto_scaling_group(
            AutoScalingGroupName="test-asg",
            LaunchConfigurationName="test-lc",
            MinSize=1, MaxSize=2, DesiredCapacity=2,  # Already at max
            AvailabilityZones=["us-east-1a"],
        )

        scaler = _Scaler(region="us-east-1")
        result = scaler.scale_up("test-asg", increment=1)
        assert result.success is True
        assert "max capacity" in result.details["note"].lower()

    def test_scale_up_dry_run(self):
        from src.healer.main import _Scaler
        scaler = _Scaler(region="us-east-1")
        result = scaler.scale_up("any-asg", increment=2, dry_run=True)
        assert result.action == HealAction.DRY_RUN
        assert result.success is True
        assert result.details["dry_run"] is True


class TestRDSFailover:

    def test_rds_failover_dry_run(self):
        from src.healer.main import _RDSFailover
        rds = _RDSFailover(region="us-east-1")
        result = rds.failover("my-db", dry_run=True)
        assert result.action == HealAction.DRY_RUN
        assert result.success is True


class TestInstanceReplacer:

    def test_replace_dry_run(self):
        from src.healer.main import _Replacer
        replacer = _Replacer(region="us-east-1")
        result = replacer.replace("i-1234567890abcdef0", dry_run=True)
        assert result.action == HealAction.DRY_RUN
        assert result.success is True
        assert result.details["would_terminate"] == "i-1234567890abcdef0"


# ── Auto Healer Routing ───────────────────────────────────────────

class TestAutoHealerRouting:

    def _make_healer(self, dry_run=True):
        from src.healer.main import AutoHealer
        return AutoHealer(region="us-east-1", dry_run=dry_run)

    def test_heal_asg_failure_dry_run(self):
        healer = self._make_healer(dry_run=True)
        event = {
            "source": "health-checker",
            "environment": "dev",
            "overall_status": "unhealthy",
            "correlation_id": "test-corr-id",
            "asg_names": ["test-asg"],
            "failing_checks": [{
                "name": "asg/test-asg",
                "status": "unhealthy",
                "details": {
                    "unhealthy_instances": ["i-abc1234567890def"],
                    "healthy": 1,
                    "min_size": 2,
                },
            }],
        }
        report = healer.heal(event)
        assert report.incident_id.startswith("INC-")
        assert report.correlation_id == "test-corr-id"
        assert len(report.actions_taken) == 1
        assert report.actions_taken[0].action == HealAction.DRY_RUN

    def test_heal_rds_failure_dry_run(self):
        healer = self._make_healer(dry_run=True)
        event = {
            "source": "health-checker",
            "environment": "dev",
            "overall_status": "unhealthy",
            "asg_names": [],
            "failing_checks": [{
                "name": "rds/my-prod-db",
                "status": "unhealthy",
                "details": {"status": "failed"},
            }],
        }
        report = healer.heal(event)
        assert len(report.actions_taken) == 1
        assert report.actions_taken[0].action == HealAction.DRY_RUN
        assert report.actions_taken[0].target == "my-prod-db"

    def test_empty_failing_checks_no_actions(self):
        healer = self._make_healer(dry_run=True)
        event = {
            "source": "health-checker",
            "environment": "dev",
            "overall_status": "healthy",
            "asg_names": [],
            "failing_checks": [],
        }
        report = healer.heal(event)
        assert len(report.actions_taken) == 0
        assert report.overall_success is True

    def test_report_has_duration(self):
        healer = self._make_healer(dry_run=True)
        event = {
            "source": "health-checker",
            "environment": "dev",
            "overall_status": "unhealthy",
            "asg_names": ["test-asg"],
            "failing_checks": [{
                "name": "asg/test-asg",
                "status": "unhealthy",
                "details": {"unhealthy_instances": ["i-abc"], "healthy": 0, "min_size": 2},
            }],
        }
        report = healer.heal(event)
        assert report.duration_seconds >= 0

    def test_to_dict_json_serializable(self):
        import json
        healer = self._make_healer(dry_run=True)
        event = {
            "source": "health-checker",
            "environment": "dev",
            "overall_status": "unhealthy",
            "asg_names": [],
            "failing_checks": [],
        }
        report = healer.heal(event)
        d = report.to_dict()
        # Must be JSON serializable
        json.dumps(d)


# ── Idempotency ───────────────────────────────────────────────────

class TestNullIdempotency:

    def test_always_not_active(self):
        from src.healer.main import _NullIdempotency
        idem = _NullIdempotency()
        assert idem.is_active("any") is False

    def test_mark_and_clear_are_noop(self):
        from src.healer.main import _NullIdempotency
        idem = _NullIdempotency()
        idem.mark("r1", "INC-001")
        idem.clear("r1")
        assert idem.is_active("r1") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
