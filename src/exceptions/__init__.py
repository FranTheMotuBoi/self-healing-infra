"""
src/exceptions/__init__.py

Typed exception hierarchy. Catching SelfHealingError catches everything.
Callers can catch specific subclasses for fine-grained handling.
"""


class SelfHealingError(Exception):
    """Base class for all self-healing infrastructure errors."""


# ── Config errors ─────────────────────────────────────────────────
class ConfigError(SelfHealingError):
    """Invalid or missing configuration."""

class MissingConfigError(ConfigError):
    """A required config key is absent."""

class InvalidConfigError(ConfigError):
    """A config value fails validation."""


# ── Check errors ──────────────────────────────────────────────────
class CheckError(SelfHealingError):
    """Error while executing a health check."""

class CheckTimeoutError(CheckError):
    """Health check exceeded its timeout."""

class CheckConnectionError(CheckError):
    """Could not connect to the target."""


# ── Heal errors ───────────────────────────────────────────────────
class HealError(SelfHealingError):
    """Error while executing a recovery action."""

class CircuitOpenError(HealError):
    """Healing blocked because the circuit breaker is open."""

    def __init__(self, resource_id: str):
        self.resource_id = resource_id
        super().__init__(f"Circuit open for resource: {resource_id}")

class CooldownActiveError(HealError):
    """Healing suppressed because a recent heal is still in cooldown."""

    def __init__(self, resource_id: str, remaining_seconds: float):
        self.resource_id = resource_id
        self.remaining_seconds = remaining_seconds
        super().__init__(
            f"Heal cooldown active for {resource_id}: "
            f"{remaining_seconds:.0f}s remaining"
        )

class SSMCommandError(HealError):
    """SSM Run Command failed or timed out."""

class InstanceReplacementError(HealError):
    """EC2 instance termination/replacement failed."""


# ── Alert errors ──────────────────────────────────────────────────
class AlertError(SelfHealingError):
    """Failed to send an alert."""

class SlackAlertError(AlertError):
    """Slack webhook returned an error."""

class PagerDutyAlertError(AlertError):
    """PagerDuty API returned an error."""


# ── Storage errors ────────────────────────────────────────────────
class StorageError(SelfHealingError):
    """Failed to read/write persistent state."""

class DynamoDBError(StorageError):
    """DynamoDB operation failed."""

class S3Error(StorageError):
    """S3 operation failed."""
