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


def make_handler() -> type[DashboardAPIHandler]:
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

    _Handler.routes = {
        ("GET", "/health"): health,
        ("GET", "/projects"): get_projects,
        ("POST", "/projects/update"): update_project,
        ("POST", "/projects/create-from-github"): create_from_github,
        ("POST", "/projects/create-from-github-batch"): create_from_github_batch,
    }

    return _Handler


class DashboardAPIServer:
    def __init__(self) -> None:
        self.host = get_setting("dashboard_api_host", "127.0.0.1")
        self.port = int(get_setting("dashboard_api_port", 7171))
        self._server = ThreadingHTTPServer((self.host, self.port), make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        logger.info(f"Dashboard API listening on http://{self.host}:{self.port}")
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
