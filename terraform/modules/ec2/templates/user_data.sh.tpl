#!/bin/bash
# terraform/modules/ec2/templates/user_data.sh.tpl
# Bootstrap script for self-healing app servers

set -euo pipefail
exec > >(tee /var/log/user-data.log | logger -t user-data) 2>&1

echo "=== Starting bootstrap for ${project_name} (${environment}) ==="

# ─── System Updates ──────────────────────────────────────────────
dnf update -y
dnf install -y python3.11 python3.11-pip python3-devel gcc git jq awscli

# ─── CloudWatch Agent ────────────────────────────────────────────
dnf install -y amazon-cloudwatch-agent

cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json << 'CWCONFIG'
{
  "agent": {
    "metrics_collection_interval": 30,
    "run_as_user": "cwagent"
  },
  "metrics": {
    "namespace": "${project_name}/EC2",
    "metrics_collected": {
      "mem": {
        "measurement": ["mem_used_percent"],
        "metrics_collection_interval": 30
      },
      "disk": {
        "measurement": ["disk_used_percent"],
        "metrics_collection_interval": 60,
        "resources": ["/"]
      },
      "cpu": {
        "measurement": ["cpu_usage_active"],
        "metrics_collection_interval": 30
      }
    }
  },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/app/*.log",
            "log_group_name": "/aws/ec2/${project_name}/${environment}/app",
            "log_stream_name": "{instance_id}",
            "timestamp_format": "%Y-%m-%dT%H:%M:%S"
          },
          {
            "file_path": "/var/log/user-data.log",
            "log_group_name": "/aws/ec2/${project_name}/${environment}/bootstrap",
            "log_stream_name": "{instance_id}"
          }
        ]
      }
    }
  }
}
CWCONFIG

systemctl enable amazon-cloudwatch-agent
systemctl start amazon-cloudwatch-agent

# ─── Application Setup ────────────────────────────────────────────
mkdir -p /opt/app /var/log/app
useradd -r -s /sbin/nologin appuser || true
chown appuser:appuser /opt/app /var/log/app

# ─── Sample App (replace with your real app deployment) ──────────
cat > /opt/app/app.py << 'APPEOF'
#!/usr/bin/env python3
"""
Simple health-check-aware application server.
Replace this with your actual application.
"""
import http.server
import json
import os
import time
import socket
import subprocess
from datetime import datetime

START_TIME = time.time()
REQUEST_COUNT = 0

class HealthAwareHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global REQUEST_COUNT
        REQUEST_COUNT += 1

        if self.path == "/health":
            self._health_check()
        elif self.path == "/metrics":
            self._metrics()
        elif self.path == "/":
            self._root()
        else:
            self.send_response(404)
            self.end_headers()

    def _health_check(self):
        uptime = time.time() - START_TIME
        checks = {
            "status": "healthy",
            "uptime_seconds": round(uptime, 2),
            "hostname": socket.gethostname(),
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "checks": {
                "app": "ok",
                "disk": self._check_disk(),
                "memory": self._check_memory()
            }
        }
        # Fail if any check is unhealthy
        if any(v != "ok" for v in checks["checks"].values()):
            checks["status"] = "degraded"

        code = 200 if checks["status"] == "healthy" else 503
        body = json.dumps(checks).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _check_disk(self):
        import shutil
        usage = shutil.disk_usage("/")
        pct = usage.used / usage.total * 100
        return "ok" if pct < 85 else "critical"

    def _check_memory(self):
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            total = int(lines[0].split()[1])
            available = int(lines[2].split()[1])
            pct_used = (1 - available / total) * 100
            return "ok" if pct_used < 90 else "critical"
        except Exception:
            return "unknown"

    def _metrics(self):
        body = json.dumps({
            "requests_total": REQUEST_COUNT,
            "uptime_seconds": round(time.time() - START_TIME, 2)
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _root(self):
        body = b'{"message": "Self-Healing App Running"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f'{datetime.utcnow().isoformat()} [{self.address_string()}] {format % args}')

if __name__ == "__main__":
    port = int(os.environ.get("APP_PORT", "${app_port}"))
    server = http.server.HTTPServer(("", port), HealthAwareHandler)
    print(f"Starting app server on port {port}")
    server.serve_forever()
APPEOF

# ─── Systemd Service ──────────────────────────────────────────────
cat > /etc/systemd/system/app.service << SVCEOF
[Unit]
Description=${project_name} Application
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=appuser
WorkingDirectory=/opt/app
Environment=APP_PORT=${app_port}
Environment=ENVIRONMENT=${environment}
Environment=AWS_DEFAULT_REGION=${region}
ExecStart=/usr/bin/python3.11 /opt/app/app.py
Restart=always
RestartSec=5
StandardOutput=append:/var/log/app/app.log
StandardError=append:/var/log/app/app-error.log

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable app
systemctl start app

# ─── Signal Success ───────────────────────────────────────────────
INSTANCE_ID=$(curl -sf http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo "unknown")
aws cloudwatch put-metric-data \
  --region "${region}" \
  --namespace "${project_name}/Bootstrap" \
  --metric-name "BootstrapSuccess" \
  --value 1 \
  --dimensions "Environment=${environment},InstanceId=$INSTANCE_ID" \
  2>/dev/null || true

echo "=== Bootstrap complete for $INSTANCE_ID ==="
