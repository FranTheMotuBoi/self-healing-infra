"""
src/health_checker/main.py — Production Health Checker v3

What's new vs v2:
  - Imports from shared src/models/domain.py (single source of truth)
  - Proper consecutive-failure tracking with SSM state store
  - Per-check retry with exponential backoff
  - MetricsPublisher integration
  - Structured logging with JSON formatter + correlation IDs
  - HEAD method support for cheap health checks
  - Timeout per endpoint from config
  - Lambda warm-start: reuses HealthChecker instance between invocations
"""
from __future__ import annotations

import json, logging, os, time, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import boto3, requests, urllib3
from botocore.exceptions import ClientError

from src.models.domain import (
    AppConfig, CheckResult, HealthReport, HealthStatus, EndpointConfig,
)
from src.logger.structured import setup_logging, request_context
from src.metrics.publisher import get_publisher

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)


# ── State store for consecutive-failure tracking ──────────────────

class _FailureStore:
    """Persists consecutive-failure counts in SSM Parameter Store."""

    def __init__(self, project: str, env: str, region: str):
        self._ssm = boto3.client("ssm", region_name=region)
        self._prefix = f"/{project}/{env}/failures"

    def get(self, name: str) -> int:
        try:
            r = self._ssm.get_parameter(Name=f"{self._prefix}/{name.replace('/','-')}")
            return int(r["Parameter"]["Value"])
        except ClientError:
            return 0

    def increment(self, name: str) -> int:
        n = self.get(name) + 1
        try:
            self._ssm.put_parameter(Name=f"{self._prefix}/{name.replace('/','-')}",
                Value=str(n), Type="String", Overwrite=True)
        except ClientError as e:
            logger.warning(f"Failed to persist failure count: {e}")
        return n

    def reset(self, name: str) -> None:
        try:
            self._ssm.delete_parameter(Name=f"{self._prefix}/{name.replace('/','-')}")
        except ClientError:
            pass

class _NullFailureStore:
    def get(self, n): return 0
    def increment(self, n): return 1
    def reset(self, n): pass


# ── HTTP Checker ──────────────────────────────────────────────────

class _HTTPChecker:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "SelfHealingInfra/3.0"

    def check(self, ep: EndpointConfig, warn_ms: int, critical_ms: int,
              max_retries: int = 2) -> CheckResult:
        last = None
        for attempt in range(1, max_retries + 1):
            r = self._once(ep, attempt, warn_ms, critical_ms)
            if r.is_healthy:
                return r
            last = r
            if attempt < max_retries:
                time.sleep(2 ** (attempt - 1))
        return last

    def _once(self, ep: EndpointConfig, attempt: int,
              warn_ms: int, critical_ms: int) -> CheckResult:
        start = time.perf_counter()
        try:
            resp = self._session.request(
                method=ep.method, url=ep.url, timeout=ep.timeout_seconds,
                verify=ep.verify_ssl, headers=ep.headers)
            ms = (time.perf_counter() - start) * 1000
            details = {"url": ep.url, "status_code": resp.status_code,
                       "response_time_ms": round(ms, 2), "attempt": attempt}
            try: details["body"] = resp.json()
            except Exception: details["body_snippet"] = resp.text[:300]

            if resp.status_code != ep.expected_status:
                return CheckResult(ep.name, HealthStatus.UNHEALTHY, ms, details=details,
                    attempt=attempt,
                    error=f"Expected HTTP {ep.expected_status}, got {resp.status_code}")
            if ep.expected_body_contains and ep.expected_body_contains not in resp.text:
                return CheckResult(ep.name, HealthStatus.DEGRADED, ms, details=details,
                    attempt=attempt,
                    error=f"Response body missing: '{ep.expected_body_contains}'")
            if ms >= critical_ms:
                return CheckResult(ep.name, HealthStatus.DEGRADED, ms, details=details,
                    attempt=attempt, error=f"Slow response: {ms:.0f}ms >= {critical_ms}ms")
            if ms >= warn_ms:
                details["warning"] = f"Slow: {ms:.0f}ms"
            return CheckResult(ep.name, HealthStatus.HEALTHY, ms, details=details, attempt=attempt)

        except requests.exceptions.ConnectionError as e:
            ms = (time.perf_counter() - start) * 1000
            return CheckResult(ep.name, HealthStatus.UNHEALTHY, ms, attempt=attempt,
                error=f"Connection error: {str(e)[:200]}", details={"url": ep.url})
        except requests.exceptions.Timeout:
            ms = (time.perf_counter() - start) * 1000
            return CheckResult(ep.name, HealthStatus.UNHEALTHY, ms, attempt=attempt,
                error=f"Timeout after {ep.timeout_seconds}s", details={"url": ep.url})
        except Exception as e:
            ms = (time.perf_counter() - start) * 1000
            return CheckResult(ep.name, HealthStatus.UNKNOWN, ms, attempt=attempt,
                error=str(e), details={"url": ep.url})


# ── AWS Checkers ──────────────────────────────────────────────────

class _AWSChecker:
    def __init__(self, region: str):
        self._region = region
        self._asg = self._elbv2 = self._rds = None

    @property
    def asg(self):
        if not self._asg: self._asg = boto3.client("autoscaling", region_name=self._region)
        return self._asg

    @property
    def elbv2(self):
        if not self._elbv2: self._elbv2 = boto3.client("elbv2", region_name=self._region)
        return self._elbv2

    @property
    def rds(self):
        if not self._rds: self._rds = boto3.client("rds", region_name=self._region)
        return self._rds

    def check_asg(self, name: str) -> CheckResult:
        start = time.perf_counter()
        try:
            resp = self.asg.describe_auto_scaling_groups(AutoScalingGroupNames=[name])
            ms = (time.perf_counter() - start) * 1000
            if not resp["AutoScalingGroups"]:
                return CheckResult(f"asg/{name}", HealthStatus.UNKNOWN, ms, error="ASG not found")
            g = resp["AutoScalingGroups"][0]
            instances = g.get("Instances", [])
            healthy = [i for i in instances
                       if i["HealthStatus"] == "Healthy" and i["LifecycleState"] == "InService"]
            unhealthy = [i for i in instances if i["HealthStatus"] != "Healthy"]
            desired, min_size = g["DesiredCapacity"], g["MinSize"]
            details = {"desired": desired, "min_size": min_size, "max_size": g["MaxSize"],
                       "healthy": len(healthy), "unhealthy": len(unhealthy),
                       "unhealthy_instances": [i["InstanceId"] for i in unhealthy]}
            if len(healthy) == 0:
                status, err = HealthStatus.UNHEALTHY, "No healthy instances"
            elif len(healthy) < min_size:
                status, err = HealthStatus.UNHEALTHY, f"Below minimum: {len(healthy)}/{min_size}"
            elif len(healthy) < desired:
                status, err = HealthStatus.DEGRADED, f"Below desired: {len(healthy)}/{desired}"
            else:
                status, err = HealthStatus.HEALTHY, None
            return CheckResult(f"asg/{name}", status, ms, details=details, error=err)
        except ClientError as e:
            return CheckResult(f"asg/{name}", HealthStatus.UNKNOWN,
                (time.perf_counter()-start)*1000, error=str(e))

    def check_target_group(self, arn: str) -> CheckResult:
        start = time.perf_counter()
        tg = arn.split(":")[-1].split("/")[1] if ":" in arn else arn
        try:
            resp = self.elbv2.describe_target_health(TargetGroupArn=arn)
            ms = (time.perf_counter() - start) * 1000
            targets = resp["TargetHealthDescriptions"]
            healthy = [t for t in targets if t["TargetHealth"]["State"] == "healthy"]
            unhealthy = [t for t in targets if t["TargetHealth"]["State"] != "healthy"]
            details = {"total": len(targets), "healthy": len(healthy), "unhealthy": len(unhealthy),
                       "unhealthy_targets": [{"id": t["Target"]["Id"],
                           "state": t["TargetHealth"]["State"],
                           "reason": t["TargetHealth"].get("Reason","")} for t in unhealthy]}
            if not healthy and targets:
                status, err = HealthStatus.UNHEALTHY, "No healthy targets"
            elif unhealthy:
                status, err = HealthStatus.DEGRADED, f"{len(unhealthy)} unhealthy target(s)"
            else:
                status, err = HealthStatus.HEALTHY, None
            return CheckResult(f"target-group/{tg}", status, ms, details=details, error=err)
        except ClientError as e:
            return CheckResult(f"target-group/{tg}", HealthStatus.UNKNOWN,
                (time.perf_counter()-start)*1000, error=str(e))

    def check_rds(self, db_id: str) -> CheckResult:
        start = time.perf_counter()
        try:
            resp = self.rds.describe_db_instances(DBInstanceIdentifier=db_id)
            ms = (time.perf_counter() - start) * 1000
            db = resp["DBInstances"][0]
            s = db["DBInstanceStatus"]
            details = {"status": s, "engine": f"{db['Engine']} {db['EngineVersion']}",
                       "class": db["DBInstanceClass"], "multi_az": db["MultiAZ"]}
            if s == "available": status, err = HealthStatus.HEALTHY, None
            elif s in ("backing-up","maintenance","modifying","upgrading"):
                status, err = HealthStatus.DEGRADED, f"RDS in '{s}' state"
            else: status, err = HealthStatus.UNHEALTHY, f"RDS unexpected state: '{s}'"
            return CheckResult(f"rds/{db_id}", status, ms, details=details, error=err)
        except ClientError as e:
            return CheckResult(f"rds/{db_id}", HealthStatus.UNKNOWN,
                (time.perf_counter()-start)*1000, error=str(e))


# ── Orchestrator ──────────────────────────────────────────────────

class HealthChecker:

    def __init__(self, config: AppConfig):
        self.cfg = config
        self._http = _HTTPChecker()
        self._aws  = _AWSChecker(region=config.aws_region)
        self._store = (
            _FailureStore(config.project_name, config.environment, config.aws_region)
            if config.environment != "local" else _NullFailureStore()
        )
        self._metrics = get_publisher(config.project_name, config.environment, config.aws_region)

    def run(self) -> HealthReport:
        t0 = time.perf_counter()
        tasks = self._build_tasks()
        results: list[CheckResult] = []

        with ThreadPoolExecutor(max_workers=min(len(tasks) + 1, 20)) as pool:
            futures = {pool.submit(fn): name for name, fn in tasks}
            for future in as_completed(futures):
                try:
                    r = future.result(timeout=60)
                    r = self._apply_failure_tracking(r)
                    results.append(r)
                    self._metrics.record_check(r.name, r.status, r.response_time_ms)
                except Exception as e:
                    name = futures[future]
                    logger.exception(f"Check task raised: {name}")
                    results.append(CheckResult(name, HealthStatus.UNKNOWN, 0, error=str(e)))

        statuses = [r.status for r in results]
        if any(s == HealthStatus.UNHEALTHY for s in statuses):
            overall = HealthStatus.UNHEALTHY
        elif any(s in (HealthStatus.DEGRADED, HealthStatus.UNKNOWN) for s in statuses):
            overall = HealthStatus.DEGRADED
        else:
            overall = HealthStatus.HEALTHY

        self._metrics.count(f"Overall.{overall.capitalize()}")
        self._metrics.flush()

        return HealthReport(
            environment=self.cfg.environment,
            overall_status=overall,
            timestamp=datetime.now(timezone.utc).isoformat(),
            checks=results,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )

    def _apply_failure_tracking(self, r: CheckResult) -> CheckResult:
        threshold = self.cfg.thresholds.consecutive_failures_before_heal
        if r.is_healthy:
            self._store.reset(r.name)
            return r
        count = self._store.increment(r.name)
        r = r.with_consecutive_failures(count)
        if count < threshold:
            logger.info(f"Suppressing heal for {r.name}: {count}/{threshold} consecutive failures")
        return r

    def _build_tasks(self):
        tasks, t = [], self.cfg.thresholds
        for ep in self.cfg.endpoints:
            if ep.enabled:
                http = self._http
                tasks.append((ep.name, lambda e=ep: http.check(
                    e, t.response_time_warn_ms, t.response_time_critical_ms)))
        for asg in self.cfg.asg_names:
            aws = self._aws
            tasks.append((f"asg/{asg}", lambda a=asg: aws.check_asg(a)))
        for tg in self.cfg.target_group_arns:
            aws = self._aws
            tasks.append((f"tg/{tg[-20:]}", lambda a=tg: aws.check_target_group(a)))
        for db in self.cfg.rds_identifiers:
            aws = self._aws
            tasks.append((f"rds/{db}", lambda d=db: aws.check_rds(d)))
        return tasks


# ── Lambda handler (reuses instance across warm invocations) ──────

_checker: HealthChecker | None = None
_log_shipper = None

def lambda_handler(event: dict, context: Any) -> dict:
    global _checker, _log_shipper
    env    = os.environ.get("ENVIRONMENT", "unknown")
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    if _log_shipper is None:
        _log_shipper = setup_logging(
            service="health-checker", environment=env,
            level=os.environ.get("LOG_LEVEL", "INFO"),
            s3_bucket=os.environ.get("LOGS_BUCKET"), region=region)

    request_id = getattr(context, "aws_request_id", str(uuid.uuid4()))

    with request_context(correlation_id=request_id, environment=env):
        try:
            config = AppConfig.from_env()
        except Exception as e:
            logger.critical(f"Config load failed: {e}")
            raise

        if _checker is None:
            _checker = HealthChecker(config)

        report = _checker.run()

        logger.info("Health check complete", extra={
            "overall_status": report.overall_status,
            "summary": report.summary,
            "duration_ms": report.duration_ms,
        })

        threshold = config.thresholds.consecutive_failures_before_heal
        actionable = [
            c.to_dict() for c in report.failing_checks
            if c.consecutive_failures >= threshold
        ]

        if actionable:
            _trigger_healer(actionable, report, config)

        if _log_shipper:
            _log_shipper.flush("health-checker", env)

        return {"statusCode": 200, "body": json.dumps({
            "correlation_id": report.correlation_id,
            "status": report.overall_status,
            "summary": report.summary,
        })}


def _trigger_healer(failing: list[dict], report: HealthReport, config: AppConfig) -> None:
    topic_arn = config.alerting.sns_topic_arn
    if not topic_arn:
        return
    try:
        boto3.client("sns", region_name=config.aws_region).publish(
            TopicArn=topic_arn,
            Subject=f"[{config.environment.upper()}] Health Check Failed — {report.overall_status}",
            Message=json.dumps({
                "source": "health-checker", "version": "3",
                "correlation_id": report.correlation_id,
                "environment": config.environment,
                "overall_status": report.overall_status,
                "timestamp": report.timestamp,
                "failing_checks": failing,
                "asg_names": config.asg_names,
            }, indent=2),
            MessageAttributes={
                "event_type":  {"DataType": "String", "StringValue": "health_check_failure"},
                "environment": {"DataType": "String", "StringValue": config.environment},
                "severity":    {"DataType": "String",
                    "StringValue": "critical" if report.overall_status == HealthStatus.UNHEALTHY else "warning"},
            },
        )
        logger.info(f"Healer triggered for {len(failing)} actionable failure(s)")
    except Exception as e:
        logger.error(f"Failed to trigger healer: {e}")


# ── CLI ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys
    parser = argparse.ArgumentParser(description="Self-Healing Infrastructure Health Checker v3")
    parser.add_argument("--config", required=True, help="Path to YAML config file")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    try:
        config = AppConfig.from_yaml(args.config)
    except Exception as e:
        print(f"Config error: {e}"); sys.exit(1)

    if args.dry_run:
        config = config.model_copy(update={"healing": config.healing.model_copy(update={"dry_run": True})})

    checker = HealthChecker(config)

    def run_once():
        report = checker.run()
        print(f"\n{'='*64}")
        print(f"  {report.overall_status.upper():12}  |  {report.timestamp}  |  {report.duration_ms:.0f}ms")
        print(f"  ID: {report.correlation_id[:12]}  Summary: {report.summary}")
        print("─" * 64)
        icons = {"healthy":"✅","degraded":"⚠️ ","unhealthy":"❌","unknown":"❓"}
        for c in sorted(report.checks, key=lambda x: x.status.value):
            icon = icons[c.status]
            consec = f" (x{c.consecutive_failures})" if c.consecutive_failures > 0 else ""
            sup = " [suppressed]" if c.consecutive_failures < config.thresholds.consecutive_failures_before_heal and not c.is_healthy else ""
            print(f"  {icon} {c.name:<44} {c.response_time_ms:>8.1f}ms  {c.status}{consec}{sup}")
            if c.error: print(f"     └─ {c.error}")
        print("=" * 64)

    if args.once:
        run_once()
    else:
        print(f"Watching every {args.interval}s — Ctrl+C to stop")
        while True:
            try: run_once()
            except KeyboardInterrupt: print("\nStopped."); break
            except Exception as e: logger.error(f"Cycle error: {e}")
            time.sleep(args.interval)
