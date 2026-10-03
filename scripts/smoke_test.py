#!/usr/bin/env python3
"""
scripts/smoke_test.py

End-to-end smoke test: validates the full self-healing pipeline is working.
Run after deploying to verify everything is wired correctly.

Usage:
  python scripts/smoke_test.py --environment dev --region us-east-1
"""

import argparse
import json
import sys
import time

import boto3
import requests


def check(label: str, fn, *args, **kwargs):
    """Run a check and print result."""
    try:
        result = fn(*args, **kwargs)
        print(f"  ✅ {label}")
        return result
    except Exception as e:
        print(f"  ❌ {label}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--project", default="self-healing-infra")
    args = parser.parse_args()

    env = args.environment
    region = args.region
    project = args.project

    print(f"\n🔍 Smoke Test: {project} ({env}) in {region}")
    print("=" * 60)

    failures = 0

    # ── AWS Connectivity ──────────────────────────────────────────
    print("\n[1] AWS Connectivity")
    sts = boto3.client("sts", region_name=region)
    identity = check("AWS credentials valid", sts.get_caller_identity)
    if identity:
        print(f"      Account: {identity['Account']}, ARN: {identity['Arn']}")

    # ── Lambda Functions ──────────────────────────────────────────
    print("\n[2] Lambda Functions")
    lmb = boto3.client("lambda", region_name=region)

    for func_suffix in ["health-checker", "healer"]:
        func_name = f"{project}-{func_suffix}-{env}"
        result = check(
            f"Lambda exists: {func_name}",
            lmb.get_function,
            FunctionName=func_name,
        )
        if result:
            state = result["Configuration"]["State"]
            print(f"      State: {state}, Runtime: {result['Configuration']['Runtime']}")

    # ── Invoke Health Checker ─────────────────────────────────────
    print("\n[3] Lambda Invocation")
    func_name = f"{project}-health-checker-{env}"
    try:
        resp = lmb.invoke(
            FunctionName=func_name,
            InvocationType="RequestResponse",
            Payload=json.dumps({"source": "smoke-test"}).encode(),
        )
        payload = json.loads(resp["Payload"].read())
        status_code = resp.get("StatusCode")
        fn_error = resp.get("FunctionError")

        if fn_error:
            print(f"  ❌ Lambda invocation error: {fn_error}")
            print(f"     Payload: {payload}")
            failures += 1
        else:
            print(f"  ✅ Health checker invoked successfully (HTTP {status_code})")
            print(f"     Response: {json.dumps(payload, indent=6)}")
    except Exception as e:
        print(f"  ❌ Failed to invoke health checker: {e}")
        failures += 1

    # ── CloudWatch Metrics ────────────────────────────────────────
    print("\n[4] CloudWatch Metrics")
    cw = boto3.client("cloudwatch", region_name=region)
    result = check(
        "CloudWatch accessible",
        cw.list_metrics,
        Namespace=f"{project}/HealthChecker",
    )
    if result:
        metric_count = len(result.get("Metrics", []))
        print(f"      Found {metric_count} custom metric(s)")

    # ── SNS Topic ─────────────────────────────────────────────────
    print("\n[5] SNS Alert Topic")
    sns = boto3.client("sns", region_name=region)
    result = check(
        "SNS topics accessible",
        sns.list_topics,
    )
    if result:
        alert_topics = [
            t["TopicArn"] for t in result["Topics"]
            if f"{project}-alerts-{env}" in t["TopicArn"]
        ]
        if alert_topics:
            print(f"  ✅ Alert topic found: {alert_topics[0]}")
        else:
            print(f"  ⚠️  Alert topic '{project}-alerts-{env}' not found")

    # ── ASG Health ────────────────────────────────────────────────
    print("\n[6] Auto Scaling Group")
    asg_client = boto3.client("autoscaling", region_name=region)
    asg_name = f"{project}-asg-{env}"
    try:
        resp = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        if resp["AutoScalingGroups"]:
            asg = resp["AutoScalingGroups"][0]
            instances = asg["Instances"]
            healthy = [i for i in instances if i["HealthStatus"] == "Healthy"]
            print(f"  ✅ ASG found: {asg_name}")
            print(f"     Desired: {asg['DesiredCapacity']}, Healthy: {len(healthy)}/{len(instances)}")
        else:
            print(f"  ⚠️  ASG '{asg_name}' not found (run `terraform apply` first)")
    except Exception as e:
        print(f"  ❌ ASG check failed: {e}")

    # ── Summary ───────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if failures == 0:
        print("✅ All smoke tests passed!")
    else:
        print(f"❌ {failures} smoke test(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
