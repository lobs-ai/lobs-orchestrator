"""
Tests for the retry manager.
"""

import pytest
from datetime import datetime, timezone

from orchestrator.core.retry import RetryManager, RetryPattern, MAX_RETRY_COUNT


@pytest.fixture
def retry_manager():
    return RetryManager()


@pytest.fixture
def base_task():
    """Base task dictionary for testing."""
    return {
        "id": "test-task-123",
        "projectId": "test-project",
        "title": "Test Task",
        "notes": "Original notes",
        "status": "active",
        "workState": "failed",
    }


def test_retry_pattern_matching():
    """Test that retry patterns correctly match error messages."""
    pattern = RetryPattern(
        name="test_pattern",
        patterns=[r"timeout", r"ETIMEDOUT", r"connection.*reset"],
        guidance="Test guidance",
    )
    
    assert pattern.matches("Connection timeout occurred")
    assert pattern.matches("ETIMEDOUT error")
    assert pattern.matches("connection was reset by peer")
    assert not pattern.matches("some other error")


def test_should_retry_network_timeout(retry_manager, base_task):
    """Test that network timeouts are eligible for retry."""
    error_log = "Error: Connection timeout after 30 seconds"
    
    should_retry, pattern_name, guidance = retry_manager.should_retry(base_task, error_log)
    
    assert should_retry is True
    assert pattern_name == "network_timeout"
    assert "Network timeout" in guidance


def test_should_retry_rate_limit(retry_manager, base_task):
    """Test that rate limit errors are eligible for retry."""
    error_log = "Error: 429 Too Many Requests - rate limit exceeded"
    
    should_retry, pattern_name, guidance = retry_manager.should_retry(base_task, error_log)
    
    assert should_retry is True
    assert pattern_name == "rate_limit"
    assert "Rate limit" in guidance


def test_should_retry_transient_api_error(retry_manager, base_task):
    """Test that transient API errors are eligible for retry."""
    error_log = "Error: 503 Service Unavailable"
    
    should_retry, pattern_name, guidance = retry_manager.should_retry(base_task, error_log)
    
    assert should_retry is True
    assert pattern_name == "transient_api_error"


def test_should_not_retry_max_count_reached(retry_manager, base_task):
    """Test that tasks with max retry count are not retried."""
    base_task["retryCount"] = MAX_RETRY_COUNT
    error_log = "Error: Connection timeout"
    
    should_retry, pattern_name, guidance = retry_manager.should_retry(base_task, error_log)
    
    assert should_retry is False
    assert pattern_name is None


def test_should_not_retry_unknown_error(retry_manager, base_task):
    """Test that unknown errors are not eligible for retry."""
    error_log = "Error: Some random unknown error that doesn't match patterns"
    
    should_retry, pattern_name, guidance = retry_manager.should_retry(base_task, error_log)
    
    assert should_retry is False
    assert pattern_name is None


def test_should_not_retry_empty_error_log(retry_manager, base_task):
    """Test that empty error logs don't trigger retry."""
    error_log = ""
    
    should_retry, pattern_name, guidance = retry_manager.should_retry(base_task, error_log)
    
    assert should_retry is False


def test_prevent_retry_loop_same_root_cause(retry_manager, base_task):
    """Test that retry loop prevention works for same root cause."""
    base_task["retryCount"] = 2
    base_task["retryHistory"] = [
        {"attempt": 1, "reason": "network_timeout"},
        {"attempt": 2, "reason": "network_timeout"},
    ]
    error_log = "Error: Connection timeout"
    
    should_retry, pattern_name, guidance = retry_manager.should_retry(base_task, error_log)
    
    # Should not retry because same pattern failed twice in a row
    assert should_retry is False


def test_allow_retry_different_root_cause(retry_manager, base_task):
    """Test that retry is allowed when root cause changes."""
    base_task["retryCount"] = 2
    base_task["retryHistory"] = [
        {"attempt": 1, "reason": "network_timeout"},
        {"attempt": 2, "reason": "rate_limit"},
    ]
    error_log = "Error: Connection timeout"
    
    should_retry, pattern_name, guidance = retry_manager.should_retry(base_task, error_log)
    
    # Should allow retry because root causes are different
    assert should_retry is True
    assert pattern_name == "network_timeout"


def test_prepare_retry_updates(retry_manager, base_task):
    """Test that retry preparation creates correct updates."""
    pattern_name = "network_timeout"
    guidance = "Network timeout detected. Retrying..."
    error_log = "Connection timeout after 30 seconds"
    
    updates = retry_manager.prepare_retry(base_task, pattern_name, guidance, error_log)
    
    assert updates["workState"] == "not_started"
    assert updates["status"] == "active"
    assert updates["retryCount"] == 1
    assert updates["lastRetryReason"] == pattern_name
    assert updates["failureReason"] is None
    assert "Retry #1" in updates["notes"]
    assert guidance in updates["notes"]
    assert "Connection timeout" in updates["notes"]
    assert len(updates["retryHistory"]) == 1
    
    history_entry = updates["retryHistory"][0]
    assert history_entry["attempt"] == 1
    assert history_entry["reason"] == pattern_name


def test_prepare_retry_increments_count(retry_manager, base_task):
    """Test that retry preparation increments retry count correctly."""
    base_task["retryCount"] = 1
    base_task["retryHistory"] = [
        {"attempt": 1, "reason": "rate_limit"}
    ]
    
    pattern_name = "network_timeout"
    guidance = "Network timeout detected. Retrying..."
    error_log = "Connection timeout"
    
    updates = retry_manager.prepare_retry(base_task, pattern_name, guidance, error_log)
    
    assert updates["retryCount"] == 2
    assert "Retry #2" in updates["notes"]
    assert len(updates["retryHistory"]) == 2


def test_prepare_retry_appends_to_notes(retry_manager, base_task):
    """Test that retry notes are appended to existing notes."""
    base_task["notes"] = "Original task notes\nwith multiple lines"
    
    pattern_name = "network_timeout"
    guidance = "Network timeout detected."
    error_log = "Timeout error"
    
    updates = retry_manager.prepare_retry(base_task, pattern_name, guidance, error_log)
    
    assert "Original task notes" in updates["notes"]
    assert "with multiple lines" in updates["notes"]
    assert "Retry #1" in updates["notes"]
    assert guidance in updates["notes"]


def test_format_retry_summary_no_retries(retry_manager, base_task):
    """Test retry summary formatting with no retries."""
    summary = retry_manager.format_retry_summary(base_task)
    
    assert "No retries" in summary


def test_format_retry_summary_with_history(retry_manager, base_task):
    """Test retry summary formatting with retry history."""
    base_task["retryCount"] = 2
    base_task["retryHistory"] = [
        {
            "attempt": 1,
            "reason": "network_timeout",
            "failedAt": "2024-01-01T12:00:00Z"
        },
        {
            "attempt": 2,
            "reason": "rate_limit",
            "failedAt": "2024-01-01T13:00:00Z"
        },
    ]
    
    summary = retry_manager.format_retry_summary(base_task)
    
    assert "2 attempts" in summary
    assert "#1: network_timeout" in summary
    assert "#2: rate_limit" in summary
    assert "2024-01-01T12:00:00Z" in summary


def test_missing_context_pattern_max_retries(retry_manager, base_task):
    """Test that missing context pattern has lower max retries."""
    error_log = "Error: No such file or directory: /path/to/file.py"
    
    # First retry should work
    should_retry, pattern_name, _ = retry_manager.should_retry(base_task, error_log)
    assert should_retry is True
    assert pattern_name == "missing_context"
    
    # Second retry should work
    base_task["retryCount"] = 1
    should_retry, pattern_name, _ = retry_manager.should_retry(base_task, error_log)
    assert should_retry is True
    
    # Third retry should NOT work (max_retries=2 for missing_context)
    base_task["retryCount"] = 2
    should_retry, pattern_name, _ = retry_manager.should_retry(base_task, error_log)
    assert should_retry is False


def test_lock_conflict_pattern(retry_manager, base_task):
    """Test that lock conflicts are eligible for retry."""
    error_log = "Error: could not acquire lock on resource /path/to/lock"
    
    should_retry, pattern_name, guidance = retry_manager.should_retry(base_task, error_log)
    
    assert should_retry is True
    assert pattern_name == "lock_conflict"
    assert "lock" in guidance.lower()
