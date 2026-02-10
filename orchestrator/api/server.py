import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from orchestrator.services.github_onboarding import create_project_from_github
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

    def create_from_github(handler: BaseHTTPRequestHandler) -> None:
        data = _read_json(handler)
        url = data.get("url")
        clone_path = data.get("clonePath")
        sync_issues = bool(data.get("syncIssues", False))

        if not url or not isinstance(url, str):
            raise ValueError("Missing required field: url")

        result = create_project_from_github(url=url, clone_path=clone_path, sync_issues=sync_issues)
        _send_json(handler, 200, result)

    _Handler.routes = {
        ("GET", "/health"): health,
        ("POST", "/projects/create-from-github"): create_from_github,
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
