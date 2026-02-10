"""
Test worker status API endpoint.
"""
import json
import pytest
from http.server import HTTPServer
from orchestrator.api.server import make_handler


def test_worker_status_route_exists():
    """Test that the worker status route is registered."""
    handler_class = make_handler(orchestrator=None)
    routes = handler_class.routes
    
    assert ("GET", "/workers/status") in routes, "Worker status route should be registered"


def test_worker_status_without_orchestrator():
    """Test that the endpoint gracefully handles missing orchestrator."""
    handler_class = make_handler(orchestrator=None)
    
    # Create a mock request handler
    class MockHandler:
        def __init__(self):
            self.path = "/workers/status"
            self.status_code = None
            self.response_data = None
        
        def send_response(self, code):
            self.status_code = code
        
        def send_header(self, key, value):
            pass
        
        def end_headers(self):
            pass
        
        def wfile_write(self, data):
            self.response_data = json.loads(data.decode("utf-8"))
    
    # Mock the response functions
    mock_handler = MockHandler()
    original_wfile_write = mock_handler.wfile_write
    
    def mock_send_json(handler, status, payload):
        handler.status_code = status
        handler.response_data = payload
    
    # Test the handler
    import orchestrator.api.server as server_module
    original_send_json = server_module._send_json
    server_module._send_json = mock_send_json
    
    try:
        worker_status_handler = handler_class.routes[("GET", "/workers/status")]
        worker_status_handler(mock_handler)
        
        assert mock_handler.status_code == 503, "Should return 503 when orchestrator is unavailable"
        assert mock_handler.response_data["ok"] is False
        assert "not available" in mock_handler.response_data["error"].lower()
    finally:
        server_module._send_json = original_send_json


def test_worker_status_response_structure():
    """Test that the response has the expected structure when orchestrator is available."""
    # Create a minimal mock orchestrator
    class MockAwareness:
        def __init__(self):
            self._start_time = 0
            self._total_completed = 5
            self._total_failed = 1
            self._task_durations = [100.0, 150.0, 200.0]
            self._completed = []
            self._failed = []
    
    class MockWorkerManager:
        def __init__(self):
            self.active_workers = {}
            self.pending_workers = set()
    
    class MockProvider:
        def get_tasks(self):
            return []
    
    class MockOrchestrator:
        def __init__(self):
            self.worker_manager = MockWorkerManager()
            self.awareness = MockAwareness()
            self.provider = MockProvider()
    
    handler_class = make_handler(orchestrator=MockOrchestrator())
    
    class MockHandler:
        def __init__(self):
            self.path = "/workers/status"
            self.status_code = None
            self.response_data = None
    
    def mock_send_json(handler, status, payload):
        handler.status_code = status
        handler.response_data = payload
    
    import orchestrator.api.server as server_module
    original_send_json = server_module._send_json
    server_module._send_json = mock_send_json
    
    try:
        mock_handler = MockHandler()
        worker_status_handler = handler_class.routes[("GET", "/workers/status")]
        worker_status_handler(mock_handler)
        
        assert mock_handler.status_code == 200, "Should return 200 when orchestrator is available"
        assert mock_handler.response_data["ok"] is True
        
        # Check expected keys in response
        expected_keys = [
            "activeWorkers",
            "queueDepth",
            "queuedTasks",
            "recentCompletions",
            "recentFailures",
            "metrics",
            "alerts",
            "timestamp",
        ]
        
        for key in expected_keys:
            assert key in mock_handler.response_data, f"Response should contain '{key}' field"
        
        # Check metrics structure
        metrics = mock_handler.response_data["metrics"]
        assert "uptimeSeconds" in metrics
        assert "totalCompleted" in metrics
        assert "totalFailed" in metrics
        assert "activeCount" in metrics
        assert "queueDepth" in metrics
        
        assert metrics["totalCompleted"] == 5
        assert metrics["totalFailed"] == 1
        
    finally:
        server_module._send_json = original_send_json


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
