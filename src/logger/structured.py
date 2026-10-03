"""
src/logger/structured.py — production structured logging
  - JSON lines (queryable in CloudWatch Logs Insights / Datadog)
  - Correlation IDs on every line via thread-local context
  - PII auto-scrub (IPs, emails, tokens)
  - Gzip NDJSON shipping to S3 for long-term audit retention
"""
from __future__ import annotations
import gzip, json, logging, re, threading, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
import boto3

_ctx = threading.local()
_PII = [
    (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'), "[EMAIL]"),
    (re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'), "[IP]"),
    (re.compile(r'(?i)(password|secret|token|key|credential)(["\s:=]+)\S+'), r'\1\2[REDACTED]'),
]

def _scrub(t: str) -> str:
    for p, r in _PII: t = p.sub(r, t)
    return t

def get_correlation_id() -> str:
    return getattr(_ctx, "correlation_id", "-")

@contextmanager
def request_context(correlation_id: str | None = None, **extra):
    _ctx.correlation_id = correlation_id or str(uuid.uuid4())
    _ctx.extra = extra
    try: yield _ctx.correlation_id
    finally: _ctx.correlation_id = "-"; _ctx.extra = {}

class JSONFormatter(logging.Formatter):
    def __init__(self, service: str, environment: str):
        super().__init__(); self.service = service; self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.exc_info: msg += "\n" + self.formatException(record.exc_info)
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname, "logger": record.name, "msg": _scrub(msg),
            "service": self.service, "env": self.environment,
            "correlation": get_correlation_id(),
            "loc": f"{record.module}.{record.funcName}:{record.lineno}",
        }
        entry.update(getattr(_ctx, "extra", {}))
        skip = {"name","msg","args","levelname","levelno","pathname","filename","module",
                "exc_info","exc_text","stack_info","lineno","funcName","created","msecs",
                "relativeCreated","thread","threadName","processName","process","message","taskName"}
        for k, v in record.__dict__.items():
            if k not in skip and not k.startswith("_"): entry[k] = v
        return json.dumps(entry, default=str)

class _S3Shipper:
    def __init__(self, bucket: str, prefix: str, region: str):
        self.bucket = bucket; self.prefix = prefix.rstrip("/")
        self._s3 = boto3.client("s3", region_name=region)
        self._buf: list[str] = []; self._lock = threading.Lock()

    def add(self, line: str) -> None:
        with self._lock: self._buf.append(line)

    def flush(self, service: str, environment: str) -> None:
        with self._lock:
            if not self._buf: return
            lines, self._buf = self._buf[:], []
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        key = (f"{self.prefix}/{environment}/{service}/{now.strftime('%Y/%m/%d/%H')}/"
               f"{now.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.ndjson.gz")
        try:
            self._s3.put_object(Bucket=self.bucket, Key=key,
                Body=gzip.compress("\n".join(lines).encode()),
                ContentType="application/x-ndjson", ContentEncoding="gzip",
                ServerSideEncryption="aws:kms")
        except Exception as e:
            logging.getLogger(__name__).error(f"S3 log ship failed: {e}")

class _S3Handler(logging.Handler):
    def __init__(self, shipper: _S3Shipper):
        super().__init__(level=logging.WARNING); self._shipper = shipper
    def emit(self, record):
        try: self._shipper.add(self.format(record))
        except Exception: self.handleError(record)

def setup_logging(service: str, environment: str, level: str = "INFO",
                  s3_bucket: str | None = None, s3_prefix: str = "logs",
                  region: str = "us-east-1") -> _S3Shipper | None:
    root = logging.getLogger(); root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = JSONFormatter(service=service, environment=environment)
    h = logging.StreamHandler(); h.setFormatter(fmt); root.addHandler(h)
    shipper = None
    if s3_bucket:
        shipper = _S3Shipper(bucket=s3_bucket, prefix=s3_prefix, region=region)
        sh = _S3Handler(shipper); sh.setFormatter(fmt); root.addHandler(sh)
    for lib in ("urllib3","boto3","botocore","s3transfer","requests"):
        logging.getLogger(lib).setLevel(logging.WARNING)
    return shipper
