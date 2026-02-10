from __future__ import annotations

from orchestrator.core.workflow import WorkflowEngine


def test_workflow_groups_initiatives_and_progress(tmp_path):
    engine = WorkflowEngine(state_path=tmp_path / "wf.json")

    tasks = [
        {
            "id": "t1",
            "kind": "task",
            "title": "Design",
            "initiative": "auth",
            "agent": "architect",
            "workState": "completed",
        },
        {
            "id": "t2",
            "kind": "task",
            "title": "User model",
            "initiative": "auth",
            "agent": "programmer",
            "workState": "completed",
            "collaboration": {"parentTaskId": "t1"},
        },
        {
            "id": "t3",
            "kind": "task",
            "title": "JWT middleware",
            "initiative": "auth",
            "agent": "programmer",
            "workState": "in_progress",
            "collaboration": {"parentTaskId": "t1"},
        },
        {
            "id": "t4",
            "kind": "task",
            "title": "Login endpoint",
            "initiative": "auth",
            "agent": "programmer",
            "workState": "not_started",
            "dependsOn": ["t3"],
        },
        # Different initiative
        {
            "id": "x1",
            "kind": "task",
            "title": "Unrelated",
            "initiative": "other",
            "workState": "completed",
        },
    ]

    snapshot = engine.compute(tasks)

    auth = snapshot.initiatives["auth"]
    assert auth.total == 4
    assert auth.done == 2
    assert auth.complete is False

    other = snapshot.initiatives["other"]
    assert other.total == 1
    assert other.done == 1
    assert other.complete is True


def test_workflow_detects_blocked_tasks_from_dependencies(tmp_path):
    engine = WorkflowEngine(state_path=tmp_path / "wf.json")

    tasks = [
        {"id": "a", "title": "A", "initiative": "i", "workState": "completed"},
        {"id": "b", "title": "B", "initiative": "i", "workState": "not_started", "dependsOn": ["a"]},
        {"id": "c", "title": "C", "initiative": "i", "workState": "not_started", "dependsOn": ["b"]},
    ]

    snapshot = engine.compute(tasks)

    # b is unblocked because a is done; c is blocked because b isn't.
    assert "b" not in snapshot.blocked_task_ids
    assert "c" in snapshot.blocked_task_ids


def test_workflow_parent_task_id_is_a_dependency_and_visualizes_tree(tmp_path):
    engine = WorkflowEngine(state_path=tmp_path / "wf.json")

    tasks = [
        {"id": "root", "title": "Root", "initiative": "i", "agent": "architect", "workState": "completed"},
        {
            "id": "child",
            "title": "Child",
            "initiative": "i",
            "agent": "programmer",
            "workState": "in_progress",
            "collaboration": {"parentTaskId": "root"},
        },
        {
            "id": "grand",
            "title": "Grand",
            "initiative": "i",
            "agent": "reviewer",
            "workState": "not_started",
            "dependsOn": ["child"],
        },
    ]

    snapshot = engine.compute(tasks)

    assert "grand" in snapshot.blocked_task_ids

    viz = engine.visualize(snapshot, "i")
    assert "Initiative: i" in viz
    assert "Progress: i: 1/3 complete" in viz

    # Expect tree markers and status indicators.
    assert "[Architect] Root ✓" in viz
    assert "[Programmer] Child ← in progress" in viz
    assert "[Reviewer] Grand (waiting)" in viz
