"""
Tests for memory monitoring functionality.
"""

import pytest
import time
from orchestrator.utils.memory_monitor import (
    MemoryMonitor,
    MemorySnapshot,
    get_memory_monitor,
    init_memory_monitoring,
)


class TestMemorySnapshot:
    """Test MemorySnapshot dataclass."""
    
    def test_snapshot_creation(self):
        """Test creating a memory snapshot."""
        snapshot = MemorySnapshot(
            timestamp="2026-02-12T12:00:00Z",
            rss_mb=100.0,
            vms_mb=200.0,
            percent=5.0,
            available_mb=1000.0,
            total_mb=4000.0,
        )
        
        assert snapshot.rss_mb == 100.0
        assert snapshot.vms_mb == 200.0
        assert snapshot.available_mb == 1000.0
        assert snapshot.total_mb == 4000.0
    
    def test_snapshot_str(self):
        """Test string representation of snapshot."""
        snapshot = MemorySnapshot(
            timestamp="2026-02-12T12:00:00Z",
            rss_mb=100.0,
            vms_mb=200.0,
            percent=5.0,
            available_mb=1000.0,
            total_mb=4000.0,
        )
        
        snapshot_str = str(snapshot)
        assert "RSS: 100.0MB" in snapshot_str
        assert "VMS: 200.0MB" in snapshot_str
        assert "Available: 1000.0MB" in snapshot_str
        assert "4000.0MB" in snapshot_str


class TestMemoryMonitor:
    """Test MemoryMonitor class."""
    
    def test_monitor_creation(self):
        """Test creating a memory monitor."""
        monitor = MemoryMonitor(
            min_free_mb_for_spawn=500,
            critical_free_mb=200,
            enable_watchdog=False,  # Disable watchdog for tests
        )
        
        assert monitor.min_free_mb_for_spawn == 500
        assert monitor.critical_free_mb == 200
        assert monitor.enable_watchdog is False
    
    def test_get_snapshot(self):
        """Test getting a memory snapshot."""
        monitor = MemoryMonitor(enable_watchdog=False)
        snapshot = monitor.get_snapshot()
        
        # Basic validation - should return real values
        assert snapshot.rss_mb > 0
        assert snapshot.vms_mb > 0
        assert snapshot.total_mb > 0
        assert snapshot.available_mb >= 0
        assert 0 <= snapshot.percent <= 100
    
    def test_high_water_mark_tracking(self):
        """Test that high-water marks are tracked."""
        monitor = MemoryMonitor(enable_watchdog=False)
        
        # Get initial snapshot
        snapshot1 = monitor.get_snapshot()
        initial_max_rss = monitor.max_rss_mb
        initial_max_vms = monitor.max_vms_mb
        
        # High-water marks should be updated
        assert initial_max_rss >= snapshot1.rss_mb
        assert initial_max_vms >= snapshot1.vms_mb
        
        # Get another snapshot
        snapshot2 = monitor.get_snapshot()
        
        # High-water marks should be monotonically increasing
        assert monitor.max_rss_mb >= initial_max_rss
        assert monitor.max_vms_mb >= initial_max_vms
    
    def test_check_can_spawn_worker_sufficient_memory(self):
        """Test worker spawn check with sufficient memory."""
        # Use very low threshold so test passes on any system
        monitor = MemoryMonitor(
            min_free_mb_for_spawn=1,  # 1MB - should always pass
            enable_watchdog=False,
        )
        
        can_spawn, reason = monitor.check_can_spawn_worker()
        
        # Should allow spawn with such a low threshold
        assert can_spawn is True
        assert "passed" in reason.lower()
    
    def test_check_can_spawn_worker_insufficient_memory(self):
        """Test worker spawn check with insufficient memory."""
        # Use impossibly high threshold so test fails
        monitor = MemoryMonitor(
            min_free_mb_for_spawn=999999,  # 999GB - should always fail
            enable_watchdog=False,
        )
        
        can_spawn, reason = monitor.check_can_spawn_worker()
        
        # Should block spawn with such a high threshold
        assert can_spawn is False
        assert "insufficient memory" in reason.lower()
    
    def test_check_can_spawn_worker_memory_pressure(self):
        """Test worker spawn check with memory pressure."""
        monitor = MemoryMonitor(
            min_free_mb_for_spawn=1,
            enable_watchdog=False,
        )
        
        # Simulate memory pressure
        monitor._memory_pressure = True
        
        can_spawn, reason = monitor.check_can_spawn_worker()
        
        # Should block spawn due to memory pressure
        assert can_spawn is False
        assert "memory pressure" in reason.lower()
    
    def test_log_snapshot(self):
        """Test logging a memory snapshot."""
        monitor = MemoryMonitor(enable_watchdog=False)
        
        # Should not raise an exception
        snapshot = monitor.log_snapshot("TEST")
        
        assert snapshot is not None
        assert snapshot.rss_mb > 0
    
    def test_log_snapshot_forced(self):
        """Test forcing a log snapshot."""
        monitor = MemoryMonitor(enable_watchdog=False)
        
        # Force logging should always log
        snapshot = monitor.log_snapshot("TEST-FORCE", force=True)
        
        assert snapshot is not None
    
    def test_log_before_after_spawn(self):
        """Test logging before/after worker spawn."""
        monitor = MemoryMonitor(enable_watchdog=False)
        task_id = "test-task-123"
        
        # Should not raise exceptions
        before = monitor.log_before_spawn(task_id)
        after = monitor.log_after_spawn(task_id)
        
        assert before is not None
        assert after is not None
    
    def test_get_worker_subprocess_memory_invalid_pid(self):
        """Test getting memory for non-existent PID."""
        monitor = MemoryMonitor(enable_watchdog=False)
        
        # Use impossible PID
        rss_mb = monitor.get_worker_subprocess_memory(999999)
        
        # Should return None for invalid PID
        assert rss_mb is None
    
    def test_watchdog_not_started_by_default(self):
        """Test that watchdog doesn't start automatically."""
        monitor = MemoryMonitor(enable_watchdog=False)
        
        assert monitor._watchdog_thread is None
    
    def test_watchdog_lifecycle(self):
        """Test starting and stopping the watchdog."""
        monitor = MemoryMonitor(
            enable_watchdog=True,
            watchdog_interval=1,  # 1 second for fast tests
        )
        
        # Start watchdog
        monitor.start_watchdog()
        
        # Should have started
        assert monitor._watchdog_thread is not None
        assert monitor._watchdog_thread.is_alive()
        
        # Wait a moment
        time.sleep(0.5)
        
        # Stop watchdog
        monitor.stop_watchdog()
        
        # Should have stopped
        time.sleep(0.5)
        assert monitor._watchdog_thread is None or not monitor._watchdog_thread.is_alive()
    
    def test_watchdog_detects_memory_pressure(self):
        """Test that watchdog detects low memory."""
        monitor = MemoryMonitor(
            enable_watchdog=True,
            watchdog_interval=1,
            critical_free_mb=999999,  # Impossibly high threshold
        )
        
        # Start watchdog
        monitor.start_watchdog()
        
        # Wait for watchdog to check
        time.sleep(2)
        
        # Should have detected memory pressure
        assert monitor._memory_pressure is True
        
        # Stop watchdog
        monitor.stop_watchdog()


class TestGlobalMonitor:
    """Test global monitor singleton."""
    
    def test_get_memory_monitor_singleton(self):
        """Test that get_memory_monitor returns same instance."""
        # Reset global state
        import orchestrator.utils.memory_monitor as mm
        mm._monitor = None
        
        monitor1 = get_memory_monitor()
        monitor2 = get_memory_monitor()
        
        # Should be same instance
        assert monitor1 is monitor2
    
    def test_init_memory_monitoring(self):
        """Test initializing memory monitoring with custom settings."""
        # Reset global state
        import orchestrator.utils.memory_monitor as mm
        mm._monitor = None
        
        monitor = init_memory_monitoring(
            min_free_mb_for_spawn=750,
            critical_free_mb=300,
            enable_watchdog=False,
        )
        
        assert monitor.min_free_mb_for_spawn == 750
        assert monitor.critical_free_mb == 300
        assert monitor.enable_watchdog is False
        
        # Clean up
        monitor.stop_watchdog()


class TestMemoryMonitorIntegration:
    """Integration tests for memory monitoring."""
    
    def test_monitor_tracks_memory_over_time(self):
        """Test that monitor tracks memory usage over multiple snapshots."""
        monitor = MemoryMonitor(enable_watchdog=False)
        
        snapshots = []
        for _ in range(5):
            snapshot = monitor.get_snapshot()
            snapshots.append(snapshot)
            time.sleep(0.1)
        
        # Should have collected 5 snapshots
        assert len(snapshots) == 5
        
        # All snapshots should have valid data
        for snapshot in snapshots:
            assert snapshot.rss_mb > 0
            assert snapshot.total_mb > 0
        
        # High-water marks should be set
        assert monitor.max_rss_mb > 0
        assert monitor.max_vms_mb > 0
    
    def test_periodic_logging_reduces_spam(self):
        """Test that periodic logging doesn't log every iteration."""
        monitor = MemoryMonitor(enable_watchdog=False)
        monitor._log_every_n_iterations = 5
        
        # Log multiple times without force
        for i in range(10):
            monitor.log_snapshot("TEST")
        
        # Iteration count should have increased
        assert monitor._iteration_count == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
