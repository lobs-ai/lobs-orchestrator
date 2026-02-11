"""
Retry manager for handling transient task failures.

This module provides logic for automatically retrying tasks when failures
are determined to be transient or fixable without human intervention.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Maximum number of retries before escalating to human
MAX_RETRY_COUNT = 3


class RetryPattern:
    """Represents a pattern that can trigger automatic retry."""
    
    def __init__(self, name: str, patterns: list[str], guidance: str, max_retries: int = MAX_RETRY_COUNT):
        self.name = name
        self.patterns = [re.compile(p, re.IGNORECASE) for p in patterns]
        self.guidance = guidance
        self.max_retries = max_retries
    
    def matches(self, error_text: str) -> bool:
        """Check if error text matches this pattern."""
        return any(pattern.search(error_text) for pattern in self.patterns)


# Define retry-eligible failure patterns
RETRY_PATTERNS = [
    RetryPattern(
        name="network_timeout",
        patterns=[
            r"timeout",
            r"timed out",
            r"connection.*reset",
            r"connection.*refused",
            r"network.*error",
            r"ETIMEDOUT",
            r"ECONNREFUSED",
            r"ECONNRESET",
        ],
        guidance="Network timeout detected. Retrying - the service may have been temporarily unavailable.",
    ),
    RetryPattern(
        name="rate_limit",
        patterns=[
            r"rate.?limit",
            r"too many requests",
            r"429",
            r"quota exceeded",
            r"throttl",
        ],
        guidance="Rate limit hit. Retrying after backoff - the API should be available again.",
    ),
    RetryPattern(
        name="transient_api_error",
        patterns=[
            r"502 bad gateway",
            r"503 service unavailable",
            r"504 gateway timeout",
            r"internal server error",
            r"temporary failure",
            r"service temporarily unavailable",
        ],
        guidance="Transient API error detected. Retrying - the service should recover.",
    ),
    RetryPattern(
        name="lock_conflict",
        patterns=[
            r"resource.*locked",
            r"lock.*timeout",
            r"could not acquire lock",
            r"lock.*held",
        ],
        guidance="Resource lock conflict. Retrying - the lock should be released.",
    ),
    RetryPattern(
        name="missing_context",
        patterns=[
            r"no such file or directory",
            r"file not found",
            r"module not found",
            r"cannot find module",
            r"import.*failed",
        ],
        guidance="Missing dependency or file. Check if dependencies were installed correctly or if files need to be created. Review the error and add any missing context to task notes before retrying.",
        max_retries=2,  # Fewer retries for context issues
    ),
]


class RetryManager:
    """Manages automatic retry logic for failed tasks."""
    
    def __init__(self):
        self.patterns = RETRY_PATTERNS
    
    def should_retry(self, task: dict[str, Any], error_log: str) -> tuple[bool, Optional[str], Optional[str]]:
        """
        Determine if a task should be retried based on the failure pattern.
        
        Args:
            task: Task dictionary with id, retryCount, etc.
            error_log: The error/failure message from the worker
        
        Returns:
            Tuple of (should_retry, pattern_name, guidance_message)
        """
        if not error_log:
            return False, None, None
        
        # Check retry count
        retry_count = task.get("retryCount") or 0
        if retry_count >= MAX_RETRY_COUNT:
            logger.info(f"Task {task.get('id')} has reached max retry count ({MAX_RETRY_COUNT})")
            return False, None, None
        
        # Check retry history first to prevent retrying same root cause twice in a row
        retry_history = task.get("retryHistory") or []
        if retry_history and len(retry_history) >= 2:
            # Get last two retry reasons
            recent_reasons = [entry.get("reason") for entry in retry_history[-2:]]
            
            # If the last two retries had the same reason, check if current error matches
            if len(recent_reasons) == 2 and recent_reasons[0] == recent_reasons[1]:
                # Check if current error matches the same pattern
                for pattern in self.patterns:
                    if pattern.matches(error_log) and pattern.name == recent_reasons[0]:
                        logger.info(
                            f"Task {task.get('id')} has same root cause as previous two retries "
                            f"({pattern.name}), preventing retry loop"
                        )
                        return False, None, None
        
        # Check for retry-eligible patterns
        for pattern in self.patterns:
            if pattern.matches(error_log):
                if retry_count >= pattern.max_retries:
                    logger.info(
                        f"Task {task.get('id')} has reached max retries for pattern {pattern.name} "
                        f"({pattern.max_retries})"
                    )
                    continue
                
                logger.info(
                    f"Task {task.get('id')} matches retry pattern: {pattern.name} "
                    f"(retry {retry_count + 1}/{pattern.max_retries})"
                )
                return True, pattern.name, pattern.guidance
        
        return False, None, None
    
    def prepare_retry(
        self,
        task: dict[str, Any],
        pattern_name: str,
        guidance: str,
        error_log: str
    ) -> dict[str, Any]:
        """
        Prepare task updates for retry.
        
        Args:
            task: Original task dictionary
            pattern_name: Name of the matched retry pattern
            guidance: Guidance message for the retry
            error_log: The error log from the failure
        
        Returns:
            Dictionary of updates to apply to the task
        """
        retry_count = (task.get("retryCount") or 0) + 1
        
        # Build retry history entry
        retry_entry = {
            "attempt": retry_count,
            "reason": pattern_name,
            "failedAt": task.get("failedAt") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "errorPreview": error_log[:500] if error_log else None,
        }
        
        retry_history = list(task.get("retryHistory") or [])
        retry_history.append(retry_entry)
        
        # Append guidance to notes
        current_notes = task.get("notes") or ""
        retry_note = f"\n\n---\n**Retry #{retry_count}** ({pattern_name}):\n{guidance}\n\nPrevious error:\n```\n{error_log[:500]}\n```"
        updated_notes = current_notes + retry_note
        
        updates = {
            "workState": "not_started",
            "status": "active",
            "retryCount": retry_count,
            "lastRetryReason": pattern_name,
            "retryHistory": retry_history,
            "notes": updated_notes,
            "failureReason": None,  # Clear failure reason for retry
        }
        
        logger.info(
            f"Prepared retry for task {task.get('id')}: "
            f"attempt {retry_count}, pattern {pattern_name}"
        )
        
        return updates
    
    def format_retry_summary(self, task: dict[str, Any]) -> str:
        """Generate a human-readable summary of retry history for a task."""
        retry_count = task.get("retryCount") or 0
        if retry_count == 0:
            return "No retries yet"
        
        retry_history = task.get("retryHistory") or []
        if not retry_history:
            return f"{retry_count} retries (no details)"
        
        lines = [f"Retry history ({retry_count} attempts):"]
        for entry in retry_history:
            attempt = entry.get("attempt", "?")
            reason = entry.get("reason", "unknown")
            failed_at = entry.get("failedAt", "unknown time")
            lines.append(f"  #{attempt}: {reason} at {failed_at}")
        
        return "\n".join(lines)


# Global retry manager instance
_retry_manager = None


def get_retry_manager() -> RetryManager:
    """Get or create the global retry manager instance."""
    global _retry_manager
    if _retry_manager is None:
        _retry_manager = RetryManager()
    return _retry_manager
