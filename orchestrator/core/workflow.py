"""orchestrator.core.workflow

Workflow Engine

This module provides deterministic initiative + work-chain tracking derived from the
current task set (filesystem state).

Responsibilities (ARCHITECTURE-V2.md "Workflow Engine"):
- Track initiatives (groups of related tasks from one goal)
- Manage work chains (parent/child links, "which tasks led to which")
- Handle sequencing (task B waits for task A)
- Report initiative progress (e.g. 5/8 complete)
- Detect when an initiative is fully complete
- Visualize work chains for debugging

The engine is intentionally conservative and stateless-by-default:
- It derives relationships from task metadata when possible.
- It can optionally persist a summarized snapshot for debugging/reporting.

Task metadata conventions:
- initiative: task["initiative"] OR task["collaboration"]["initiative"]
- parent/child: task["collaboration"]["parentTaskId"] OR task["parentTaskId"]
- explicit deps: task["dependsOn"] OR task["workflow"]["dependsOn"]

A parentTaskId implies a dependency (child waits for parent completion).

"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from orchestrator.config import STATE_DIR

logger = logging.getLogger(__name__)


def _utc_now_iso(now_fn: Callable[[], datetime] | None = None) -> str:
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_initiative(task: Mapping[str, Any]) -> str | None:
    initiative = (task.get("initiative") or task.get("collaboration", {}).get("initiative") or "").strip()
    return initiative or None


def _task_parent_id(task: Mapping[str, Any]) -> str | None:
    parent = task.get("parentTaskId") or task.get("parent_task_id")
    if not parent:
        parent = task.get("collaboration", {}).get("parentTaskId")
    if isinstance(parent, str) and parent.strip():
        return parent.strip()
    return None


def _task_agent(task: Mapping[str, Any]) -> str | None:
    agent = task.get("agent")
    if isinstance(agent, str) and agent.strip():
        return agent.strip()
    return None


def _task_title(task: Mapping[str, Any]) -> str:
    title = task.get("title") or task.get("prompt") or ""
    if not isinstance(title, str):
        return ""
    return title.strip()


def _task_state(task: Mapping[str, Any]) -> str:
    """Return a normalized state label.

    We treat workState for kind=task as primary, but fall back to status fields.
    """

    # Primary: workState
    ws = task.get("workState")
    if isinstance(ws, str) and ws.strip():
        return ws.strip().lower()

    # Secondary: status
    st = task.get("status")
    if isinstance(st, str) and st.strip():
        return st.strip().lower()

    return "unknown"


def _is_terminal_done(state: str) -> bool:
    # "archived" is effectively terminal; "completed" is terminal.
    # Some legacy flows may use status="completed".
    return state in {"completed", "archived", "done"}


def _is_in_progress(state: str) -> bool:
    return state in {"in_progress", "active", "running"}


def _parse_dep_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        val = value.strip()
        return [val] if val else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    return []


def _task_dependencies(task: Mapping[str, Any]) -> list[str]:
    deps: list[str] = []

    # parentTaskId implies dependency
    parent = _task_parent_id(task)
    if parent:
        deps.append(parent)

    # explicit dependsOn
    deps.extend(_parse_dep_list(task.get("dependsOn")))
    deps.extend(_parse_dep_list(task.get("workflow", {}).get("dependsOn")))

    # De-dup while preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for dep in deps:
        if dep not in seen:
            seen.add(dep)
            uniq.append(dep)
    return uniq


@dataclass(frozen=True, slots=True)
class WorkflowTask:
    id: str
    title: str
    initiative: str
    agent: str | None
    project_id: str | None
    state: str
    depends_on: tuple[str, ...]

    @property
    def done(self) -> bool:
        return _is_terminal_done(self.state)

    @property
    def in_progress(self) -> bool:
        return _is_in_progress(self.state)


@dataclass(frozen=True, slots=True)
class InitiativeProgress:
    initiative: str
    total: int
    done: int

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.done >= self.total


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    """Computed view of workflow state."""

    generated_at: str
    initiatives: dict[str, InitiativeProgress]
    tasks_by_initiative: dict[str, list[WorkflowTask]]
    blocked_task_ids: set[str]


class WorkflowEngine:
    """Computes initiative progress + dependency blocking and can persist a snapshot."""

    STATE_FILE = STATE_DIR / "workflow-state.json"

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.state_path = state_path or self.STATE_FILE
        self._now_fn = now_fn

    # ------------------------------------------------------------------
    # Core computation
    # ------------------------------------------------------------------

    def compute(self, tasks: Iterable[Mapping[str, Any]]) -> WorkflowSnapshot:
        tasks_by_id: dict[str, WorkflowTask] = {}
        by_init: dict[str, list[WorkflowTask]] = {}

        for raw in tasks:
            task_id = raw.get("id")
            if not isinstance(task_id, str) or not task_id.strip():
                continue

            initiative = _task_initiative(raw)
            if not initiative:
                continue

            wf_task = WorkflowTask(
                id=task_id.strip(),
                title=_task_title(raw),
                initiative=initiative,
                agent=_task_agent(raw),
                project_id=(raw.get("projectId") if isinstance(raw.get("projectId"), str) else None),
                state=_task_state(raw),
                depends_on=tuple(_task_dependencies(raw)),
            )

            tasks_by_id[wf_task.id] = wf_task
            by_init.setdefault(initiative, []).append(wf_task)

        blocked = self._compute_blocked(tasks_by_id)

        initiatives: dict[str, InitiativeProgress] = {}
        for initiative, items in by_init.items():
            total = len(items)
            done = sum(1 for t in items if t.done)
            initiatives[initiative] = InitiativeProgress(initiative=initiative, total=total, done=done)

        # Sort initiative task lists for stable output: roots first, then title.
        sorted_by_init: dict[str, list[WorkflowTask]] = {}
        for initiative, items in by_init.items():
            sorted_by_init[initiative] = sorted(items, key=lambda t: (len(t.depends_on), t.title.lower(), t.id))

        return WorkflowSnapshot(
            generated_at=_utc_now_iso(self._now_fn),
            initiatives=initiatives,
            tasks_by_initiative=sorted_by_init,
            blocked_task_ids=blocked,
        )

    def _compute_blocked(self, tasks_by_id: Mapping[str, WorkflowTask]) -> set[str]:
        blocked: set[str] = set()
        for task in tasks_by_id.values():
            if task.done:
                continue

            for dep_id in task.depends_on:
                dep = tasks_by_id.get(dep_id)
                # If dependency isn't found, we conservatively treat it as blocking.
                if dep is None or not dep.done:
                    blocked.add(task.id)
                    break
        return blocked

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------

    def progress_string(self, snapshot: WorkflowSnapshot, initiative: str) -> str:
        prog = snapshot.initiatives.get(initiative)
        if not prog:
            return f"{initiative}: 0/0"
        return f"{initiative}: {prog.done}/{prog.total} complete"

    def is_initiative_complete(self, snapshot: WorkflowSnapshot, initiative: str) -> bool:
        prog = snapshot.initiatives.get(initiative)
        return bool(prog and prog.complete)

    def blocked_tasks(self, snapshot: WorkflowSnapshot, initiative: str) -> list[WorkflowTask]:
        tasks = snapshot.tasks_by_initiative.get(initiative, [])
        return [t for t in tasks if t.id in snapshot.blocked_task_ids]

    def visualize(self, snapshot: WorkflowSnapshot, initiative: str) -> str:
        """Return an ASCII visualization of the initiative's work chain.

        This prefers a tree view when parent relationships can be inferred from
        depends_on edges that point to tasks in the same initiative.
        """

        tasks = snapshot.tasks_by_initiative.get(initiative)
        if not tasks:
            return f"Initiative: {initiative}\n(no tasks)"

        by_id = {t.id: t for t in tasks}

        # Build child mapping for same-initiative edges.
        children: dict[str, list[str]] = {t.id: [] for t in tasks}
        roots: list[str] = []
        for t in tasks:
            # Prefer a single parent edge if it exists inside initiative.
            parent = next((d for d in t.depends_on if d in by_id), None)
            if parent:
                children[parent].append(t.id)
            else:
                roots.append(t.id)

        for pid, kids in children.items():
            kids.sort(key=lambda cid: (by_id[cid].done is False, by_id[cid].title.lower(), cid))

        lines: list[str] = [f"Initiative: {initiative}", f"Progress: {self.progress_string(snapshot, initiative)}", "Chain:"]

        def fmt_status(t: WorkflowTask) -> str:
            if t.done:
                return "✓"
            if t.id in snapshot.blocked_task_ids:
                return "(waiting)"
            if t.in_progress:
                return "← in progress"
            return ""

        def fmt_agent(t: WorkflowTask) -> str:
            if t.agent:
                return f"[{t.agent.capitalize()}]"
            return "[Unassigned]"

        def walk(node_id: str, prefix: str) -> None:
            t = by_id[node_id]
            suffix = fmt_status(t)
            suffix_part = f" {suffix}" if suffix else ""
            title = t.title or "(untitled)"
            lines.append(f"{prefix}{fmt_agent(t)} {title}{suffix_part} ({t.id[:8]})")

            kid_ids = children.get(node_id, [])
            for idx, kid in enumerate(kid_ids):
                is_last = idx == len(kid_ids) - 1
                branch = "└── " if is_last else "├── "
                next_prefix = prefix + ("    " if is_last else "│   ")
                walk(kid, prefix + branch)
                # The recursive call already includes its own prefix; adjust.
                # (We pass combined prefix into walk for simplicity.)
                # But we already appended with prefix+branch; so for grandchildren,
                # we need to base them on next_prefix.
                # To do that, we re-walk kids with correct prefix is tricky.
            
        # The above walk doesn't propagate next_prefix; implement iterative stack instead.
        lines = [f"Initiative: {initiative}", f"Progress: {self.progress_string(snapshot, initiative)}", "Chain:"]

        stack: list[tuple[str, str, bool]] = []
        for rid in reversed(sorted(roots, key=lambda r: (by_id[r].title.lower(), r))):
            stack.append((rid, "  ", True))

        while stack:
            node_id, prefix, is_root = stack.pop()
            t = by_id[node_id]
            suffix = fmt_status(t)
            suffix_part = f" {suffix}" if suffix else ""
            title = t.title or "(untitled)"
            if is_root:
                lines.append(f"  {fmt_agent(t)} {title}{suffix_part} ({t.id[:8]})")
                child_prefix = "  "
            else:
                lines.append(f"{prefix}{fmt_agent(t)} {title}{suffix_part} ({t.id[:8]})")
                child_prefix = prefix

            kid_ids = children.get(node_id, [])
            if not kid_ids:
                continue

            # For children, we want a tree-like prefix. Determine base indent.
            # When current line starts with prefix that includes '├──'/'└──',
            # children should use '│   ' or '    ' continuation.
            if is_root:
                cont = "  "
            else:
                # Replace last branch markers with continuation spaces.
                if prefix.endswith("└── "):
                    cont = prefix[:-4] + "    "
                elif prefix.endswith("├── "):
                    cont = prefix[:-4] + "│   "
                else:
                    cont = prefix

            for idx in reversed(range(len(kid_ids))):
                cid = kid_ids[idx]
                is_last = idx == len(kid_ids) - 1
                branch = "└── " if is_last else "├── "
                stack.append((cid, cont + branch, False))

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_snapshot(self, snapshot: WorkflowSnapshot) -> None:
        data = self._snapshot_to_dict(snapshot)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.state_path)

    def load_snapshot(self) -> dict[str, Any] | None:
        try:
            if not self.state_path.exists():
                return None
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("[WORKFLOW] Failed to load %s: %s", self.state_path, e)
            return None

    @staticmethod
    def _snapshot_to_dict(snapshot: WorkflowSnapshot) -> dict[str, Any]:
        return {
            "version": 1,
            "generatedAt": snapshot.generated_at,
            "initiatives": {
                k: {"initiative": v.initiative, "total": v.total, "done": v.done, "complete": v.complete}
                for k, v in sorted(snapshot.initiatives.items(), key=lambda kv: kv[0].lower())
            },
            "blockedTaskIds": sorted(snapshot.blocked_task_ids),
            "tasks": {
                init: [
                    {
                        "id": t.id,
                        "title": t.title,
                        "agent": t.agent,
                        "projectId": t.project_id,
                        "state": t.state,
                        "dependsOn": list(t.depends_on),
                        "blocked": t.id in snapshot.blocked_task_ids,
                    }
                    for t in tasks
                ]
                for init, tasks in sorted(snapshot.tasks_by_initiative.items(), key=lambda kv: kv[0].lower())
            },
        }
