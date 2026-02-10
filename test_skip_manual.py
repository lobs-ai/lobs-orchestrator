#!/usr/bin/env python3
"""Manual test script for prerequisite skip functionality.

Run this to verify that skip_prereqs logging works correctly.
"""

import json
import logging
import sys
from pathlib import Path

# Set up logging to see warnings
logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')

# Add orchestrator to path
sys.path.insert(0, str(Path(__file__).parent))

from orchestrator.utils.executables import which, check_node_available
from orchestrator.utils.settings import save_settings, load_settings


def test_skip_all():
    """Test skip_prereqs=true."""
    print("\n=== Test 1: skip_prereqs=true ===")
    save_settings({"skip_prereqs": True})
    
    result = which("fake-tool-xyz")
    print(f"which('fake-tool-xyz') returned: {result}")
    assert result is not None, "Should return a path when skipping"
    
    result = check_node_available()
    print(f"check_node_available() returned: {result}")
    assert result is True, "Should return True when skipping"
    
    print("✓ Test passed")


def test_skip_specific():
    """Test skip_prereqs_list."""
    print("\n=== Test 2: skip_prereqs_list=['gh'] ===")
    save_settings({"skip_prereqs": False, "skip_prereqs_list": ["gh"]})
    
    result = which("gh")
    print(f"which('gh') returned: {result}")
    assert result is not None, "Should return a path for 'gh' when in skip list"
    
    result = which("fake-tool-xyz")
    print(f"which('fake-tool-xyz') returned: {result}")
    assert result is None, "Should return None for tools not in skip list"
    
    print("✓ Test passed")


def test_no_skip():
    """Test normal behavior without skip."""
    print("\n=== Test 3: No skip settings ===")
    save_settings({"skip_prereqs": False, "skip_prereqs_list": []})
    
    result = which("fake-tool-xyz")
    print(f"which('fake-tool-xyz') returned: {result}")
    assert result is None, "Should return None for nonexistent tools"
    
    print("✓ Test passed")


def main():
    """Run all manual tests."""
    print("Manual Test: Prerequisite Skip Functionality")
    print("=" * 50)
    
    # Save original settings
    original_settings = load_settings()
    
    try:
        test_skip_all()
        test_skip_specific()
        test_no_skip()
        
        print("\n" + "=" * 50)
        print("✓ All tests passed!")
        print("\nCheck the output above for warning messages.")
        print("You should see warnings for Test 1 and Test 2.")
        
    finally:
        # Restore original settings
        save_settings(original_settings)
        print("\n✓ Original settings restored")


if __name__ == "__main__":
    main()
