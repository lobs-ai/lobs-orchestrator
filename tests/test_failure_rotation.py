import time

from orchestrator.core.failure_rotation import FailureRotation, FailurePolicy


def test_failure_rotation_skips_after_threshold(tmp_path):
    policy = FailurePolicy(threshold=3, cooldown_seconds=100)
    fr = FailureRotation(tmp_path, policy=policy, state_file=tmp_path / "fr.json")

    task_id = "task-1"
    assert fr.should_skip(task_id) is False

    fr.record_failure(task_id, now=10.0)
    assert fr.should_skip(task_id, now=10.0) is False

    fr.record_failure(task_id, now=11.0)
    assert fr.should_skip(task_id, now=11.0) is False

    entry = fr.record_failure(task_id, now=12.0)
    assert entry["streak"] == 3
    assert fr.should_skip(task_id, now=12.0) is True
    assert fr.should_skip(task_id, now=112.0) is False


def test_failure_rotation_resets_on_success(tmp_path):
    policy = FailurePolicy(threshold=2, cooldown_seconds=100)
    fr = FailureRotation(tmp_path, policy=policy, state_file=tmp_path / "fr.json")

    task_id = "task-2"
    fr.record_failure(task_id, now=1.0)
    fr.record_failure(task_id, now=2.0)
    assert fr.should_skip(task_id, now=2.0) is True

    fr.record_success(task_id)
    assert fr.should_skip(task_id, now=2.0) is False
    assert fr.get_entry(task_id) is None


def test_pick_retry_when_all_skipped(tmp_path):
    policy = FailurePolicy(threshold=1, cooldown_seconds=100)
    fr = FailureRotation(tmp_path, policy=policy, state_file=tmp_path / "fr.json")

    # task-a skipped until 200, task-b until 150 => should pick task-b
    fr.record_failure("task-a", now=100.0)
    fr._state["tasks"]["task-a"]["skip_until_ts"] = 200.0

    fr.record_failure("task-b", now=100.0)
    fr._state["tasks"]["task-b"]["skip_until_ts"] = 150.0

    assert fr.pick_retry_when_all_skipped(["task-a", "task-b"]) == "task-b"
