from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from orchestrator.config import PIPELINES_DIR, TASKS_DIR
from orchestrator.utils.settings import get_setting
from orchestrator.utils.output_paths import resolve_paths

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineStage:
    id: str
    agent: str | None = None
    type: str = "task"  # "task" | "approval"
    depends_on: str | None = None
    output_path: str | None = None
    title: str | None = None
    notes: str | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class PipelineConfig:
    id: str
    stages: tuple[PipelineStage, ...]


class PipelineError(ValueError):
    pass


class PipelineManager:
    """Multi-stage pipeline runner.

    Pipelines are configured in settings under key: "pipelines".

    Instances are persisted in the control repo:
      state/pipelines/<pipeline_id>/<date>.json

    Stage tasks are created as normal tasks in state/tasks/ and annotated with:
      task['pipelineMeta']

    Approval stages are gates: the instance pauses until `approval.status` is set
    to "approved" (or "rejected") in the instance file.

    This implementation is intentionally filesystem-based and conservative.
    """

    def __init__(self, *, tasks_dir: Path | None = None, pipelines_dir: Path | None = None):
        self._tasks_dir = tasks_dir or TASKS_DIR
        self._pipelines_dir = pipelines_dir or PIPELINES_DIR

    # -----------------
    # Config parsing
    # -----------------
    def load_pipelines(self) -> dict[str, PipelineConfig]:
        raw = get_setting("pipelines", [])
        if not raw:
            return {}
        if not isinstance(raw, list):
            logger.warning("Invalid pipelines config: expected list")
            return {}

        out: dict[str, PipelineConfig] = {}
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                continue
            pid = str(entry.get("id") or "").strip()
            if not pid:
                logger.warning("Invalid pipeline entry at index %s: missing id", i)
                continue

            stages_raw = entry.get("stages") or []
            if not isinstance(stages_raw, list) or not stages_raw:
                logger.warning("Invalid pipeline %s: missing stages", pid)
                continue

            stages: list[PipelineStage] = []
            for j, s in enumerate(stages_raw):
                if not isinstance(s, dict):
                    continue
                sid = str(s.get("id") or "").strip()
                if not sid:
                    logger.warning("Invalid pipeline %s stage at index %s: missing id", pid, j)
                    continue

                stype = str(s.get("type") or "task").strip().lower()
                agent = s.get("agent")
                agent_str = str(agent).strip() if agent is not None else None
                depends_on = str(s.get("dependsOn") or "").strip() or None
                output_path = str(s.get("outputPath") or "").strip() or None
                title = str(s.get("title") or "").strip() or None
                notes = str(s.get("notes") or "").strip() or None
                project_id = str(s.get("projectId") or "").strip() or None

                if stype == "approval":
                    stages.append(
                        PipelineStage(
                            id=sid,
                            type="approval",
                            depends_on=depends_on,
                            output_path=output_path,
                            title=title,
                            notes=notes,
                            project_id=project_id,
                        )
                    )
                else:
                    if not agent_str:
                        logger.warning("Invalid pipeline %s stage %s: missing agent", pid, sid)
                        continue
                    stages.append(
                        PipelineStage(
                            id=sid,
                            agent=agent_str,
                            type="task",
                            depends_on=depends_on,
                            output_path=output_path,
                            title=title,
                            notes=notes,
                            project_id=project_id,
                        )
                    )

            if not stages:
                continue

            out[pid] = PipelineConfig(id=pid, stages=tuple(stages))

        return out

    # -----------------
    # Instances
    # -----------------
    def _instance_path(self, pipeline_id: str, date_iso: str) -> Path:
        return (self._pipelines_dir / pipeline_id / f"{date_iso}.json").resolve()

    def load_instance(self, pipeline_id: str, date_iso: str) -> dict[str, Any] | None:
        p = self._instance_path(pipeline_id, date_iso)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save_instance(self, pipeline_id: str, date_iso: str, data: Mapping[str, Any]) -> None:
        p = self._instance_path(pipeline_id, date_iso)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(p)

    def ensure_instance(self, *, pipeline_id: str, date_iso: str) -> dict[str, Any]:
        existing = self.load_instance(pipeline_id, date_iso)
        if existing:
            return existing

        inst: dict[str, Any] = {
            "version": 1,
            "pipelineId": pipeline_id,
            "date": date_iso,
            "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "running",
            "currentStage": None,
            "stages": {},
            "approval": {"status": "n/a"},
        }
        self.save_instance(pipeline_id, date_iso, inst)
        return inst

    # -----------------
    # Task creation helpers
    # -----------------
    def _utc_now_iso(self, now: datetime | None = None) -> str:
        dt = now or datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _find_stage(self, cfg: PipelineConfig, stage_id: str) -> PipelineStage | None:
        return next((s for s in cfg.stages if s.id == stage_id), None)

    def _next_stage(self, cfg: PipelineConfig, current_stage_id: str | None) -> PipelineStage | None:
        if current_stage_id is None:
            return cfg.stages[0] if cfg.stages else None
        for idx, st in enumerate(cfg.stages):
            if st.id == current_stage_id:
                return cfg.stages[idx + 1] if idx + 1 < len(cfg.stages) else None
        return None

    def _existing_task_for_stage(self, pipeline_id: str, date_iso: str, stage_id: str) -> str | None:
        if not self._tasks_dir.exists():
            return None
        for p in self._tasks_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                meta = data.get("pipelineMeta") or {}
                if (
                    meta.get("pipelineId") == pipeline_id
                    and meta.get("date") == date_iso
                    and meta.get("stageId") == stage_id
                ):
                    return str(data.get("id") or p.stem)
            except Exception:
                continue
        return None

    def _create_stage_task(
        self,
        *,
        pipeline_id: str,
        date_iso: str,
        stage: PipelineStage,
        prev_output_path: str | None,
        project_id: str,
        title: str,
        notes: str,
    ) -> str:
        task_id = str(uuid.uuid4()).upper()
        now_iso = self._utc_now_iso()

        task: dict[str, Any] = {
            "id": task_id,
            "title": title,
            "notes": notes,
            "projectId": project_id,
            "status": "active",
            "owner": "lobs",
            "reviewState": "approved",
            "workState": "not_started",
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "agent": stage.agent,
            "initiative": pipeline_id,
            "pipelineMeta": {
                "pipelineId": pipeline_id,
                "date": date_iso,
                "stageId": stage.id,
            },
        }

        # Output/input path wiring for prompter and for stage chaining.
        # Prefer explicit stage.output_path if provided.
        if stage.output_path:
            task["outputPathTemplate"] = stage.output_path
        # Carry forward previous stage output as input.
        if prev_output_path:
            task["inputPath"] = prev_output_path

        # Materialize outputPath now for stability (agents still see it).
        try:
            from orchestrator.config import CONTROL_REPO_PATH

            paths = resolve_paths(
                control_repo_path=CONTROL_REPO_PATH,
                item=task,
                project_id=project_id,
                agent=str(stage.agent or ""),
                task_id=task_id,
                default_output_template=None,
            )
            if paths.output_path is not None:
                task["outputPath"] = str(paths.output_path)
        except Exception:
            pass

        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        out = self._tasks_dir / f"{task_id}.json"
        out.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
        return task_id

    # -----------------
    # Public API
    # -----------------
    def tick(self) -> list[str]:
        """Advance any pipeline instances waiting on approval that are now approved."""
        created: list[str] = []
        pipelines = self.load_pipelines()
        if not pipelines:
            return created

        if not self._pipelines_dir.exists():
            return created

        for pipeline_id, cfg in pipelines.items():
            pdir = self._pipelines_dir / pipeline_id
            if not pdir.exists():
                continue

            for inst_file in sorted(pdir.glob("*.json")):
                try:
                    inst = json.loads(inst_file.read_text(encoding="utf-8"))
                except Exception:
                    continue

                if inst.get("status") != "waiting_approval":
                    continue
                approval = inst.get("approval") or {}
                if approval.get("status") != "approved":
                    continue

                date_iso = str(inst.get("date") or inst_file.stem)
                current_stage = inst.get("currentStage")
                # currentStage points at approval stage; advance.
                nxt = self._next_stage(cfg, str(current_stage) if current_stage else None)
                if not nxt:
                    inst["status"] = "completed"
                    inst["currentStage"] = None
                    self.save_instance(pipeline_id, date_iso, inst)
                    continue

                created.extend(self._advance_instance(pipeline_id, date_iso, cfg, inst, now_stage=nxt))
        return created

    def on_task_completed(self, task: Mapping[str, Any]) -> list[str]:
        """Called when a task completes successfully; advances pipeline if applicable."""
        meta = task.get("pipelineMeta")
        if not isinstance(meta, Mapping):
            return []
        pipeline_id = str(meta.get("pipelineId") or "").strip()
        stage_id = str(meta.get("stageId") or "").strip()
        date_iso = str(meta.get("date") or "").strip()
        if not pipeline_id or not stage_id or not date_iso:
            return []

        pipelines = self.load_pipelines()
        cfg = pipelines.get(pipeline_id)
        if not cfg:
            return []

        inst = self.ensure_instance(pipeline_id=pipeline_id, date_iso=date_iso)
        # Mark this stage done.
        stages = inst.setdefault("stages", {})
        st_entry = stages.setdefault(stage_id, {})
        st_entry.update({"status": "completed", "completedAt": self._utc_now_iso()})
        inst["currentStage"] = stage_id

        # Determine this stage's outputPath to feed into next stage.
        prev_output = None
        if isinstance(task.get("outputPath"), str) and task.get("outputPath"):
            prev_output = task.get("outputPath")

        # Advance.
        next_stage = self._next_stage(cfg, stage_id)
        created: list[str] = []
        if next_stage is None:
            inst["status"] = "completed"
            inst["currentStage"] = None
            self.save_instance(pipeline_id, date_iso, inst)
            return []

        created = self._advance_instance(
            pipeline_id,
            date_iso,
            cfg,
            inst,
            prev_output_path=prev_output,
            now_stage=next_stage,
        )
        return created

    # -----------------
    # Advance logic
    # -----------------
    def _advance_instance(
        self,
        pipeline_id: str,
        date_iso: str,
        cfg: PipelineConfig,
        inst: dict[str, Any],
        *,
        prev_output_path: str | None = None,
        now_stage: PipelineStage,
    ) -> list[str]:
        created: list[str] = []

        if now_stage.type == "approval":
            inst["status"] = "waiting_approval"
            inst["currentStage"] = now_stage.id
            inst["approval"] = {
                "status": "pending",
                "requestedAt": self._utc_now_iso(),
                "stageId": now_stage.id,
            }
            self.save_instance(pipeline_id, date_iso, inst)
            logger.info("[PIPELINE] %s %s waiting for approval (stage=%s)", pipeline_id, date_iso, now_stage.id)
            return []

        # Create next stage task (dedupe).
        existing = self._existing_task_for_stage(pipeline_id, date_iso, now_stage.id)
        if existing:
            logger.info(
                "[PIPELINE] Skip create (exists): pipeline=%s date=%s stage=%s task=%s",
                pipeline_id,
                date_iso,
                now_stage.id,
                str(existing)[:8],
            )
            inst.setdefault("stages", {}).setdefault(now_stage.id, {}).update({"status": "created", "taskId": existing})
            inst["currentStage"] = now_stage.id
            self.save_instance(pipeline_id, date_iso, inst)
            return []

        # Use pipeline id as initiative and for default path variables.
        title = now_stage.title or f"[{pipeline_id}] {now_stage.id}"
        notes = now_stage.notes or ""
        project_id = now_stage.project_id or "lobs-control"

        # If pipeline config includes per-stage outputPath, stage.output_path already wired.
        task_id = self._create_stage_task(
            pipeline_id=pipeline_id,
            date_iso=date_iso,
            stage=now_stage,
            prev_output_path=prev_output_path,
            project_id=project_id,
            title=title,
            notes=notes,
        )
        created.append(task_id)

        inst.setdefault("stages", {}).setdefault(now_stage.id, {}).update(
            {"status": "created", "taskId": task_id, "createdAt": self._utc_now_iso()}
        )
        inst["currentStage"] = now_stage.id
        inst["status"] = "running"
        self.save_instance(pipeline_id, date_iso, inst)

        logger.info(
            "[PIPELINE] Created task %s for pipeline=%s date=%s stage=%s",
            task_id[:8],
            pipeline_id,
            date_iso,
            now_stage.id,
        )
        return created
