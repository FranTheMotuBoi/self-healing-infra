"""
src/circuit_breaker/breaker.py — DynamoDB-backed circuit breaker

States: CLOSED (heals allowed) → OPEN (blocked) → HALF_OPEN (one trial)
"""
from __future__ import annotations
import logging, time
from dataclasses import dataclass
import boto3
from botocore.exceptions import ClientError
from src.models.domain import CircuitState

logger = logging.getLogger(__name__)

@dataclass
class CircuitStatus:
    resource_id: str; state: CircuitState; failure_count: int
    opened_at: float | None; last_failure: float | None; last_success: float | None

class CircuitBreaker:
    def __init__(self, table_name: str, region: str = "us-east-1",
                 failure_threshold: int = 5, reset_timeout_s: int = 300):
        self._table_name = table_name; self._thresh = failure_threshold
        self._timeout = reset_timeout_s
        self._ddb = boto3.resource("dynamodb", region_name=region)
        self._table = self._ddb.Table(table_name)

    def allow_request(self, rid: str) -> bool:
        s = self._load(rid); now = time.time()
        if s.state == CircuitState.CLOSED: return True
        if s.state == CircuitState.OPEN:
            if s.opened_at and (now - s.opened_at) >= self._timeout:
                self._set_state(rid, CircuitState.HALF_OPEN); return True
            logger.warning(f"Circuit OPEN for {rid}"); return False
        return True  # HALF_OPEN

    def record_success(self, rid: str) -> None:
        s = self._load(rid)
        if s.state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            logger.info(f"Circuit CLOSING for {rid}"); self._reset(rid)
        else:
            self._update(rid, failure_count=max(0, s.failure_count - 1))

    def record_failure(self, rid: str) -> None:
        s = self._load(rid); now = time.time(); n = s.failure_count + 1
        if s.state == CircuitState.HALF_OPEN or n >= self._thresh:
            logger.error(f"Circuit OPENING for {rid} ({n}/{self._thresh})")
            self._open(rid, n, now)
        else:
            logger.warning(f"Failure {n}/{self._thresh} for {rid}")
            self._update(rid, failure_count=n, last_failure=now)

    def get_state(self, rid: str) -> CircuitState: return self._load(rid).state
    def force_reset(self, rid: str) -> None: self._reset(rid)

    def _load(self, rid: str) -> CircuitStatus:
        try:
            item = self._table.get_item(Key={"resource_id": rid}).get("Item")
            if not item: return CircuitStatus(rid, CircuitState.CLOSED, 0, None, None, None)
            return CircuitStatus(rid, CircuitState(item.get("state","CLOSED")),
                int(item.get("failure_count",0)),
                float(item["opened_at"]) if item.get("opened_at") else None,
                float(item["last_failure"]) if item.get("last_failure") else None,
                float(item["last_success"]) if item.get("last_success") else None)
        except ClientError as e:
            logger.error(f"DynamoDB read failed: {e}")
            return CircuitStatus(rid, CircuitState.CLOSED, 0, None, None, None)

    def _open(self, rid: str, count: int, now: float) -> None:
        try:
            self._table.put_item(Item={"resource_id": rid, "state": CircuitState.OPEN,
                "failure_count": count, "opened_at": str(now), "last_failure": str(now),
                "ttl": int(now) + 86400})
        except ClientError as e: logger.error(f"Circuit open failed: {e}")

    def _set_state(self, rid: str, state: CircuitState) -> None:
        try:
            self._table.update_item(Key={"resource_id": rid},
                UpdateExpression="SET #s = :s",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={":s": state.value})
        except ClientError as e: logger.error(f"Set state failed: {e}")

    def _update(self, rid: str, **fields) -> None:
        if not fields: return
        try:
            self._table.update_item(Key={"resource_id": rid},
                UpdateExpression="SET " + ", ".join(f"#{k}=:{k}" for k in fields),
                ExpressionAttributeNames={f"#{k}": k for k in fields},
                ExpressionAttributeValues={f":{k}": str(v) for k, v in fields.items()})
        except ClientError as e: logger.error(f"Update failed: {e}")

    def _reset(self, rid: str) -> None:
        now = time.time()
        try:
            self._table.put_item(Item={"resource_id": rid, "state": CircuitState.CLOSED,
                "failure_count": 0, "last_success": str(now), "ttl": int(now) + 86400})
        except ClientError as e: logger.error(f"Reset failed: {e}")

class NullCircuitBreaker:
    def allow_request(self, rid): return True
    def record_success(self, rid): pass
    def record_failure(self, rid): pass
    def get_state(self, rid): return CircuitState.CLOSED
    def force_reset(self, rid): pass

def get_circuit_breaker(table: str | None, region: str = "us-east-1",
                        threshold: int = 5, timeout_s: int = 300):
    if not table: return NullCircuitBreaker()
    return CircuitBreaker(table, region, threshold, timeout_s)
