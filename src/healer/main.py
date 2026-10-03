"""
src/healer/main.py — Production Auto-Healer v3

What's new vs v2:
  - Circuit breaker on every resource (DynamoDB-backed)
  - Idempotency table prevents duplicate concurrent heals
  - Correlation IDs carried from health-checker through to SNS alert
  - MetricsPublisher integration
  - Full typed domain models from src/models/domain.py
  - Graceful escalation: restart → replace → scale-up
  - Heal report persisted to S3 with KMS encryption
"""
from __future__ import annotations

import json, logging, os, time, uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src.models.domain import HealAction, HealReport, HealResult
from src.circuit_breaker.breaker import get_circuit_breaker
from src.logger.structured import setup_logging, request_context
from src.metrics.publisher import get_publisher

logger = logging.getLogger(__name__)


# ── SSM Restarter ─────────────────────────────────────────────────

class _Restarter:
    def __init__(self, region: str):
        self._ssm = boto3.client("ssm", region_name=region)

    def restart(self, instance_id: str, service: str = "app", dry_run: bool = False) -> HealResult:
        t0 = time.perf_counter()
        target = f"{instance_id}/{service}"
        if dry_run:
            return HealResult(HealAction.DRY_RUN, True, target, details={"dry_run": True})
        try:
            r = self._ssm.send_command(
                InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
                Parameters={"commands": [
                    f"systemctl restart {service}", "sleep 5",
                    f"systemctl is-active {service} && echo SERVICE_OK || echo SERVICE_FAILED"],
                    "executionTimeout": ["60"]},
                Comment=f"Auto-heal: restart {service}", TimeoutSeconds=120)
            cmd_id = r["Command"]["CommandId"]
            out = self._poll(cmd_id, instance_id)
            ms = (time.perf_counter() - t0) * 1000
            ok = out.get("status") == "Success" and "SERVICE_OK" in out.get("output","")
            return HealResult(HealAction.RESTART_SERVICE, ok, target, duration_ms=ms,
                details={"command_id": cmd_id, "ssm_status": out.get("status"),
                         "output": out.get("output","")[:500]},
                error=None if ok else f"Restart failed: {out.get('output','')[-200:]}")
        except ClientError as e:
            return HealResult(HealAction.RESTART_SERVICE, False, target,
                duration_ms=(time.perf_counter()-t0)*1000, error=str(e))

    def _poll(self, cmd_id: str, iid: str, timeout: int = 60) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self._ssm.get_command_invocation(CommandId=cmd_id, InstanceId=iid)
                if r["Status"] in ("Success","Failed","TimedOut","Cancelled"):
                    return {"status": r["Status"],
                            "output": r.get("StandardOutputContent","") + r.get("StandardErrorContent","")}
            except self._ssm.exceptions.InvocationDoesNotExist: pass
            time.sleep(3)
        return {"status": "Timeout", "output": ""}


# ── Instance Replacer ─────────────────────────────────────────────

class _Replacer:
    def __init__(self, region: str):
        self._asg = boto3.client("autoscaling", region_name=region)

    def replace(self, instance_id: str, dry_run: bool = False) -> HealResult:
        t0 = time.perf_counter()
        if dry_run:
            return HealResult(HealAction.DRY_RUN, True, instance_id,
                details={"dry_run": True, "would_terminate": instance_id})
        try:
            r = self._asg.terminate_instance_in_auto_scaling_group(
                InstanceId=instance_id, ShouldDecrementDesiredCapacity=False)
            ms = (time.perf_counter() - t0) * 1000
            act = r.get("Activity", {})
            return HealResult(HealAction.REPLACE_INSTANCE, True, instance_id, duration_ms=ms,
                details={"activity_id": act.get("ActivityId"),
                         "note": "ASG will launch replacement automatically"})
        except ClientError as e:
            return HealResult(HealAction.REPLACE_INSTANCE, False, instance_id,
                duration_ms=(time.perf_counter()-t0)*1000, error=str(e))


# ── ASG Scaler ────────────────────────────────────────────────────

class _Scaler:
    def __init__(self, region: str):
        self._asg = boto3.client("autoscaling", region_name=region)

    def scale_up(self, asg_name: str, increment: int = 1, dry_run: bool = False) -> HealResult:
        t0 = time.perf_counter()
        if dry_run:
            return HealResult(HealAction.DRY_RUN, True, asg_name,
                details={"dry_run": True, "would_scale_by": increment})
        try:
            r = self._asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
            g = r["AutoScalingGroups"][0]
            current, max_size = g["DesiredCapacity"], g["MaxSize"]
            new = min(current + increment, max_size)
            if new == current:
                return HealResult(HealAction.SCALE_UP_ASG, True, asg_name,
                    details={"note": f"Already at max capacity ({max_size})"})
            self._asg.set_desired_capacity(AutoScalingGroupName=asg_name,
                DesiredCapacity=new, HonorCooldown=False)
            ms = (time.perf_counter() - t0) * 1000
            return HealResult(HealAction.SCALE_UP_ASG, True, asg_name, duration_ms=ms,
                details={"previous": current, "new": new, "max": max_size})
        except ClientError as e:
            return HealResult(HealAction.SCALE_UP_ASG, False, asg_name,
                duration_ms=(time.perf_counter()-t0)*1000, error=str(e))


# ── RDS Failover ──────────────────────────────────────────────────

class _RDSFailover:
    def __init__(self, region: str):
        self._rds = boto3.client("rds", region_name=region)

    def failover(self, db_id: str, dry_run: bool = False) -> HealResult:
        t0 = time.perf_counter()
        if dry_run:
            return HealResult(HealAction.DRY_RUN, True, db_id, details={"dry_run": True})
        try:
            self._rds.reboot_db_instance(DBInstanceIdentifier=db_id, ForceFailover=True)
            ms = (time.perf_counter() - t0) * 1000
            return HealResult(HealAction.RDS_FAILOVER, True, db_id, duration_ms=ms,
                details={"note": "Multi-AZ failover initiated (~60-120s to promote standby)"})
        except ClientError as e:
            return HealResult(HealAction.RDS_FAILOVER, False, db_id,
                duration_ms=(time.perf_counter()-t0)*1000, error=str(e))


# ── Idempotency ───────────────────────────────────────────────────

class _Idempotency:
    """DynamoDB-backed cooldown: skips heal if one ran within cooldown_minutes."""

    def __init__(self, table: str, region: str, cooldown_minutes: int = 5):
        self._table = boto3.resource("dynamodb", region_name=region).Table(table)
        self._cd = cooldown_minutes * 60

    def is_active(self, rid: str) -> bool:
        try:
            item = self._table.get_item(Key={"resource_id": rid}).get("Item")
            if not item: return False
            return (time.time() - float(item.get("started_at", 0))) < self._cd
        except Exception as e:
            logger.warning(f"Idempotency check failed ({rid}): {e}")
            return False  # Fail open

    def mark(self, rid: str, incident_id: str) -> None:
        try:
            now = time.time()
            self._table.put_item(Item={"resource_id": rid, "incident_id": incident_id,
                "started_at": str(now), "ttl": int(now) + 3600})
        except Exception as e:
            logger.warning(f"Idempotency mark failed ({rid}): {e}")

    def clear(self, rid: str) -> None:
        try: self._table.delete_item(Key={"resource_id": rid})
        except Exception: pass

class _NullIdempotency:
    def is_active(self, rid): return False
    def mark(self, rid, iid): pass
    def clear(self, rid): pass


# ── Auto Healer ───────────────────────────────────────────────────

class AutoHealer:

    def __init__(self, region: str, dry_run: bool = False,
                 idempotency_table: str | None = None,
                 circuit_table: str | None = None,
                 cooldown_minutes: int = 5):
        self.region   = region
        self.dry_run  = dry_run
        self._restart  = _Restarter(region)
        self._replace  = _Replacer(region)
        self._scale    = _Scaler(region)
        self._rds      = _RDSFailover(region)
        self._idem     = (_Idempotency(idempotency_table, region, cooldown_minutes)
                          if idempotency_table and not dry_run else _NullIdempotency())
        self._cb       = get_circuit_breaker(circuit_table, region)
        self._s3       = boto3.client("s3", region_name=region)
        self._sns      = boto3.client("sns", region_name=region)

    def heal(self, event: dict) -> HealReport:
        t0 = time.perf_counter()
        incident_id = (f"INC-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
                       f"-{uuid.uuid4().hex[:6].upper()}")
        correlation_id = event.get("correlation_id", str(uuid.uuid4()))
        env = event.get("environment", "unknown")
        failing = event.get("failing_checks", [])
        asg_names = event.get("asg_names", [])

        logger.info(f"[{incident_id}] Heal started",
                    extra={"incident_id": incident_id, "failing_count": len(failing)})

        actions: list[HealResult] = []
        for check in failing:
            name, details = check.get("name",""), check.get("details",{})
            result = self._route(name, details, asg_names, incident_id)
            actions.append(result)
            logger.info(f"[{incident_id}] {name} → {result.action}: {'OK' if result.success else 'FAIL'}")

        report = HealReport(
            incident_id=incident_id, correlation_id=correlation_id,
            environment=env,
            timestamp=datetime.now(timezone.utc).isoformat(),
            trigger_reason=event.get("overall_status","unknown"),
            actions_taken=actions,
            overall_success=all(a.success for a in actions) if actions else True,
            duration_seconds=round(time.perf_counter()-t0, 2),
        )
        self._persist(report, event)
        return report

    def _route(self, name: str, details: dict,
               asg_names: list[str], incident_id: str) -> HealResult:
        if name.startswith("asg/"):
            return self._heal_asg(name.split("/",1)[1], details, incident_id)
        if name.startswith("target-group/"):
            return self._heal_tg(details, asg_names, incident_id)
        if name.startswith("rds/"):
            return self._heal_rds(name.split("/",1)[1], incident_id)
        # HTTP/ALB failure → scale up
        for asg in asg_names:
            return self._scale.scale_up(asg, dry_run=self.dry_run)
        return HealResult(HealAction.NO_ACTION, True, name,
            details={"note": "No routing rule matched"})

    def _heal_asg(self, asg_name: str, details: dict, incident_id: str) -> HealResult:
        unhealthy = details.get("unhealthy_instances", [])
        if unhealthy:
            iid = unhealthy[0]
            if self._idem.is_active(iid):
                return HealResult(HealAction.COOLDOWN_ACTIVE, True, iid,
                    details={"reason": "cooldown active"})
            if not self._cb.allow_request(iid):
                return HealResult(HealAction.CIRCUIT_OPEN, True, iid,
                    details={"reason": "circuit open"})
            self._idem.mark(iid, incident_id)
            # Try restart first
            r = self._restart.restart(iid, dry_run=self.dry_run)
            if r.success:
                self._cb.record_success(iid); self._idem.clear(iid); return r
            # Restart failed → replace
            logger.warning(f"Restart failed for {iid}, replacing instance")
            self._cb.record_failure(iid)
            r2 = self._replace.replace(iid, dry_run=self.dry_run)
            if r2.success: self._idem.clear(iid)
            return r2
        # No specific instance — scale up
        if details.get("healthy",0) < details.get("min_size",1):
            return self._scale.scale_up(asg_name, dry_run=self.dry_run)
        return HealResult(HealAction.NO_ACTION, True, asg_name,
            details={"note": "No unhealthy instances identified"})

    def _heal_tg(self, details: dict, asg_names: list[str], incident_id: str) -> HealResult:
        for t in details.get("unhealthy_targets", []):
            iid = t.get("id","")
            if iid.startswith("i-"):
                if self._idem.is_active(iid):
                    return HealResult(HealAction.COOLDOWN_ACTIVE, True, iid,
                        details={"reason": "cooldown"})
                if not self._cb.allow_request(iid):
                    return HealResult(HealAction.CIRCUIT_OPEN, True, iid,
                        details={"reason": "circuit open"})
                self._idem.mark(iid, incident_id)
                r = self._replace.replace(iid, dry_run=self.dry_run)
                if r.success: self._cb.record_success(iid); self._idem.clear(iid)
                else: self._cb.record_failure(iid)
                return r
        for asg in asg_names:
            return self._scale.scale_up(asg, dry_run=self.dry_run)
        return HealResult(HealAction.NO_ACTION, True, "target-group",
            details={"note": "No actionable target"})

    def _heal_rds(self, db_id: str, incident_id: str) -> HealResult:
        if self._idem.is_active(db_id):
            return HealResult(HealAction.COOLDOWN_ACTIVE, True, db_id,
                details={"reason": "cooldown active"})
        self._idem.mark(db_id, incident_id)
        r = self._rds.failover(db_id, dry_run=self.dry_run)
        if r.success: self._idem.clear(db_id)
        return r

    def _persist(self, report: HealReport, event: dict) -> None:
        bucket = os.environ.get("LOGS_BUCKET")
        if not bucket: return
        now = datetime.now(timezone.utc)
        key = (f"incidents/{report.environment}/{now.strftime('%Y/%m/%d')}/"
               f"{report.incident_id}.json")
        try:
            self._s3.put_object(Bucket=bucket, Key=key,
                Body=json.dumps({"incident": report.to_dict(), "original_event": event},
                    indent=2, default=str),
                ContentType="application/json", ServerSideEncryption="aws:kms")
            logger.info(f"Incident persisted: s3://{bucket}/{key}")
        except Exception as e:
            logger.error(f"S3 persist failed: {e}")


# ── Lambda handler ────────────────────────────────────────────────

_healer: AutoHealer | None = None
_log_shipper = None

def lambda_handler(event: dict, context: Any) -> dict:
    global _healer, _log_shipper
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    env    = os.environ.get("ENVIRONMENT", "unknown")

    if _log_shipper is None:
        _log_shipper = setup_logging(
            service="healer", environment=env,
            level=os.environ.get("LOG_LEVEL","INFO"),
            s3_bucket=os.environ.get("LOGS_BUCKET"), region=region)

    if _healer is None:
        _healer = AutoHealer(
            region=region,
            dry_run=os.environ.get("DRY_RUN","false").lower() == "true",
            idempotency_table=os.environ.get("IDEMPOTENCY_TABLE"),
            circuit_table=os.environ.get("CIRCUIT_BREAKER_TABLE"),
            cooldown_minutes=int(os.environ.get("HEAL_COOLDOWN_MINUTES","5")),
        )

    for record in event.get("Records", [event]):
        raw = record.get("Sns", {}).get("Message", "{}")
        try: heal_event = json.loads(raw)
        except json.JSONDecodeError: heal_event = record

        if heal_event.get("source") != "health-checker":
            continue

        with request_context(correlation_id=heal_event.get("correlation_id"),
                             environment=env, incident_source="sns"):
            report = _healer.heal(heal_event)
            logger.info("Heal complete", extra={
                "incident_id": report.incident_id,
                "overall_success": report.overall_success,
                "actions": len(report.actions_taken),
                "duration_s": report.duration_seconds,
            })
            _notify(report)

    if _log_shipper:
        _log_shipper.flush("healer", env)

    return {"statusCode": 200}


def _notify(report: HealReport) -> None:
    topic = os.environ.get("SNS_TOPIC_ARN")
    if not topic: return
    icon = "✅" if report.overall_success else "❌"
    actions = "\n".join(
        f"  • {a.action} → {a.target}: {'OK' if a.success else 'FAILED'}"
        + (f"\n    {a.error}" if not a.success and a.error else "")
        for a in report.actions_taken) or "  (no actions taken)"
    try:
        boto3.client("sns").publish(
            TopicArn=topic,
            Subject=(f"{icon} [{report.environment.upper()}] Auto-Heal {report.incident_id}: "
                     f"{'Resolved' if report.overall_success else 'Escalation needed'}"),
            Message=(f"Incident:    {report.incident_id}\n"
                     f"Correlation: {report.correlation_id}\n"
                     f"Environment: {report.environment}\n"
                     f"Duration:    {report.duration_seconds}s\n\n"
                     f"Actions:\n{actions}"),
        )
    except Exception as e:
        logger.error(f"Notification failed: {e}")
