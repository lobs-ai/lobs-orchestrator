from __future__ import annotations

import json
from datetime import datetime, timezone

from orchestrator.utils.settings import set_setting


def _read_tasks(tasks_dir):
    out = []
    for p in tasks_dir.glob("*.json"):
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def test_pipeline_task_completion_creates_next_stage_task(temp_control_repo):
    # Pipeline with 2 task stages.
    set_setting(
        "pipelines",
        [
            {
                "id": "p1",
                "stages": [
                    {"id": "research", "agent": "researcher", "outputPath": "state/research/{pipeline}/{date}.md"},
                    {"id": "document", "agent": "writer", "dependsOn": "research", "outputPath": "state/reports/pending/{pipeline}-{date}.md"},
                ],
            }
        ],
    )

    from orchestrator.config import TASKS_DIR, PIPELINES_DIR
    from orchestrator.core.pipelines import PipelineManager

    mgr = PipelineManager(tasks_dir=TASKS_DIR, pipelines_dir=PIPELINES_DIR)

    # Simulate stage1 task completed.
    completed_task = {
        "id": "T1",
        "title": "Research",
        "projectId": "lobs-control",
        "agent": "researcher",
        "workState": "completed",
        "pipelineMeta": {"pipelineId": "p1", "date": "2026-01-01", "stageId": "research"},
        "outputPath": str(temp_control_repo / "state" / "research" / "p1" / "2026-01-01.md"),
    }

    created = mgr.on_task_completed(completed_task)
    assert len(created) == 1

    # Next stage task should exist.
    tasks = _read_tasks(TASKS_DIR)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["pipelineMeta"]["pipelineId"] == "p1"
    assert t["pipelineMeta"]["stageId"] == "document"
    assert t["pipelineMeta"]["date"] == "2026-01-01"
    assert t["inputPath"] == completed_task["outputPath"]
    assert "outputPath" in t

    # Instance file should exist.
    inst = PIPELINES_DIR / "p1" / "2026-01-01.json"
    assert inst.exists()


def test_pipeline_approval_gate_waits_then_advances(temp_control_repo):
    set_setting(
        "pipelines",
        [
            {
                "id": "p2",
                "stages": [
                    {"id": "document", "agent": "writer", "outputPath": "state/reports/pending/{pipeline}-{date}.md"},
                    {"id": "approve", "type": "approval", "dependsOn": "document"},
                    {"id": "design", "agent": "architect", "dependsOn": "approve", "outputPath": "state/designs/pending/{pipeline}-{date}.md"},
                ],
            }
        ],
    )

    from orchestrator.config import TASKS_DIR, PIPELINES_DIR
    from orchestrator.core.pipelines import PipelineManager

    mgr = PipelineManager(tasks_dir=TASKS_DIR, pipelines_dir=PIPELINES_DIR)

    completed_doc = {
        "id": "D1",
        "title": "Doc",
        "projectId": "lobs-control",
        "agent": "writer",
        "workState": "completed",
        "pipelineMeta": {"pipelineId": "p2", "date": "2026-01-01", "stageId": "document"},
        "outputPath": str(temp_control_repo / "state" / "reports" / "pending" / "p2-2026-01-01.md"),
    }

    created = mgr.on_task_completed(completed_doc)
    assert created == []

    inst_path = PIPELINES_DIR / "p2" / "2026-01-01.json"
    inst = json.loads(inst_path.read_text(encoding="utf-8"))
    assert inst["status"] == "waiting_approval"
    assert inst["approval"]["status"] == "pending"

    # Human approves by editing instance.
    inst["approval"]["status"] = "approved"
    inst_path.write_text(json.dumps(inst, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    created2 = mgr.tick()
    assert len(created2) == 1

    tasks = _read_tasks(TASKS_DIR)
    assert len(tasks) == 1
    assert tasks[0]["pipelineMeta"]["stageId"] == "design"
