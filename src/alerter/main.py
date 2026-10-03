"""
src/alerter/main.py

Rich alerting via Slack webhooks and SNS email.
Formats incidents beautifully so on-call engineers have full context.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3
import requests

logger = logging.getLogger(__name__)


class SlackAlerter:
    """Sends rich Slack notifications via incoming webhooks."""

    STATUS_COLORS = {
        "healthy": "#2eb886",
        "degraded": "#f2c94c",
        "unhealthy": "#e01e5a",
        "unknown": "#95a5a6",
        "resolved": "#2eb886",
    }

    STATUS_ICONS = {
        "healthy": "✅",
        "degraded": "⚠️",
        "unhealthy": "🚨",
        "unknown": "❓",
        "resolved": "✅",
    }

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_incident_alert(
        self,
        environment: str,
        overall_status: str,
        failing_checks: list[dict],
        incident_id: str | None = None,
        dashboard_url: str | None = None,
    ) -> bool:
        color = self.STATUS_COLORS.get(overall_status.lower(), "#95a5a6")
        icon = self.STATUS_ICONS.get(overall_status.lower(), "❓")

        check_rows = "\n".join(
            f"• `{c['name']}` → *{c['status'].upper()}*"
            + (f"\n  _{c.get('error', '')}_" if c.get("error") else "")
            for c in failing_checks
        )

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{icon} Infrastructure Alert: {overall_status.upper()}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Environment:*\n`{environment}`"},
                    {"type": "mrkdwn", "text": f"*Status:*\n`{overall_status}`"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Time:*\n{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Incident ID:*\n`{incident_id or 'N/A'}`",
                    },
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Failing Checks ({len(failing_checks)}):*\n{check_rows}",
                },
            },
        ]

        if dashboard_url:
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "📊 Open Dashboard"},
                        "url": dashboard_url,
                        "style": "primary",
                    }
                ],
            })

        blocks.append({"type": "divider"})
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "🤖 _Sent by Self-Healing Infrastructure System — auto-heal is running_",
                }
            ],
        })

        payload = {
            "attachments": [
                {
                    "color": color,
                    "blocks": blocks,
                    "fallback": f"[{environment.upper()}] {overall_status.upper()}: {len(failing_checks)} check(s) failing",
                }
            ]
        }

        return self._send(payload)

    def send_heal_complete(
        self,
        incident_id: str,
        environment: str,
        success: bool,
        actions: list[dict],
        duration_seconds: float | None = None,
    ) -> bool:
        icon = "✅" if success else "⚠️"
        color = "#2eb886" if success else "#f2c94c"

        action_rows = "\n".join(
            f"• `{a['action']}` on `{a['target']}` → {'✅ OK' if a['success'] else '❌ FAILED'}"
            + (f"\n  _{a.get('error', '')}_" if not a['success'] and a.get('error') else "")
            for a in actions
        )

        payload = {
            "attachments": [
                {
                    "color": color,
                    "blocks": [
                        {
                            "type": "header",
                            "text": {
                                "type": "plain_text",
                                "text": f"{icon} Auto-Heal {'Complete' if success else 'Partially Failed'}",
                                "emoji": True,
                            },
                        },
                        {
                            "type": "section",
                            "fields": [
                                {"type": "mrkdwn", "text": f"*Incident:*\n`{incident_id}`"},
                                {"type": "mrkdwn", "text": f"*Environment:*\n`{environment}`"},
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Duration:*\n{f'{duration_seconds:.1f}s' if duration_seconds else 'N/A'}",
                                },
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Actions:*\n{len(actions)} executed",
                                },
                            ],
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Recovery Actions:*\n{action_rows or '_No actions taken_'}",
                            },
                        },
                    ],
                    "fallback": f"Auto-heal {incident_id}: {'success' if success else 'partial failure'}",
                }
            ]
        }

        return self._send(payload)

    def _send(self, payload: dict) -> bool:
        try:
            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                logger.error(f"Slack returned {resp.status_code}: {resp.text}")
                return False
            return True
        except Exception as e:
            logger.error(f"Failed to send Slack alert: {e}")
            return False


class AlertManager:
    """Fetches webhook from SSM and dispatches to Slack + SNS."""

    def __init__(self, region: str):
        self.region = region
        self.ssm = boto3.client("ssm", region_name=region)
        self._slack: SlackAlerter | None = None

    @property
    def slack(self) -> SlackAlerter | None:
        if self._slack is None:
            webhook_url = self._get_ssm_param(
                os.environ.get("SLACK_WEBHOOK_SSM", "")
            )
            if webhook_url:
                self._slack = SlackAlerter(webhook_url)
        return self._slack

    def _get_ssm_param(self, param_name: str) -> str | None:
        if not param_name:
            return None
        try:
            resp = self.ssm.get_parameter(Name=param_name, WithDecryption=True)
            return resp["Parameter"]["Value"]
        except ClientError:
            logger.warning(f"SSM parameter '{param_name}' not found")
            return None

    def alert_failure(self, health_event: dict) -> None:
        environment = health_event.get("environment", "unknown")
        status = health_event.get("overall_status", "unknown")
        failing = health_event.get("failing_checks", [])
        dashboard_url = self._build_dashboard_url(environment)

        if self.slack:
            self.slack.send_incident_alert(
                environment=environment,
                overall_status=status,
                failing_checks=failing,
                incident_id=health_event.get("incident_id"),
                dashboard_url=dashboard_url,
            )

    def alert_healed(self, heal_report: dict) -> None:
        if self.slack:
            self.slack.send_heal_complete(
                incident_id=heal_report.get("incident_id", "N/A"),
                environment=heal_report.get("environment", "unknown"),
                success=heal_report.get("overall_success", False),
                actions=heal_report.get("actions_taken", []),
            )

    def _build_dashboard_url(self, environment: str) -> str:
        project = os.environ.get("PROJECT_NAME", "self-healing-infra")
        return (
            f"https://{self.region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={self.region}#dashboards:name={project}-{environment}"
        )
