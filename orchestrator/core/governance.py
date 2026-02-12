"""Governance and system-intelligence layer for orchestrator autonomy.

Implements:
- Tiered execution authority (Tier 1/2/3)
- Confidence/risk/impact classification
- Approval gating (architect + human)
- Wrongness detection and temporary autonomy reduction
- Persistent operational metrics and reflection artifacts
- Structured agent communication logging
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.config import STATE_DIR, TASKS_DIR
from orchestrator.providers.base import TaskProvider
from orchestrator.utils.settings import get_setting

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_category(error_text: str) -> str:
    text = (error_text or "").lower()
    categories = {
        "timeout": ["timeout", "timed out"],
        "memory": ["oom", "out of memory", "killed", "memory"],
        "auth": ["unauthorized", "forbidden", "api key", "authentication"],
        "network": ["connection", "dns", "network", "refused"],
        "git": ["git", "rebase", "merge conflict", "conflict"],
        "test": ["test failed", "assertion", "pytest", "failing test"],
    }
    for cat, needles in categories.items():
        if any(n in text for n in needles):
            return cat
    return "other"


@dataclass(frozen=True)
class GovernanceAssessment:
    tier: int
    impact_scope: str  # local|cross|strategic
    risk_level: str  # low|medium|high
    confidence: float
    reason: str


class GovernanceManager:
    """Central authority + adaptation manager."""

    STRATEGIC_RE = re.compile(
        r"\b(orchestrator|orchestration|autonomy|autonomous|strategy|safety system|governance|major feature|direction)\b",
        re.IGNORECASE,
    )
    CROSS_RE = re.compile(
        r"\b(public api|api contract|data contract|schema|cross[- ]service|infra|dependency|performance)\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        provider: TaskProvider,
        *,
        state_dir: Path | None = None,
        tasks_dir: Path | None = None,
    ):
        self.provider = provider
        self.state_dir = self._resolve_state_dir(state_dir)
        self.tasks_dir = Path(tasks_dir) if tasks_dir is not None else TASKS_DIR
        self.metrics_file = self.state_dir / "system-metrics.json"
        self.reflections_dir = self.state_dir / "reflections"
        self.comms_log_file = self.state_dir / "agent-communications.jsonl"
        self._metrics = self._load_metrics()
        self._learning_interval_s = int(get_setting("learning_pass_interval_seconds", 3600))
        self._last_learning_run = float(self._metrics.get("last_learning_run_ts", 0.0))
        self._proposal_conf_threshold = float(self._metrics.get("proposal_conf_threshold", 0.5))

    # ------------------------------------------------------------------
    # Assessment + gating
    # ------------------------------------------------------------------

    def assess_task(self, task: dict[str, Any]) -> GovernanceAssessment:
        g = task.get("governance") if isinstance(task.get("governance"), dict) else {}
        if g:
            tier = int(g.get("tier", 0) or 0)
            impact = str(g.get("impactScope", "") or "").strip().lower()
            risk = str(g.get("riskLevel", "") or "").strip().lower()
            confidence = float(g.get("confidence", -1))
            if tier in (1, 2, 3) and impact in {"local", "cross", "strategic"} and risk in {"low", "medium", "high"}:
                if 0.0 <= confidence <= 1.0:
                    return GovernanceAssessment(tier, impact, risk, confidence, "task governance metadata")

        text = " ".join(
            str(x) for x in [
                task.get("title", ""),
                task.get("notes", ""),
                " ".join(task.get("tags", [])) if isinstance(task.get("tags"), list) else "",
                task.get("kind", ""),
            ]
        )
        if self.STRATEGIC_RE.search(text):
            assessment = GovernanceAssessment(3, "strategic", "high", 0.35, "strategic keyword heuristic")
        elif self.CROSS_RE.search(text):
            assessment = GovernanceAssessment(2, "cross", "medium", 0.6, "cross-module/interface keyword heuristic")
        else:
            assessment = GovernanceAssessment(1, "local", "low", 0.85, "local/default heuristic")

        if assessment.impact_scope in {"cross", "strategic"} and assessment.confidence < self._proposal_conf_threshold:
            return GovernanceAssessment(
                max(assessment.tier, 2),
                assessment.impact_scope,
                "high" if assessment.impact_scope == "strategic" else assessment.risk_level,
                assessment.confidence,
                "low confidence with non-local impact",
            )

        return assessment

    def evaluate_and_gate(self, task: dict[str, Any], project_id: str) -> tuple[bool, GovernanceAssessment, str]:
        """Returns (allow_execution, assessment, reason)."""
        governance = task.get("governance") if isinstance(task.get("governance"), dict) else {}
        if governance.get("bypass"):
            return True, self.assess_task(task), "governance bypass task"

        # Temporary autonomy reduction for unstable domains.
        unstable_until = float(self._metrics.get("unstable_domains", {}).get(project_id, 0.0))
        force_t1_review_until = float(self._metrics.get("force_tier1_architect_until_ts", 0.0))
        if time.time() < unstable_until or time.time() < force_t1_review_until:
            base = self.assess_task(task)
            promoted = GovernanceAssessment(
                tier=max(2, base.tier),
                impact_scope="cross" if base.impact_scope == "local" else base.impact_scope,
                risk_level="medium" if base.risk_level == "low" else base.risk_level,
                confidence=base.confidence,
                reason="temporary autonomy reduction due to instability",
            )
            return self._gate_for_assessment(task, project_id, promoted)

        assessment = self.assess_task(task)
        return self._gate_for_assessment(task, project_id, assessment)

    def _gate_for_assessment(
        self,
        task: dict[str, Any],
        project_id: str,
        assessment: GovernanceAssessment,
    ) -> tuple[bool, GovernanceAssessment, str]:
        task_id = str(task.get("id", ""))
        governance = task.get("governance") if isinstance(task.get("governance"), dict) else {}

        if assessment.tier <= 1:
            return True, assessment, "tier1-local-autonomous"

        if assessment.tier == 2:
            if governance.get("approvedByArchitectAt"):
                return True, assessment, "tier2-approved-by-architect"
            self._ensure_architect_approval_task(task, project_id, assessment)
            return False, assessment, "tier2-awaiting-architect-approval"

        # Tier 3
        if governance.get("humanApprovedAt"):
            return True, assessment, "tier3-approved-by-human"
        self._ensure_human_approval_request(task, project_id, assessment)
        return False, assessment, "tier3-awaiting-human-approval"

    def _ensure_architect_approval_task(
        self,
        task: dict[str, Any],
        project_id: str,
        assessment: GovernanceAssessment,
    ) -> None:
        now = _utc_now_iso()
        task_id = str(task.get("id", ""))
        governance = task.get("governance") if isinstance(task.get("governance"), dict) else {}
        approval_task_id = governance.get("approvalTaskId")
        if not approval_task_id:
            approval_task_id = f"ARCH-APPROVAL-{task_id[:8]}-{uuid.uuid4().hex[:6].upper()}"
            proposal_payload = {
                "targetTaskId": task_id,
                "title": task.get("title", ""),
                "projectId": project_id,
                "assessment": asdict(assessment),
                "createdAt": now,
            }
            self.provider.update_task(
                approval_task_id,
                {
                    "id": approval_task_id,
                    "kind": "task",
                    "projectId": project_id,
                    "title": f"Architect Approval: {task.get('title', task_id[:8])}",
                    "notes": (
                        "Review and approve/reject this Tier 2 change proposal.\n\n"
                        "Reply with APPROVED or REJECTED in your summary.\n\n"
                        f"Proposal JSON:\n```json\n{json.dumps(proposal_payload, indent=2)}\n```"
                    ),
                    "status": "active",
                    "workState": "not_started",
                    "agent": "architect",
                    "governance": {
                        "bypass": True,
                        "approvalForTaskId": task_id,
                        "approvalType": "tier2",
                    },
                    "createdAt": now,
                    "updatedAt": now,
                },
            )
            self.record_proposal_event("created", proactive=bool(task.get("proactiveMeta")))

        updated_gov = {
            **governance,
            "tier": assessment.tier,
            "impactScope": assessment.impact_scope,
            "riskLevel": assessment.risk_level,
            "confidence": assessment.confidence,
            "approvalTaskId": approval_task_id,
            "blockedAt": now,
            "status": "awaiting_architect_approval",
        }
        self.provider.update_task(
            task_id,
            {
                "reviewState": "needs_architect_approval",
                "governance": updated_gov,
                "updatedAt": now,
            },
        )

    def _ensure_human_approval_request(
        self,
        task: dict[str, Any],
        project_id: str,
        assessment: GovernanceAssessment,
    ) -> None:
        now = _utc_now_iso()
        task_id = str(task.get("id", ""))
        governance = task.get("governance") if isinstance(task.get("governance"), dict) else {}
        request_id = governance.get("humanApprovalRequestId")
        if not request_id:
            request_id = f"HUMAN-APPROVAL-{task_id[:8]}-{uuid.uuid4().hex[:6].upper()}"
            self.provider.add_inbox_item(
                {
                    "id": request_id,
                    "title": f"Human Approval Required: {task.get('title', task_id[:8])}",
                    "body": (
                        "Tier 3 strategic change requested.\n\n"
                        f"Task: {task_id}\nProject: {project_id}\n"
                        f"Impact: {assessment.impact_scope}\nRisk: {assessment.risk_level}\n"
                        f"Confidence: {assessment.confidence:.2f}\n"
                    ),
                    "type": "approval_required",
                    "severity": "high",
                    "projectId": project_id,
                    "taskId": task_id,
                    "createdAt": now,
                }
            )
            self.record_proposal_event("created", proactive=bool(task.get("proactiveMeta")))

        updated_gov = {
            **governance,
            "tier": assessment.tier,
            "impactScope": assessment.impact_scope,
            "riskLevel": assessment.risk_level,
            "confidence": assessment.confidence,
            "humanApprovalRequestId": request_id,
            "blockedAt": now,
            "status": "awaiting_human_approval",
        }
        self.provider.update_task(
            task_id,
            {
                "reviewState": "needs_human_approval",
                "governance": updated_gov,
                "updatedAt": now,
            },
        )

    def process_approval_task_completion(self, approval_task: dict[str, Any]) -> None:
        """Called when an architect approval task completes."""
        governance = approval_task.get("governance") if isinstance(approval_task.get("governance"), dict) else {}
        target_id = governance.get("approvalForTaskId")
        if not target_id:
            return
        target = self.provider.get_task(target_id) or {}
        target_gov = target.get("governance") if isinstance(target.get("governance"), dict) else {}
        now = _utc_now_iso()
        target_gov["approvedByArchitectAt"] = now
        target_gov["status"] = "approved_by_architect"
        self.provider.update_task(
            target_id,
            {
                "governance": target_gov,
                "reviewState": "approved",
                "updatedAt": now,
            },
        )
        self.record_proposal_event("accepted", proactive=bool(target.get("proactiveMeta")))

    def process_inbox_response(self, item: dict[str, Any]) -> bool:
        """Apply human/architect approvals from inbox response threads.

        Returns True when an approval/rejection was recognized and applied.
        """
        decision_text = self._extract_inbox_response_text(item)
        if not decision_text:
            return False

        decision = self._parse_approval_decision(decision_text)
        if decision not in {"approved", "rejected"}:
            return False

        candidates = self._find_pending_approval_tasks()
        if not candidates:
            return False

        target_ids = self._extract_target_task_ids(decision_text)
        if target_ids:
            candidates = [t for t in candidates if str(t.get("id")) in target_ids]
        if not candidates:
            return False

        # If user did not identify a task and multiple approvals are pending, avoid applying ambiguously.
        if not target_ids and len(candidates) > 1:
            return False

        source = self._infer_response_source(item)
        now = _utc_now_iso()
        for task in candidates:
            task_id = str(task.get("id"))
            governance = task.get("governance") if isinstance(task.get("governance"), dict) else {}
            review_state = str(task.get("reviewState", ""))

            if decision == "approved":
                if review_state == "needs_human_approval":
                    governance["humanApprovedAt"] = now
                    governance["status"] = "approved_by_human"
                else:
                    governance["approvedByArchitectAt"] = now
                    governance["status"] = "approved_by_architect"
                updates = {
                    "governance": governance,
                    "reviewState": "approved",
                    "workState": "not_started",
                    "status": "active",
                    "updatedAt": now,
                }
                self.record_proposal_event("accepted", proactive=bool(task.get("proactiveMeta")))
            else:
                governance["status"] = f"rejected_by_{source}"
                governance["rejectedAt"] = now
                updates = {
                    "governance": governance,
                    "reviewState": "rejected",
                    "workState": "blocked",
                    "status": "active",
                    "updatedAt": now,
                }
                self.record_proposal_event("rejected", proactive=bool(task.get("proactiveMeta")))

            self.provider.update_task(task_id, updates)

            self.log_communication(
                channel="initiative",
                sender=source,
                recipient="orchestrator",
                message_type=f"approval_{decision}",
                content=decision_text,
                related_task_id=task_id,
                initiative=(task.get("initiative") or None),
            )

        self._metrics["human_overrides"] = int(self._metrics.get("human_overrides", 0)) + len(candidates)
        self._save_metrics()
        return True

    # ------------------------------------------------------------------
    # Metrics, wrongness, and learning
    # ------------------------------------------------------------------

    def record_spawn_block(self, reason: str) -> None:
        blocks = self._metrics.setdefault("spawn_blocks", {})
        blocks[reason] = int(blocks.get(reason, 0)) + 1
        if reason == "project_lock":
            self._metrics["lock_contention_incidents"] = int(self._metrics.get("lock_contention_incidents", 0)) + 1
        self._save_metrics()

    def record_circuit_activation(self) -> None:
        self._metrics["circuit_breaker_activations"] = int(self._metrics.get("circuit_breaker_activations", 0)) + 1
        self._save_metrics()

    def record_task_outcome(
        self,
        task_id: str,
        project_id: str,
        success: bool,
        attempts: int,
        duration_seconds: float,
        *,
        failure_reason: str = "",
        error_log: str = "",
        proactive: bool = False,
    ) -> None:
        outcomes = self._metrics.setdefault("outcomes", {})
        outcomes["completed"] = int(outcomes.get("completed", 0)) + (1 if success else 0)
        outcomes["failed"] = int(outcomes.get("failed", 0)) + (0 if success else 1)
        outcomes["total_attempts"] = int(outcomes.get("total_attempts", 0)) + max(1, attempts)
        outcomes["total_duration_seconds"] = float(outcomes.get("total_duration_seconds", 0.0)) + max(0.0, duration_seconds)
        if attempts > 1:
            outcomes["retry_events"] = int(outcomes.get("retry_events", 0)) + 1
        if "rollback" in (failure_reason or "").lower():
            outcomes["rollback_count"] = int(outcomes.get("rollback_count", 0)) + 1
        if proactive:
            outcomes["proactive_completed"] = int(outcomes.get("proactive_completed", 0)) + (1 if success else 0)
            outcomes["proactive_failed"] = int(outcomes.get("proactive_failed", 0)) + (0 if success else 1)

        if not success:
            failures = self._metrics.setdefault("recent_failures", [])
            failures.append(
                {
                    "ts": time.time(),
                    "task_id": task_id,
                    "project_id": project_id,
                    "category": _error_category(error_log or failure_reason),
                }
            )
            # Keep rolling window of recent failures.
            self._metrics["recent_failures"] = failures[-300:]

        self._write_reflection_artifact(
            task_id=task_id,
            project_id=project_id,
            success=success,
            attempts=attempts,
            duration_seconds=duration_seconds,
            failure_reason=failure_reason,
            error_log=error_log,
        )

        self._apply_wrongness_triggers(task_id=task_id, project_id=project_id, success=success, failure_reason=failure_reason)
        self._save_metrics()

    def record_proposal_event(self, event: str, *, proactive: bool) -> None:
        key = "proposals_proactive" if proactive else "proposals_general"
        stats = self._metrics.setdefault(key, {"created": 0, "accepted": 0, "rejected": 0})
        if event in stats:
            stats[event] = int(stats.get(event, 0)) + 1
        self._apply_wrongness_triggers(task_id="", project_id="", success=True, failure_reason="")
        self._save_metrics()

    def _apply_wrongness_triggers(
        self,
        *,
        task_id: str,
        project_id: str,
        success: bool,
        failure_reason: str,
    ) -> None:
        now = time.time()
        failures = self._metrics.get("recent_failures", [])

        repeated_fail_threshold = int(get_setting("wrongness_repeated_failure_threshold", 3))
        by_task = Counter(f["task_id"] for f in failures if f.get("task_id"))
        by_category = Counter(f["category"] for f in failures if f.get("category"))
        if any(v >= repeated_fail_threshold for v in by_task.values()) or any(v >= repeated_fail_threshold for v in by_category.values()):
            unstable = self._metrics.setdefault("unstable_domains", {})
            if project_id:
                unstable[project_id] = max(float(unstable.get(project_id, 0.0)), now + 6 * 3600)

        outcomes = self._metrics.get("outcomes", {})
        completed = int(outcomes.get("completed", 0))
        rollbacks = int(outcomes.get("rollback_count", 0))
        if completed >= 5 and rollbacks / max(1, completed) > 0.2:
            self._metrics["pause_proactive_until_ts"] = max(float(self._metrics.get("pause_proactive_until_ts", 0.0)), now + 6 * 3600)
            self._metrics["force_tier1_architect_until_ts"] = max(float(self._metrics.get("force_tier1_architect_until_ts", 0.0)), now + 6 * 3600)

        proposal_stats = self._metrics.get("proposals_proactive", {})
        p_created = int(proposal_stats.get("created", 0))
        p_rejected = int(proposal_stats.get("rejected", 0))
        if p_created >= 5:
            rejection_rate = p_rejected / max(1, p_created)
            if rejection_rate > 0.5:
                self._proposal_conf_threshold = min(0.9, self._proposal_conf_threshold + 0.05)
                self._metrics["proposal_conf_threshold"] = self._proposal_conf_threshold

    def should_pause_proactive(self) -> bool:
        return time.time() < float(self._metrics.get("pause_proactive_until_ts", 0.0))

    def run_learning_pass(self) -> list[str]:
        now = time.time()
        if now - self._last_learning_run < self._learning_interval_s:
            return []

        failures = self._metrics.get("recent_failures", [])
        categories = Counter(f.get("category", "other") for f in failures)
        created: list[str] = []
        for category, count in categories.items():
            if count < 3:
                continue
            task_id = f"LEARN-{category.upper()}-{uuid.uuid4().hex[:6].upper()}"
            self.provider.update_task(
                task_id,
                {
                    "id": task_id,
                    "kind": "task",
                    "projectId": "lobs-control",
                    "title": f"Meta-improvement: reduce recurring {category} failures",
                    "notes": (
                        f"Recurring failure category detected: {category} ({count} occurrences).\n"
                        "Propose and implement a systemic mitigation in orchestrator guardrails/prompts."
                    ),
                    "status": "active",
                    "workState": "not_started",
                    "agent": "architect",
                    "governance": {
                        "bypass": True,
                        "tier": 2,
                        "impactScope": "cross",
                        "riskLevel": "medium",
                        "confidence": 0.75,
                    },
                    "tags": ["meta-improvement", f"failure:{category}"],
                    "createdAt": _utc_now_iso(),
                    "updatedAt": _utc_now_iso(),
                },
            )
            created.append(task_id)

        self._last_learning_run = now
        self._metrics["last_learning_run_ts"] = now
        self._save_metrics()
        return created

    # ------------------------------------------------------------------
    # Communications
    # ------------------------------------------------------------------

    def log_communication(
        self,
        *,
        channel: str,
        sender: str,
        recipient: str,
        message_type: str,
        content: str,
        related_task_id: str | None = None,
        initiative: str | None = None,
    ) -> None:
        self.comms_log_file.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": _utc_now_iso(),
            "channel": channel,  # work|initiative
            "sender": sender,
            "recipient": recipient,
            "messageType": message_type,
            "content": content[:5000],
            "relatedTaskId": related_task_id,
            "initiative": initiative,
        }
        with open(self.comms_log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    # ------------------------------------------------------------------
    # Dashboard export
    # ------------------------------------------------------------------

    def get_dashboard_metrics(self) -> dict[str, Any]:
        outcomes = self._metrics.get("outcomes", {})
        completed = int(outcomes.get("completed", 0))
        failed = int(outcomes.get("failed", 0))
        total = completed + failed
        attempts_total = int(outcomes.get("total_attempts", 0))
        duration_total = float(outcomes.get("total_duration_seconds", 0.0))

        proactive_stats = self._metrics.get("proposals_proactive", {})
        proactive_created = int(proactive_stats.get("created", 0))
        proactive_accepted = int(proactive_stats.get("accepted", 0))
        proactive_rejected = int(proactive_stats.get("rejected", 0))

        return {
            "executionQuality": {
                "taskSuccessRate": (completed / max(1, total)),
                "averageAttemptsPerTask": (attempts_total / max(1, total)),
                "retryFrequency": int(outcomes.get("retry_events", 0)),
                "averageTimeToCompletionSeconds": (duration_total / max(1, completed)),
                "rollbackRate": (int(outcomes.get("rollback_count", 0)) / max(1, completed)),
            },
            "stability": {
                "workerCrashRate": (failed / max(1, total)),
                "memoryViolations": int(self._metrics.get("memory_violations", 0)),
                "circuitBreakerActivations": int(self._metrics.get("circuit_breaker_activations", 0)),
                "lockContentionIncidents": int(self._metrics.get("lock_contention_incidents", 0)),
            },
            "strategicEffectiveness": {
                "proactiveProposalAcceptanceRate": (proactive_accepted / max(1, proactive_created)),
                "proactiveChangesRevertedRate": (proactive_rejected / max(1, proactive_created)),
                "recurringErrorCategories": self._top_error_categories(),
                "humanOverrideFrequency": int(self._metrics.get("human_overrides", 0)),
            },
            "controls": {
                "pauseProactiveUntilTs": float(self._metrics.get("pause_proactive_until_ts", 0.0)),
                "forceTier1ArchitectUntilTs": float(self._metrics.get("force_tier1_architect_until_ts", 0.0)),
                "proposalConfidenceThreshold": float(self._metrics.get("proposal_conf_threshold", self._proposal_conf_threshold)),
            },
        }

    def _top_error_categories(self) -> list[dict[str, Any]]:
        failures = self._metrics.get("recent_failures", [])
        counts = Counter(f.get("category", "other") for f in failures)
        return [{"category": c, "count": n} for c, n in counts.most_common(5)]

    def _extract_inbox_response_text(self, item: dict[str, Any]) -> str:
        messages = item.get("messages")
        if isinstance(messages, list) and messages:
            for msg in reversed(messages):
                text = str((msg or {}).get("text", "")).strip()
                if text:
                    return text
        return str(item.get("lastMessage", "")).strip()

    def _parse_approval_decision(self, text: str) -> str:
        s = text.lower()
        reject_signals = [
            "reject",
            "rejected",
            "not approved",
            "do not approve",
            "decline",
            "deny",
            "no,",
            "no ",
        ]
        approve_signals = [
            "approve",
            "approved",
            "go ahead",
            "lgtm",
            "ship it",
            "yes",
        ]
        if any(sig in s for sig in reject_signals):
            return "rejected"
        if any(sig in s for sig in approve_signals):
            return "approved"
        return ""

    def _extract_target_task_ids(self, text: str) -> set[str]:
        ids = set()
        # Match likely orchestrator ids: UUID-ish, ARCH-APPROVAL..., HUMAN-APPROVAL..., PROPOSAL..., task-...
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{4,}", text):
            if "-" in token or "_" in token:
                ids.add(token)
        return ids

    def _find_pending_approval_tasks(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        # Filesystem-backed discovery (authoritative in local mode).
        try:
            if self.tasks_dir.exists():
                for task_file in self.tasks_dir.glob("*.json"):
                    try:
                        task = json.loads(task_file.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    task_id = str(task.get("id", ""))
                    if not task_id or task_id in seen:
                        continue
                    if self._is_task_waiting_approval(task):
                        out.append(task)
                        seen.add(task_id)
        except Exception:
            pass

        # Provider fallback (for tests / non-filesystem backends).
        try:
            for task in self.provider.get_tasks():
                task_id = str((task or {}).get("id", ""))
                if not task_id or task_id in seen:
                    continue
                if self._is_task_waiting_approval(task):
                    out.append(task)
                    seen.add(task_id)
        except Exception:
            pass

        return out

    @staticmethod
    def _is_task_waiting_approval(task: dict[str, Any]) -> bool:
        review_state = str(task.get("reviewState", ""))
        governance = task.get("governance") if isinstance(task.get("governance"), dict) else {}
        status = str(governance.get("status", ""))
        return review_state in {"needs_human_approval", "needs_architect_approval"} or status.startswith("awaiting_")

    @staticmethod
    def _infer_response_source(item: dict[str, Any]) -> str:
        messages = item.get("messages")
        if isinstance(messages, list) and messages:
            author = str((messages[-1] or {}).get("author", "")).strip().lower()
            if author:
                return author
        return "human"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _write_reflection_artifact(
        self,
        *,
        task_id: str,
        project_id: str,
        success: bool,
        attempts: int,
        duration_seconds: float,
        failure_reason: str,
        error_log: str,
    ) -> None:
        self.reflections_dir.mkdir(parents=True, exist_ok=True)
        artifact = {
            "taskId": task_id,
            "projectId": project_id,
            "timestamp": _utc_now_iso(),
            "succeeded": success,
            "attempts": attempts,
            "durationSeconds": duration_seconds,
            "whatSucceeded": "Task completed and finalized cleanly." if success else "",
            "whatFailed": "" if success else failure_reason or "execution failure",
            "retriesRequired": max(0, attempts - 1),
            "missedSignals": _error_category(error_log or failure_reason) if not success else "",
            "patternCategory": "delivery_success" if success else _error_category(error_log or failure_reason),
        }
        path = self.reflections_dir / f"{task_id}.json"
        path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    def _load_metrics(self) -> dict[str, Any]:
        if not self.metrics_file.exists():
            return {}
        try:
            return json.loads(self.metrics_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_metrics(self) -> None:
        self.metrics_file.parent.mkdir(parents=True, exist_ok=True)
        self.metrics_file.write_text(json.dumps(self._metrics, indent=2) + "\n", encoding="utf-8")
    def _resolve_state_dir(self, explicit_dir: Path | None) -> Path:
        env_override = (str(get_setting("governance_state_dir", "")) or "").strip()
        candidates: list[Path] = []
        if explicit_dir:
            candidates.append(Path(explicit_dir))
        if env_override:
            candidates.append(Path(env_override).expanduser())
        candidates.append(STATE_DIR)
        candidates.append(Path.cwd() / "state")

        for cand in candidates:
            try:
                cand.mkdir(parents=True, exist_ok=True)
                return cand
            except Exception:
                continue
        return Path.cwd()
