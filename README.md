# 🔧 Self-Healing Cloud Infrastructure System

A production-grade AWS infrastructure that automatically detects failures, heals itself, and alerts your team — all without human intervention.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    Self-Healing Pipeline                        │
│                                                                 │
│  [Health Checker] → [Failure Detector] → [Auto Healer]         │
│         ↓                   ↓                  ↓               │
│  [CloudWatch]         [SNS Alerts]      [SSM Automation]       │
│         ↓                   ↓                  ↓               │
│    [S3 Logs]           [PagerDuty]       [EC2/ECS/RDS]         │
└─────────────────────────────────────────────────────────────────┘
```

## Stack

| Layer | Technology |
|-------|-----------|
| Infrastructure | Terraform (AWS) |
| Runtime | Python 3.11 |
| Compute | EC2 Auto Scaling Groups |
| Monitoring | CloudWatch + Custom Metrics |
| Alerting | SNS + Slack Webhook |
| Secrets | AWS Secrets Manager |
| Logging | CloudWatch Logs + S3 |
| Orchestration | AWS Lambda + EventBridge |

## Features

- ✅ **Continuous Health Checks** — HTTP, TCP, process, disk, memory
- ✅ **Auto Restart** — Restarts failed services via SSM Run Command
- ✅ **Instance Replacement** — Terminates unhealthy EC2s, ASG launches fresh ones
- ✅ **RDS Failover** — Triggers Multi-AZ failover on primary DB failure
- ✅ **Slack + Email Alerts** — Rich notifications with incident context
- ✅ **Audit Logging** — Every heal action logged to S3 + CloudWatch
- ✅ **Dry Run Mode** — Test without making real changes
- ✅ **Terraform IaC** — Entire infra reproducible in one command

## Quick Start

```bash
# 1. Clone and setup
git clone <repo>
cd self-healing-infra
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Configure AWS
aws configure
cp .env.example .env  # Fill in your values

# 3. Deploy infrastructure
cd terraform/environments/dev
terraform init
terraform plan
terraform apply

# 4. Run health checker locally
python -m src.health_checker.main --config config/dev.yaml

# 5. Deploy as Lambda (automated)
./scripts/deploy_lambda.sh dev
```

## Project Structure

```
self-healing-infra/
├── terraform/
│   ├── modules/
│   │   ├── ec2/          # EC2 + ASG + Launch Template
│   │   ├── alb/          # Application Load Balancer
│   │   ├── rds/          # RDS Multi-AZ
│   │   └── monitoring/   # CloudWatch + Alarms + Dashboards
│   └── environments/
│       ├── dev/
│       └── prod/
├── src/
│   ├── health_checker/   # Checks all endpoints/services
│   ├── healer/           # Executes recovery actions
│   ├── alerter/          # Sends Slack/email notifications
│   └── logger/           # Structured audit logging
├── tests/                # Unit + integration tests
├── scripts/              # Deploy, rollback, smoke-test scripts
└── dashboard/            # Local monitoring dashboard
```
