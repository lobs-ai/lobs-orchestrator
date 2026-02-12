from __future__ import annotations

import json
from unittest.mock import Mock

from orchestrator.core.monitor import Monitor


class FakeProvider:
    def __init__(self, inbox_items: list[dict] | None = None):
        self.inbox = {str(i["id"]): dict(i) for i in (inbox_items or [])}
        self.tasks: dict[str, dict] = {}

    def get_inbox_items(self) -> list[dict]:
        return [dict(v) for v in self.inbox.values()]

    def update_inbox_item(self, item_id: str, updates: dict[str, object]) -> None:
        cur = self.inbox.get(item_id, {"id": item_id})
        cur.update(updates)
        self.inbox[item_id] = cur

    def update_task(self, task_id: str, updates: dict[str, object]) -> None:
        cur = self.tasks.get(task_id, {"id": task_id})
        cur.update(updates)
        self.tasks[task_id] = cur

    def add_inbox_item(self, item: dict) -> None:
        self.inbox[str(item["id"])] = dict(item)


def _suggester_wrapper(text: str) -> str:
    return json.dumps({"result": {"payloads": [{"text": text}]}})


def test_process_inbox_runs_llm_triage_prompt(monkeypatch):
    provider = FakeProvider(
        [
            {
                "id": "i1",
                "title": "Need decision",
                "body": "Should we redesign dashboard?",
                "status": "open",
                "type": "proposal",
            }
        ]
    )
    monitor = Monitor(provider)

    called = {"value": False}

    def fake_run(cmd, **kwargs):
        called["value"] = True
        assert "--agent" in cmd and "suggester" in cmd
        prompt = cmd[cmd.index("-m") + 1]
        assert "inbox triage router" in prompt.lower()
        return Mock(stdout=_suggester_wrapper('{"responder":"human","actionRequired":false,"reason":"needs approval"}'))

    monkeypatch.setattr("orchestrator.core.monitor.subprocess.run", fake_run)

    monitor.process_inbox()

    assert called["value"] is True
    item = provider.inbox["i1"]
    assert item["responder"] == "human"
    assert item["actionRequired"] is False
    assert item["triageSource"] == "suggester"
    assert "triagedAt" in item


def test_process_inbox_triage_routes_to_agent_task(monkeypatch):
    provider = FakeProvider(
        [
            {
                "id": "i2",
                "title": "Fix failing login tests",
                "body": "Recent login flow changes broke tests.",
                "status": "open",
                "projectId": "flock",
                "type": "request",
            }
        ]
    )
    monitor = Monitor(provider)

    def fake_run(*args, **kwargs):
        return Mock(stdout=_suggester_wrapper('{"responder":"programmer","actionRequired":true,"reason":"code fix needed"}'))

    monkeypatch.setattr("orchestrator.core.monitor.subprocess.run", fake_run)

    monitor.process_inbox()

    item = provider.inbox["i2"]
    assert item["status"] == "resolved"
    assert item["taskId"] == "task_from_inbox_i2"

    task = provider.tasks["task_from_inbox_i2"]
    assert task["agent"] == "programmer"
    assert task["projectId"] == "flock"
    assert task["workState"] == "not_started"


def test_process_inbox_keeps_existing_system_route_when_triage_fails(monkeypatch):
    provider = FakeProvider(
        [
            {
                "id": "i3",
                "title": "System check",
                "body": "Run routine system validation.",
                "status": "open",
                "type": "request",
                "responder": "system",
                "actionRequired": True,
            }
        ]
    )
    monitor = Monitor(provider)

    def fake_run(*args, **kwargs):
        raise RuntimeError("suggester unavailable")

    monkeypatch.setattr("orchestrator.core.monitor.subprocess.run", fake_run)

    monitor.process_inbox()

    item = provider.inbox["i3"]
    assert item["status"] == "resolved"
    assert "response" in item
