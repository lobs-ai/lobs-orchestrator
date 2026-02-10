#!/usr/bin/env python3
"""
Manual test for the worker status API endpoint.
Run this with: python3 test_api_manual.py
"""
import requests
import json
import time


def test_health():
    """Test the health endpoint."""
    print("Testing /health endpoint...")
    try:
        response = requests.get("http://127.0.0.1:7171/health", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Response: {json.dumps(response.json(), indent=2)}")
        return response.status_code == 200
    except Exception as e:
        print(f"Error: {e}")
        return False


def test_worker_status():
    """Test the worker status endpoint."""
    print("\nTesting /workers/status endpoint...")
    try:
        response = requests.get("http://127.0.0.1:7171/workers/status", timeout=5)
        print(f"Status: {response.status_code}")
        data = response.json()
        print(f"Response: {json.dumps(data, indent=2)}")
        
        # Validate structure
        if response.status_code == 200:
            required_keys = [
                "ok", "activeWorkers", "queueDepth", "queuedTasks",
                "recentCompletions", "recentFailures", "metrics", "alerts"
            ]
            missing_keys = [k for k in required_keys if k not in data]
            if missing_keys:
                print(f"❌ Missing keys: {missing_keys}")
                return False
            
            print("\n✅ All required keys present")
            print(f"Active workers: {len(data['activeWorkers'])}")
            print(f"Queue depth: {data['queueDepth']}")
            print(f"Total completed: {data['metrics']['totalCompleted']}")
            print(f"Total failed: {data['metrics']['totalFailed']}")
            print(f"Alerts: {len(data['alerts'])}")
            return True
        elif response.status_code == 503:
            print("⚠️  Orchestrator not available (expected if not running)")
            return True
        else:
            print(f"❌ Unexpected status code: {response.status_code}")
            return False
            
    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to server. Is the orchestrator running?")
        print("   Start it with: python3 main.py")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("Worker Status API Manual Test")
    print("=" * 60)
    print()
    
    health_ok = test_health()
    status_ok = test_worker_status()
    
    print()
    print("=" * 60)
    if health_ok and status_ok:
        print("✅ All tests passed!")
    else:
        print("❌ Some tests failed")
    print("=" * 60)
