from orchestrator.core.failure_rotation import FailureRotation, FailurePolicy


def test_failure_backoff_exponential_schedule(tmp_path):
    # Use small numbers for the test.
    policy = FailurePolicy(base_seconds=10, second_seconds=50, factor=3, max_backoff_seconds=10_000)
    fr = FailureRotation(tmp_path, policy=policy, state_file=tmp_path / "fr.json")

    task_id = "task-1"
    assert fr.should_skip(task_id, now=0.0) is False

    # 1st failure: 10s backoff
    entry1 = fr.record_failure(task_id, now=100.0)
    assert entry1["retry_count"] == 1
    assert fr.should_skip(task_id, now=109.0) is True
    assert fr.should_skip(task_id, now=110.0) is False

    # 2nd failure: 50s backoff
    entry2 = fr.record_failure(task_id, now=200.0)
    assert entry2["retry_count"] == 2
    assert fr.should_skip(task_id, now=249.0) is True
    assert fr.should_skip(task_id, now=250.0) is False

    # 3rd failure: 50 * 3 = 150s backoff
    entry3 = fr.record_failure(task_id, now=300.0)
    assert entry3["retry_count"] == 3
    assert fr.should_skip(task_id, now=449.0) is True
    assert fr.should_skip(task_id, now=450.0) is False


def test_failure_backoff_resets_on_success(tmp_path):
    policy = FailurePolicy(base_seconds=10, second_seconds=50, factor=3, max_backoff_seconds=10_000)
    fr = FailureRotation(tmp_path, policy=policy, state_file=tmp_path / "fr.json")

    task_id = "task-2"
    fr.record_failure(task_id, now=1.0)
    assert fr.should_skip(task_id, now=5.0) is True

    fr.record_success(task_id)
    assert fr.should_skip(task_id, now=5.0) is False
    assert fr.get_entry(task_id) is None


def test_pick_retry_when_all_skipped(tmp_path):
    policy = FailurePolicy(base_seconds=10, second_seconds=50, factor=3, max_backoff_seconds=10_000)
    fr = FailureRotation(tmp_path, policy=policy, state_file=tmp_path / "fr.json")

    # task-a skipped until 200, task-b until 150 => should pick task-b
    fr.record_failure("task-a", now=100.0)
    fr._state["tasks"]["task-a"]["skip_until_ts"] = 200.0

    fr.record_failure("task-b", now=100.0)
    fr._state["tasks"]["task-b"]["skip_until_ts"] = 150.0

    assert fr.pick_retry_when_all_skipped(["task-a", "task-b"]) == "task-b"
