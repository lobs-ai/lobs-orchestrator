import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from orchestrator.services.github_onboarding import create_project_from_github
from orchestrator.services.scanner import Scanner
from orchestrator.services.control import ControlManager
from orchestrator.utils.settings import get_setting

logger = logging.getLogger(__name__)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("Invalid JSON body")


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    # CORS for local dashboard dev
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


class DashboardAPIHandler(BaseHTTPRequestHandler):
    # Injected by factory
    routes: dict[tuple[str, str], Callable[[BaseHTTPRequestHandler], None]] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Route http.server logs through logging
        logger.info("dashboard-api: " + format % args)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        handler = self.routes.get(("GET", self.path))
        if not handler:
            _send_json(self, 404, {"ok": False, "error": "Not found"})
            return
        try:
            handler(self)
        except Exception as e:
            logger.error(f"GET {self.path} failed: {e}")
            _send_json(self, 500, {"ok": False, "error": str(e)})

    def do_POST(self) -> None:  # noqa: N802
        handler = self.routes.get(("POST", self.path))
        if not handler:
            _send_json(self, 404, {"ok": False, "error": "Not found"})
            return
        try:
            handler(self)
        except ValueError as e:
            _send_json(self, 400, {"ok": False, "error": str(e)})
        except Exception as e:
            logger.error(f"POST {self.path} failed: {e}", exc_info=True)
            _send_json(self, 500, {"ok": False, "error": str(e)})


def make_handler(orchestrator: Any = None) -> type[DashboardAPIHandler]:
    class _Handler(DashboardAPIHandler):
        pass

    def health(handler: BaseHTTPRequestHandler) -> None:
        _send_json(handler, 200, {"ok": True})

    scanner = Scanner()
    control = ControlManager()

    def create_from_github(handler: BaseHTTPRequestHandler) -> None:
        data = _read_json(handler)
        url = data.get("url")
        clone_path = data.get("clonePath")
        sync_issues = bool(data.get("syncIssues", False))

        if not url or not isinstance(url, str):
            raise ValueError("Missing required field: url")

        result = create_project_from_github(url=url, clone_path=clone_path, sync_issues=sync_issues)
        _send_json(handler, 200, result)

    def create_from_github_batch(handler: BaseHTTPRequestHandler) -> None:
        """Create multiple projects from GitHub repos sequentially.
        
        Request body:
        {
            "repos": [
                {"url": "...", "clonePath": "...", "syncIssues": false},
                {"url": "...", "clonePath": "...", "syncIssues": true},
                ...
            ]
        }
        
        Response:
        {
            "ok": true,
            "results": [
                {"ok": true, "project": {...}},
                {"ok": false, "error": "..."},
                ...
            ],
            "summary": {
                "total": 3,
                "succeeded": 2,
                "failed": 1
            }
        }
        """
        data = _read_json(handler)
        repos = data.get("repos")
        
        if not repos or not isinstance(repos, list):
            raise ValueError("Missing required field: repos (must be a list)")
        
        if len(repos) == 0:
            raise ValueError("repos list cannot be empty")
        
        results = []
        succeeded = 0
        failed = 0
        
        # Process repos sequentially
        for i, repo_config in enumerate(repos):
            if not isinstance(repo_config, dict):
                results.append({
                    "ok": False,
                    "error": f"Invalid repo config at index {i}: must be an object"
                })
                failed += 1
                continue
            
            url = repo_config.get("url")
            clone_path = repo_config.get("clonePath")
            sync_issues = bool(repo_config.get("syncIssues", False))
            
            if not url or not isinstance(url, str):
                results.append({
                    "ok": False,
                    "error": f"Missing or invalid 'url' field at index {i}"
                })
                failed += 1
                continue
            
            try:
                logger.info(f"Processing repo {i+1}/{len(repos)}: {url}")
                result = create_project_from_github(
                    url=url,
                    clone_path=clone_path,
                    sync_issues=sync_issues
                )
                results.append(result)
                succeeded += 1
                logger.info(f"Successfully created project from {url}")
            except Exception as e:
                logger.error(f"Failed to create project from {url}: {e}", exc_info=True)
                results.append({
                    "ok": False,
                    "error": str(e),
                    "url": url
                })
                failed += 1
        
        response = {
            "ok": True,
            "results": results,
            "summary": {
                "total": len(repos),
                "succeeded": succeeded,
                "failed": failed
            }
        }
        
        _send_json(handler, 200, response)

    def get_projects(handler: BaseHTTPRequestHandler) -> None:
        projects = scanner.get_projects()
        _send_json(handler, 200, {"ok": True, "projects": projects})

    def update_project(handler: BaseHTTPRequestHandler) -> None:
        data = _read_json(handler)
        project_id = data.get("projectId")
        updates = data.get("updates")

        if not project_id or not isinstance(project_id, str):
            raise ValueError("Missing required field: projectId")
        if not isinstance(updates, dict):
            raise ValueError("Missing required field: updates")

        control.request_op({
            "type": "update_project",
            "project_id": project_id,
            "updates": updates,
        })
        _send_json(handler, 200, {"ok": True})

    def get_worker_status(handler: BaseHTTPRequestHandler) -> None:
        """Get current worker status including active workers, queue depth, and recent completions."""
        if not orchestrator:
            _send_json(handler, 503, {"ok": False, "error": "Orchestrator not available"})
            return
        
        import time
        from datetime import datetime, timezone
        
        # Get active workers from WorkerManager
        worker_manager = orchestrator.worker_manager
        active_workers_data = []
        
        for task_id, (process, project_id, log_file, start_time, agent_id, task_title, agent_template, worker_id) in worker_manager.active_workers.items():
            duration_sec = int(time.time() - start_time)
            active_workers_data.append({
                "taskId": task_id,
                "projectId": project_id,
                "taskTitle": task_title,
                "agentId": agent_id,
                "agentTemplate": agent_template,
                "workerId": worker_id,
                "durationSeconds": duration_sec,
                "state": "running",
            })
        
        # Add pending workers (syncing/finalizing)
        for task_id in worker_manager.pending_workers:
            if task_id not in worker_manager.active_workers:
                active_workers_data.append({
                    "taskId": task_id,
                    "state": "syncing/finalizing",
                })
        
        # Get queue depth from provider
        try:
            eligible_tasks = orchestrator.provider.get_tasks()
            queue_depth = len(eligible_tasks)
            queued_tasks = [
                {
                    "taskId": task.get("id", "unknown"),
                    "projectId": task.get("projectId", "unknown"),
                    "title": task.get("title", task.get("prompt", "Untitled"))[:100],
                    "kind": task.get("kind", "task"),
                }
                for task in eligible_tasks[:10]  # Only show first 10 queued tasks
            ]
        except Exception as e:
            logger.error(f"Failed to get queue depth: {e}")
            queue_depth = 0
            queued_tasks = []
        
        # Get recent completions from AwarenessMonitor
        awareness = orchestrator.awareness
        recent_completions = []
        
        if awareness:
            for item in list(awareness._completed)[-10:]:  # Last 10 completions
                recent_completions.append({
                    "taskId": item.task_id,
                    "projectId": item.project_id,
                    "title": item.title,
                    "agentType": item.agent_type,
                    "completedAt": item.completed_at,
                    "durationSeconds": int(item.duration_sec) if item.duration_sec else None,
                })
        
        # Get recent failures
        recent_failures = []
        if awareness:
            for item in list(awareness._failed)[-5:]:  # Last 5 failures
                recent_failures.append({
                    "taskId": item.task_id,
                    "projectId": item.project_id,
                    "title": item.title,
                    "agentType": item.agent_type,
                    "completedAt": item.completed_at,
                })
        
        # System metrics
        uptime_sec = int(time.time() - awareness._start_time) if awareness else 0
        avg_duration = None
        if awareness and awareness._task_durations:
            avg_duration = int(sum(awareness._task_durations) / len(awareness._task_durations))
        
        metrics = {
            "uptimeSeconds": uptime_sec,
            "totalCompleted": awareness._total_completed if awareness else 0,
            "totalFailed": awareness._total_failed if awareness else 0,
            "averageDurationSeconds": avg_duration,
            "activeCount": len(worker_manager.active_workers),
            "pendingCount": len(worker_manager.pending_workers),
            "queueDepth": queue_depth,
        }
        
        # Alerts (basic heuristics)
        alerts = []
        
        # Alert if queue is backing up
        if queue_depth > 10:
            alerts.append({
                "level": "warning",
                "message": f"Queue depth is high: {queue_depth} task(s) waiting",
            })
        
        # Alert if recent failures
        if recent_failures:
            alerts.append({
                "level": "info",
                "message": f"{len(recent_failures)} recent failure(s)",
            })
        
        # Alert if worker has been running for a long time
        for worker in active_workers_data:
            if worker.get("durationSeconds", 0) > 1800:  # 30 minutes
                alerts.append({
                    "level": "warning",
                    "message": f"Worker for {worker['taskId'][:8]} has been running for {worker['durationSeconds'] // 60} minutes",
                })
        
        response = {
            "ok": True,
            "activeWorkers": active_workers_data,
            "queueDepth": queue_depth,
            "queuedTasks": queued_tasks,
            "recentCompletions": recent_completions,
            "recentFailures": recent_failures,
            "metrics": metrics,
            "alerts": alerts,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        _send_json(handler, 200, response)

    _Handler.routes = {
        ("GET", "/health"): health,
        ("GET", "/projects"): get_projects,
        ("GET", "/workers/status"): get_worker_status,
        ("POST", "/projects/update"): update_project,
        ("POST", "/projects/create-from-github"): create_from_github,
        ("POST", "/projects/create-from-github-batch"): create_from_github_batch,
    }

    return _Handler


class DashboardAPIServer:
    def __init__(self, orchestrator: Any = None) -> None:
        self.host = get_setting("dashboard_api_host", "127.0.0.1")
        self.port = int(get_setting("dashboard_api_port", 7171))
        self.orchestrator = orchestrator
        self._server = ThreadingHTTPServer((self.host, self.port), make_handler(orchestrator))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        logger.info(f"Dashboard API listening on http://{self.host}:{self.port}")
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
