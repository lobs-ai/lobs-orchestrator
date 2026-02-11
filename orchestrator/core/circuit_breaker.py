"""orchestrator.core.circuit_breaker

Infrastructure health detection and circuit breaker.

Detects infrastructure failures (gateway auth, session locks, missing API keys,
service unavailable) and pauses task spawning until the issue is resolved.
Prevents wasting retries on tasks that fail due to infrastructure, not task logic.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Patterns that indicate infrastructure failure, not task failure
INFRA_FAILURE_PATTERNS = [
    (re.compile(r"gateway.*token.*mismatch|unauthorized.*gateway", re.IGNORECASE), "gateway_auth"),
    (re.compile(r"session file locked", re.IGNORECASE), "session_lock"),
    (re.compile(r"No API key found for provider", re.IGNORECASE), "missing_api_key"),
    (re.compile(r"connect failed.*unauthorized", re.IGNORECASE), "gateway_auth"),
    (re.compile(r"ECONNREFUSED|ETIMEDOUT|ENOTFOUND", re.IGNORECASE), "service_unavailable"),
    (re.compile(r"All models failed", re.IGNORECASE), "all_models_failed"),
    (re.compile(r"FailoverError", re.IGNORECASE), "failover_exhausted"),
    (re.compile(r"rate.?limit|429|too many requests", re.IGNORECASE), "rate_limited"),
]


@dataclass
class CircuitState:
    """Tracks circuit breaker state."""
    is_open: bool = False  # True = paused, no spawning
    reason: str = ""
    opened_at: float = 0.0
    consecutive_infra_failures: int = 0
    last_failure_at: float = 0.0
    last_failure_type: str = ""
    # Auto-close after this many seconds to retry
    cooldown_seconds: float = 120.0  # 2 minutes
    # Open circuit after this many consecutive infra failures
    threshold: int = 3


class CircuitBreaker:
    """Detects infrastructure failures and pauses spawning to avoid wasting retries."""

    def __init__(self, threshold: int = 3, cooldown_seconds: float = 120.0):
        self.state = CircuitState(threshold=threshold, cooldown_seconds=cooldown_seconds)

    def classify_failure(self, error_log: str, failure_reason: str = "") -> tuple[bool, str]:
        """Classify whether a failure is infrastructure-related.
        
        Returns (is_infra, infra_type).
        """
        text = f"{failure_reason}\n{error_log}"
        for pattern, infra_type in INFRA_FAILURE_PATTERNS:
            if pattern.search(text):
                return True, infra_type
        return False, ""

    def record_failure(self, error_log: str, failure_reason: str = "") -> bool:
        """Record a failure and update circuit state.
        
        Returns True if this was classified as an infrastructure failure.
        """
        is_infra, infra_type = self.classify_failure(error_log, failure_reason)
        
        if is_infra:
            self.state.consecutive_infra_failures += 1
            self.state.last_failure_at = time.time()
            self.state.last_failure_type = infra_type
            
            logger.warning(
                f"[CIRCUIT] Infrastructure failure detected: {infra_type} "
                f"(consecutive: {self.state.consecutive_infra_failures}/{self.state.threshold})"
            )
            
            if self.state.consecutive_infra_failures >= self.state.threshold and not self.state.is_open:
                self._open_circuit(infra_type)
            
            return True
        else:
            # Task-level failure — reset consecutive counter
            self.state.consecutive_infra_failures = 0
            return False

    def record_success(self) -> None:
        """Record a successful task completion. Resets circuit state."""
        if self.state.is_open:
            logger.info("[CIRCUIT] Circuit closing — task succeeded")
        self.state.consecutive_infra_failures = 0
        self.state.is_open = False
        self.state.reason = ""

    def should_allow_spawn(self) -> tuple[bool, str]:
        """Check if spawning is allowed.
        
        Returns (allowed, reason).
        """
        if not self.state.is_open:
            return True, ""
        
        # Check if cooldown has elapsed — allow a probe
        elapsed = time.time() - self.state.opened_at
        if elapsed >= self.state.cooldown_seconds:
            logger.info(
                f"[CIRCUIT] Cooldown elapsed ({elapsed:.0f}s), allowing probe spawn"
            )
            return True, ""
        
        remaining = self.state.cooldown_seconds - elapsed
        reason = (
            f"Circuit breaker OPEN: {self.state.reason}. "
            f"Paused for {remaining:.0f}s more. "
            f"({self.state.consecutive_infra_failures} consecutive infra failures)"
        )
        return False, reason

    def _open_circuit(self, infra_type: str) -> None:
        """Open the circuit breaker — pause all spawning."""
        self.state.is_open = True
        self.state.opened_at = time.time()
        self.state.reason = infra_type
        
        logger.error(
            f"[CIRCUIT] ⚠️ CIRCUIT BREAKER OPEN — {infra_type}. "
            f"Pausing all task spawning for {self.state.cooldown_seconds}s. "
            f"({self.state.consecutive_infra_failures} consecutive infrastructure failures)"
        )

    @property
    def is_open(self) -> bool:
        return self.state.is_open

    def status(self) -> dict:
        return {
            "is_open": self.state.is_open,
            "reason": self.state.reason,
            "consecutive_infra_failures": self.state.consecutive_infra_failures,
            "last_failure_type": self.state.last_failure_type,
            "cooldown_remaining": max(0, self.state.cooldown_seconds - (time.time() - self.state.opened_at)) if self.state.is_open else 0,
        }
