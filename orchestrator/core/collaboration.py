"""orchestrator.core.collaboration

Collaboration Manager

Handles agent-to-agent handoffs and work chains.

Reference: docs/ARCHITECTURE.md (collaboration/handoff flow).

This module is intentionally deterministic and filesystem-friendly:
- Accepts a structured handoff payload
- Creates a derived task routed to the destination agent
- Tracks initiative -> chain metadata in a local state file for reporting

Handoff format (payload):

    {
      "from": "architect",
      "to": "programmer",
      "initiative": "user-auth-system",
      "work": {
        "title": "Implement JWT middleware",
        "context": "See design doc at docs/auth-design.md",
        "acceptance": "Middleware validates tokens per spec"
      }
    }

The manager will:
- Create a new task via provider.update_task(task_id, task_dict)
- Set task.agent = handoff.to (explicit routing)
- Annotate the task with handoff metadata (initiative, from/to, parent)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from uuid import uuid4

from orchestrator.config import STATE_DIR
from orchestrator.providers.base import TaskProvider

logger = logging.getLogger(__name__)


AgentType = Literal["programmer", "researcher", "reviewer", "writer", "architect"]


_ALLOWED_HANDOFFS: set[tuple[AgentType, AgentType]] = {
    ("architect", "programmer"),
    ("architect", "researcher"),
    ("reviewer", "programmer"),
    ("reviewer", "architect"),
    ("researcher", "architect"),
    ("researcher", "writer"),  # Research → formatted write-up
}


def _utc_now_iso(now_fn: Callable[[], datetime] | None = None) -> str:
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stable_chain_id(initiative: str) -> str:
    """Stable identifier for an initiative chain.

    Using a stable hash makes reporting consistent across restarts.
    """

    norm = initiative.strip().lower()
    digest = sha256(norm.encode("utf-8")).hexdigest()
    return f"chain_{digest[:16]}"


@dataclass(frozen=True, slots=True)
class HandoffWork:
    title: str
    context: str | None = None
    acceptance: str | None = None


@dataclass(frozen=True, slots=True)
class Handoff:
    from_agent: AgentType
    to_agent: AgentType
    initiative: str
    work: HandoffWork

    # Optional routing hints.
    project_id: str | None = None


class CollaborationManager:
    """Process agent handoffs and maintain initiative/work-chain state."""

    STATE_FILE = STATE_DIR / "collaboration-state.json"

    def __init__(
        self,
        provider: TaskProvider,
        *,
        state_path: Path | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.provider = provider
        self.state_path = state_path or self.STATE_FILE
        self._now_fn = now_fn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_handoff(
        self,
        payload: dict[str, Any],
        *,
        parent_task_id: str | None = None,
        default_project_id: str | None = None,
    ) -> str:
        """Validate a handoff payload and create a derived task.

        Returns the new task id.
        """

        handoff = self._parse_handoff(payload)

        if (handoff.from_agent, handoff.to_agent) not in _ALLOWED_HANDOFFS:
            logger.warning(
                "[COLLAB] Unrecognized handoff pattern %s -> %s (initiative=%s). Proceeding.",
                handoff.from_agent,
                handoff.to_agent,
                handoff.initiative,
            )

        project_id = handoff.project_id or default_project_id or "lobs-control"
        task_id = str(uuid4()).upper()
        chain_id = _stable_chain_id(handoff.initiative)

        task = self._build_task(
            task_id=task_id,
            project_id=project_id,
            handoff=handoff,
            chain_id=chain_id,
            parent_task_id=parent_task_id,
        )

        # Creating tasks is done via update_task for the filesystem provider.
        # This is safe because ControlManager is the single writer.
        self.provider.update_task(task_id, task)

        self._record_handoff(
            handoff=handoff,
            task_id=task_id,
            chain_id=chain_id,
            parent_task_id=parent_task_id,
            project_id=project_id,
        )

        logger.info(
            "[COLLAB] Created handoff task %s (%s -> %s, initiative=%s)",
            task_id[:8],
            handoff.from_agent,
            handoff.to_agent,
            handoff.initiative,
        )

        return task_id

    def initiatives(self) -> dict[str, Any]:
        """Return the current collaboration state grouped by initiative."""
        state = self._load_state()
        return state.get("initiatives", {})

    def group_tasks_by_initiative(self, tasks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Group tasks by initiative based on task metadata.

        Tasks that do not have an initiative are omitted.
        """

        grouped: dict[str, list[dict[str, Any]]] = {}
        for t in tasks:
            initiative = (t.get("initiative") or t.get("collaboration", {}).get("initiative") or "").strip()
            if not initiative:
                continue
            grouped.setdefault(initiative, []).append(t)
        return grouped

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_task(
        self,
        *,
        task_id: str,
        project_id: str,
        handoff: Handoff,
        chain_id: str,
        parent_task_id: str | None,
    ) -> dict[str, Any]:
        now_iso = _utc_now_iso(self._now_fn)

        notes_parts: list[str] = []
        if handoff.work.context:
            notes_parts.append(str(handoff.work.context).strip())
        if handoff.work.acceptance:
            notes_parts.append("Acceptance:\n" + str(handoff.work.acceptance).strip())

        provenance = {
            "type": "handoff",
            "from": handoff.from_agent,
            "to": handoff.to_agent,
            "initiative": handoff.initiative,
            "chainId": chain_id,
        }
        if parent_task_id:
            provenance["parentTaskId"] = parent_task_id

        # Embed metadata at end of notes for human readability.
        notes_parts.append("\n---\nHandoff Metadata:\n" + json.dumps(provenance, indent=2, sort_keys=True))

        task: dict[str, Any] = {
            "id": task_id,
            "kind": "task",
            "projectId": project_id,
            "title": handoff.work.title,
            "notes": "\n\n".join(p for p in notes_parts if p),
            "status": "active",
            "workState": "not_started",
            "createdAt": now_iso,
            "updatedAt": now_iso,

            # Explicit agent routing; the Router honors `agent`.
            "agent": handoff.to_agent,

            # Machine-readable collaboration metadata.
            "initiative": handoff.initiative,
            "collaboration": {
                "type": "handoff",
                "from": handoff.from_agent,
                "to": handoff.to_agent,
                "initiative": handoff.initiative,
                "chainId": chain_id,
                "parentTaskId": parent_task_id,
            },
        }

        return task

    @staticmethod
    def _normalize_agent(value: Any, field_name: str) -> AgentType:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"handoff.{field_name} must be a non-empty string")
        norm = value.strip().lower()
        if norm not in {"programmer", "researcher", "reviewer", "writer", "architect"}:
            raise ValueError(f"handoff.{field_name} must be a known agent type, got: {value!r}")
        return norm  # type: ignore[return-value]

    def _parse_handoff(self, payload: dict[str, Any]) -> Handoff:
        if not isinstance(payload, dict):
            raise ValueError("handoff payload must be an object")

        from_agent = self._normalize_agent(payload.get("from"), "from")
        to_agent = self._normalize_agent(payload.get("to"), "to")

        initiative = payload.get("initiative")
        if not isinstance(initiative, str) or not initiative.strip():
            raise ValueError("handoff.initiative must be a non-empty string")
        initiative = initiative.strip()

        work = payload.get("work")
        if not isinstance(work, dict):
            raise ValueError("handoff.work must be an object")

        title = work.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("handoff.work.title must be a non-empty string")

        context = work.get("context")
        acceptance = work.get("acceptance")
        if context is not None and not isinstance(context, str):
            raise ValueError("handoff.work.context must be a string if provided")
        if acceptance is not None and not isinstance(acceptance, str):
            raise ValueError("handoff.work.acceptance must be a string if provided")

        project_id = payload.get("projectId") or payload.get("project_id")
        if project_id is not None:
            if not isinstance(project_id, str) or not project_id.strip():
                raise ValueError("handoff.projectId must be a non-empty string if provided")
            project_id = project_id.strip()

        return Handoff(
            from_agent=from_agent,
            to_agent=to_agent,
            initiative=initiative,
            work=HandoffWork(title=title.strip(), context=context, acceptance=acceptance),
            project_id=project_id,
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            if self.state_path.exists():
                return json.loads(self.state_path.read_text())
        except Exception as e:
            logger.warning("[COLLAB] Failed to load state %s: %s", self.state_path, e)
        return {"version": 1, "initiatives": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.state_path)

    def _record_handoff(
        self,
        *,
        handoff: Handoff,
        task_id: str,
        chain_id: str,
        parent_task_id: str | None,
        project_id: str,
    ) -> None:
        state = self._load_state()
        initiatives = state.setdefault("initiatives", {})
        entry = initiatives.setdefault(
            handoff.initiative,
            {
                "chainId": chain_id,
                "createdAt": _utc_now_iso(self._now_fn),
                "handoffs": [],
                "tasks": [],
            },
        )

        entry.setdefault("tasks", [])
        entry.setdefault("handoffs", [])

        task_ref = {
            "id": task_id,
            "projectId": project_id,
            "agent": handoff.to_agent,
            "title": handoff.work.title,
            "createdAt": _utc_now_iso(self._now_fn),
            "parentTaskId": parent_task_id,
        }
        entry["tasks"].append(task_ref)

        entry["handoffs"].append(
            {
                "from": handoff.from_agent,
                "to": handoff.to_agent,
                "taskId": task_id,
                "projectId": project_id,
                "parentTaskId": parent_task_id,
                "createdAt": _utc_now_iso(self._now_fn),
            }
        )

        self._save_state(state)
