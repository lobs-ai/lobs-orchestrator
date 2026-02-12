"""
Memory monitoring and protection utilities.

Prevents OOM kills by tracking memory usage and enforcing limits.
"""

import logging
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:
    psutil = None

logger = logging.getLogger(__name__)


@dataclass
class MemorySnapshot:
    """Single point-in-time memory measurement."""
    timestamp: str
    rss_mb: float  # Resident set size (physical memory)
    vms_mb: float  # Virtual memory size
    percent: float  # Percentage of system memory
    available_mb: float  # System available memory
    total_mb: float  # Total system memory
    
    def __str__(self) -> str:
        return (
            f"RSS: {self.rss_mb:.1f}MB, "
            f"VMS: {self.vms_mb:.1f}MB, "
            f"Available: {self.available_mb:.1f}MB / {self.total_mb:.1f}MB"
        )


class MemoryMonitor:
    """
    Tracks memory usage and enforces safety limits.
    
    Features:
    - Periodic memory snapshot logging
    - High-water mark tracking
    - System memory threshold checks
    - Worker spawn safety checks
    - Optional watchdog thread for automatic protection
    """
    
    def __init__(
        self,
        min_free_mb_for_spawn: int = 500,
        critical_free_mb: int = 200,
        enable_watchdog: bool = True,
        watchdog_interval: int = 30,
    ):
        """
        Args:
            min_free_mb_for_spawn: Minimum free memory (MB) required to spawn workers
            critical_free_mb: Memory threshold (MB) that triggers warnings
            enable_watchdog: Whether to run background watchdog thread
            watchdog_interval: Watchdog check interval (seconds)
        """
        self._psutil_available = psutil is not None
        self.process = psutil.Process(os.getpid()) if self._psutil_available else None
        self.min_free_mb_for_spawn = min_free_mb_for_spawn
        self.critical_free_mb = critical_free_mb
        
        # High-water marks
        self.max_rss_mb = 0.0
        self.max_vms_mb = 0.0
        
        # Watchdog
        self.enable_watchdog = enable_watchdog
        self.watchdog_interval = watchdog_interval
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self._memory_pressure = False
        
        # Iteration counter for periodic logging
        self._iteration_count = 0
        self._log_every_n_iterations = 10  # Log every 10th iteration

        if not self._psutil_available:
            logger.warning("[MEMORY] psutil not installed; memory safety checks are disabled.")

    @staticmethod
    def _fallback_total_mb() -> float:
        """Best-effort total memory when psutil is unavailable."""
        if hasattr(os, "sysconf"):
            try:
                pages = os.sysconf("SC_PHYS_PAGES")
                page_size = os.sysconf("SC_PAGE_SIZE")
                if isinstance(pages, int) and pages > 0 and isinstance(page_size, int) and page_size > 0:
                    return (pages * page_size) / (1024 * 1024)
            except (ValueError, OSError):
                pass
        return 8192.0
        
    def get_snapshot(self) -> MemorySnapshot:
        """Get current memory usage snapshot."""
        if not self._psutil_available or self.process is None:
            total_mb = self._fallback_total_mb()
            return MemorySnapshot(
                timestamp=datetime.now(timezone.utc).isoformat(),
                rss_mb=0.0,
                vms_mb=0.0,
                percent=0.0,
                available_mb=total_mb,
                total_mb=total_mb,
            )

        mem_info = self.process.memory_info()
        sys_mem = psutil.virtual_memory()
        
        rss_mb = mem_info.rss / (1024 * 1024)
        vms_mb = mem_info.vms / (1024 * 1024)
        available_mb = sys_mem.available / (1024 * 1024)
        total_mb = sys_mem.total / (1024 * 1024)
        percent = self.process.memory_percent()
        
        # Update high-water marks
        self.max_rss_mb = max(self.max_rss_mb, rss_mb)
        self.max_vms_mb = max(self.max_vms_mb, vms_mb)
        
        return MemorySnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
            rss_mb=rss_mb,
            vms_mb=vms_mb,
            percent=percent,
            available_mb=available_mb,
            total_mb=total_mb,
        )
    
    def log_snapshot(self, label: str = "MEMORY", force: bool = False) -> MemorySnapshot:
        """
        Log current memory usage.
        
        Args:
            label: Log label prefix
            force: Force logging even if not at periodic interval
            
        Returns:
            Current memory snapshot
        """
        snapshot = self.get_snapshot()
        
        # Periodic logging to reduce log spam
        self._iteration_count += 1
        should_log = force or (self._iteration_count % self._log_every_n_iterations == 0)
        
        if should_log or snapshot.available_mb < self.critical_free_mb:
            level = logging.WARNING if snapshot.available_mb < self.critical_free_mb else logging.INFO
            logger.log(
                level,
                f"[{label}] {snapshot} "
                f"(HWM: RSS {self.max_rss_mb:.1f}MB, VMS {self.max_vms_mb:.1f}MB)"
            )
            
            if snapshot.available_mb < self.critical_free_mb:
                logger.warning(
                    f"[{label}] ⚠️  CRITICAL: System memory below {self.critical_free_mb}MB! "
                    f"OOM kill risk is HIGH."
                )
        
        return snapshot
    
    def check_can_spawn_worker(self) -> tuple[bool, str]:
        """
        Check if it's safe to spawn a new worker.
        
        Returns:
            (can_spawn, reason) tuple
        """
        if not self._psutil_available:
            return True, "Memory checks disabled (psutil unavailable)"

        snapshot = self.get_snapshot()
        
        if snapshot.available_mb < self.min_free_mb_for_spawn:
            reason = (
                f"Insufficient memory: {snapshot.available_mb:.1f}MB available, "
                f"need {self.min_free_mb_for_spawn}MB to spawn worker"
            )
            logger.warning(f"[MEMORY] Worker spawn blocked: {reason}")
            return False, reason
        
        if self._memory_pressure:
            reason = "Memory pressure detected by watchdog"
            logger.warning(f"[MEMORY] Worker spawn blocked: {reason}")
            return False, reason
        
        return True, "Memory check passed"
    
    def log_before_spawn(self, task_id: str) -> MemorySnapshot:
        """Log memory before spawning a worker."""
        logger.info(f"[MEMORY] Before spawning worker for task {task_id[:8]}...")
        return self.log_snapshot("PRE-SPAWN", force=True)
    
    def log_after_spawn(self, task_id: str) -> MemorySnapshot:
        """Log memory after spawning a worker."""
        logger.info(f"[MEMORY] After spawning worker for task {task_id[:8]}...")
        return self.log_snapshot("POST-SPAWN", force=True)
    
    def log_startup_info(self):
        """Log system memory info at startup."""
        snapshot = self.get_snapshot()
        sys_mem_percent = 0.0
        if self._psutil_available and psutil is not None:
            sys_mem_percent = psutil.virtual_memory().percent

        logger.info("=" * 60)
        logger.info("[MEMORY] System Memory Report")
        logger.info("-" * 60)
        logger.info(f"Total RAM: {snapshot.total_mb:.1f}MB")
        logger.info(f"Available: {snapshot.available_mb:.1f}MB ({sys_mem_percent}% used)")
        logger.info(f"Orchestrator Process: RSS {snapshot.rss_mb:.1f}MB, VMS {snapshot.vms_mb:.1f}MB")
        logger.info(f"Platform: {platform.system()} {platform.release()}")
        logger.info("-" * 60)
        
        # Warnings for low-memory systems
        if snapshot.total_mb < 8192:  # Less than 8GB
            logger.warning(
                f"⚠️  Low memory system detected ({snapshot.total_mb:.0f}MB total). "
                "Recommend reducing max_concurrent_workers to 1."
            )
        
        if snapshot.total_mb < 4096:  # Less than 4GB
            logger.warning(
                "⚠️  VERY LOW MEMORY system! (<4GB) "
                "OOM kills are likely. Consider:"
            )
            logger.warning("   - Set max_concurrent_workers: 1")
            logger.warning("   - Disable ollama_advisor_enabled")
            logger.warning("   - Increase poll_interval to 30+")
        
        if snapshot.available_mb < 1000:  # Less than 1GB free
            logger.warning(
                f"⚠️  Very low available memory ({snapshot.available_mb:.0f}MB). "
                "Consider restarting other applications."
            )
        
        logger.info("=" * 60)
    
    def start_watchdog(self):
        """Start the memory watchdog thread."""
        if not self.enable_watchdog:
            logger.info("[MEMORY] Watchdog disabled")
            return
        
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            logger.warning("[MEMORY] Watchdog already running")
            return
        
        logger.info(
            f"[MEMORY] Starting memory watchdog "
            f"(check every {self.watchdog_interval}s, "
            f"critical threshold: {self.critical_free_mb}MB)"
        )
        
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="MemoryWatchdog"
        )
        self._watchdog_thread.start()
    
    def stop_watchdog(self):
        """Stop the memory watchdog thread."""
        if not self._watchdog_thread:
            return
        
        logger.info("[MEMORY] Stopping memory watchdog...")
        self._watchdog_stop.set()
        self._watchdog_thread.join(timeout=5.0)
        self._watchdog_thread = None
        logger.info("[MEMORY] Watchdog stopped")
    
    def _watchdog_loop(self):
        """Background watchdog loop."""
        logger.info("[MEMORY] Watchdog thread started")
        
        while not self._watchdog_stop.is_set():
            try:
                snapshot = self.get_snapshot()
                
                # Check for memory pressure
                if snapshot.available_mb < self.critical_free_mb:
                    if not self._memory_pressure:
                        logger.warning(
                            f"[MEMORY-WATCHDOG] ⚠️  MEMORY PRESSURE DETECTED! "
                            f"Available: {snapshot.available_mb:.1f}MB < {self.critical_free_mb}MB. "
                            f"Blocking new worker spawns until memory recovers."
                        )
                        self._memory_pressure = True
                else:
                    if self._memory_pressure:
                        logger.info(
                            f"[MEMORY-WATCHDOG] Memory pressure cleared. "
                            f"Available: {snapshot.available_mb:.1f}MB"
                        )
                        self._memory_pressure = False
                
                # Log watchdog check periodically
                if self._iteration_count % 10 == 0:  # Every 10 checks (~5 minutes at 30s interval)
                    logger.debug(
                        f"[MEMORY-WATCHDOG] Check: {snapshot} "
                        f"Pressure: {self._memory_pressure}"
                    )
                
            except Exception as e:
                logger.error(f"[MEMORY-WATCHDOG] Error: {e}", exc_info=True)
            
            self._watchdog_stop.wait(self.watchdog_interval)
        
        logger.info("[MEMORY] Watchdog thread stopped")
    
    def get_worker_subprocess_memory(self, pid: int) -> Optional[float]:
        """
        Get memory usage of a worker subprocess.
        
        Args:
            pid: Process ID
            
        Returns:
            RSS in MB, or None if process not found
        """
        if not self._psutil_available:
            return None
        try:
            proc = psutil.Process(pid)
            mem_info = proc.memory_info()
            return mem_info.rss / (1024 * 1024)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
    
    def log_worker_memory(self, worker_id: str, pid: int):
        """Log memory usage of a worker subprocess."""
        rss_mb = self.get_worker_subprocess_memory(pid)
        if rss_mb is not None:
            logger.info(
                f"[MEMORY] Worker {worker_id[:8]} (PID {pid}): RSS {rss_mb:.1f}MB"
            )
        else:
            logger.warning(
                f"[MEMORY] Worker {worker_id[:8]} (PID {pid}): Unable to read memory"
            )


# Global singleton instance
_monitor: Optional[MemoryMonitor] = None


def get_memory_monitor() -> MemoryMonitor:
    """Get or create the global memory monitor instance."""
    global _monitor
    if _monitor is None:
        # Default configuration - can be overridden via settings
        _monitor = MemoryMonitor(
            min_free_mb_for_spawn=500,
            critical_free_mb=200,
            enable_watchdog=True,
            watchdog_interval=30,
        )
    return _monitor


def init_memory_monitoring(
    min_free_mb_for_spawn: Optional[int] = None,
    critical_free_mb: Optional[int] = None,
    enable_watchdog: bool = True,
    watchdog_interval: int = 30,
):
    """
    Initialize memory monitoring with custom settings.
    
    Should be called early in startup, before worker spawns.
    """
    global _monitor
    
    kwargs = {
        "enable_watchdog": enable_watchdog,
        "watchdog_interval": watchdog_interval,
    }
    
    if min_free_mb_for_spawn is not None:
        kwargs["min_free_mb_for_spawn"] = min_free_mb_for_spawn
    
    if critical_free_mb is not None:
        kwargs["critical_free_mb"] = critical_free_mb
    
    _monitor = MemoryMonitor(**kwargs)
    _monitor.log_startup_info()
    _monitor.start_watchdog()
    
    return _monitor
