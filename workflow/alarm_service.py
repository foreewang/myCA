"""Fail-open alarm hooks and a hardware-independent monitoring loop.

No hook raises into production work. GET reads the store/cache and never resets
an alarm, acknowledges an event, or changes admission/control policies.
"""
from __future__ import annotations

import contextvars
import logging
import os
import queue
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from workflow.alarm_store import AlarmQueryError, AlarmStore, utc_timestamp

logger = logging.getLogger(__name__)
_context: contextvars.ContextVar[tuple[str | None, str | None]] = contextvars.ContextVar(
    "alarm_context", default=(None, None))
_service: AlarmService | None = None

SYSTEM_ALARM_CODES = {
    "UNKNOWN_ERROR": ("system", "P1", "系统发生未分类故障，请根据诊断编号检查本地日志"),
    "ALARM_RECORDING_INTERRUPTED": ("storage", "P1", "报警记录曾中断，期间部分事件可能缺失"),
    "ALARM_MONITOR_FAILED": ("system", "P1", "报警监测执行失败，监测信息可能不完整"),
    "ALARM_DISK_CONFIG_INVALID": ("system", "P2", "磁盘报警阈值配置无效，已使用默认阈值：预警20%、严重10%"),
    "TASK_WORKER_UNAVAILABLE": ("task", "P1", "任务工作线程不可用"),
    "DISK_SPACE_LOW": ("storage", "P2", "存储可用空间不足，请及时清理或扩容"),
    "DISK_SPACE_CRITICAL": ("storage", "P1", "存储可用空间严重不足"),
}


def alarm_catalog() -> dict[str, tuple[str, str, str]]:
    from workflow.alarm_device_codes import DEVICE_ALARM_CODES
    from workflow.alarm_task_codes import TASK_ALARM_CODES
    return {**SYSTEM_ALARM_CODES, **DEVICE_ALARM_CODES, **TASK_ALARM_CODES}


@contextmanager
def alarm_context(task_id: str | None, execution_id: str | None):
    token = _context.set((task_id, execution_id))
    try:
        yield
    finally:
        _context.reset(token)


def get_alarm_context() -> tuple[str | None, str | None]:
    """Capture session origin before handing work to a background thread."""
    return _context.get()


class AlarmService:
    def __init__(self, path: Path, *, device_id: str | None = None,
                 interval: float = 5, warning_percent: float | str = 20, critical_percent: float | str = 10):
        self.path, self.device_id = Path(path), device_id
        self.interval = interval
        self._disk_config_invalid = False
        try:
            warning_percent, critical_percent = float(warning_percent), float(critical_percent)
            if not 0 < critical_percent < warning_percent < 100:
                raise ValueError("alarm disk thresholds require 0 < critical < warning < 100")
        except (TypeError, ValueError, OverflowError):
            warning_percent, critical_percent = 20.0, 10.0
            self._disk_config_invalid = True
            logger.warning("invalid alarm disk thresholds; using warning=20%, critical=10%")
        self.warning_percent, self.critical_percent = warning_percent, critical_percent
        self.store: AlarmStore | None = None
        self._health_lock = threading.RLock()
        self._record_lock = threading.RLock()
        self._active_keys: set[tuple[str, str, str]] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_failure = False
        self._failure_generation = 0
        self._failure_id: str | None = None
        self._sample_time: float | None = None
        self._monitor_status = "starting"
        self._worker: dict[str, Any] = {"status": "unknown", "worker_alive": None}
        self._disk: dict[str, Any] = {"status": "unknown"}
        # This lock protects memory only. Never hold it during SQLite or logging.
        self._submission_lock = threading.Lock()
        self._pending: queue.Queue = queue.Queue(maxsize=4096)
        self._writer: threading.Thread | None = None
        self._writer_stop = threading.Event()
        self._accept_device_events = False
        self._dropped_events = 0
        self._reported_drops = 0
        self._open_store()
        self._condition("ALARM_DISK_CONFIG_INVALID", self._disk_config_invalid, component="alarm_monitor")

    def start_writer(self) -> None:
        """Called during initialization, never from a hardware hook."""
        if self._writer is not None:
            return
        self._writer = threading.Thread(target=self._write_events, name="alarm-writer", daemon=True)
        try:
            self._writer.start()
            with self._submission_lock:
                self._accept_device_events = True
        except Exception:
            self._failed_write()

    def enqueue_device_event(self, code: str, **kwargs: Any) -> None:
        # Capture origin/time now: the writer has neither the caller's context
        # nor its occurrence time. No logging, database access or record lock here.
        context = contextvars.copy_context()
        kwargs.setdefault("occurred_at", utc_timestamp(time.time()))
        with self._submission_lock:
            if not self._accept_device_events or not self._writer or not self._writer.is_alive():
                self._dropped_events += 1
                return
            try:
                self._pending.put_nowait((context, code, kwargs))
            except queue.Full:
                self._dropped_events += 1

    def _write_events(self) -> None:
        while not self._writer_stop.is_set() or not self._pending.empty():
            try:
                context, code, kwargs = self._pending.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if code is None:
                    kwargs["barrier"].set()
                else:
                    context.run(self.record, code, **kwargs)
            except Exception:
                self._failed_write()
            finally:
                self._pending.task_done()

    def flush_device_events(self, timeout: float = 2) -> bool:
        """Wait for prior submissions in tests/maintenance, never in GET or motion."""
        barrier = threading.Event()
        with self._submission_lock:
            if not self._accept_device_events:
                return False
            try:
                self._pending.put_nowait((None, None, {"barrier": barrier}))
            except queue.Full:
                return False
        return barrier.wait(timeout)

    def stop_writer(self, timeout: float = 2) -> None:
        # Device cleanup runs before this call; drain accepted events in FIFO order.
        with self._submission_lock:
            self._accept_device_events = False
            self._writer_stop.set()
        if self._writer and self._writer.is_alive():
            self._writer.join(timeout=timeout)
            if self._writer.is_alive():
                self._failed_write()
                logger.error("alarm writer did not drain before shutdown timeout; pending events may be lost")

    def _open_store(self) -> None:
        try:
            store = AlarmStore(self.path, device_id=self.device_id)
            with self._record_lock:
                self._active_keys = store.active_keys()
                self.store = store
        except Exception:
            self._failed_write()

    def _failed_write(self) -> None:
        with self._health_lock:
            first = not self._write_failure
            self._write_failure = True
            self._failure_generation += 1
            self._failure_id = self._failure_id or uuid4().hex
        if first:
            logger.exception("alarm recording unavailable diagnostic_id=%s", self._failure_id)

    def record(self, code: str, *, component: str, resource: str = "default",
               task_id: str | None = None, execution_id: str | None = None,
               severity: str | None = None, fact: bool = False,
               occurrence_key: str | None = None, recovery: bool = False,
               occurred_at: str | None = None, diagnostic_id: str | None = None) -> str | None:
        try:
            if self.store is None:
                return None
            catalog = alarm_catalog()
            if code not in catalog:
                logger.warning("unregistered alarm code=%s", code)
                code = "UNKNOWN_ERROR"
            category, default_severity, message = catalog[code]
            if severity not in (None, "INFO", "P0", "P1", "P2"):
                severity = None
            ctx_task, ctx_execution = _context.get()
            task_id, execution_id = task_id or ctx_task, execution_id or ctx_execution
            diagnostic_id = diagnostic_id or uuid4().hex
            with self._record_lock:
                key = (code, component, resource)
                # Successful Modbus transactions/frame observations are hot
                # paths. Do not open SQLite when there is nothing to recover.
                if recovery and key not in self._active_keys:
                    return None
                alarm_id = self.store.record(
                    code=code, category=category, severity=severity or default_severity,
                    message=message, component=component, resource=resource,
                    task_id=task_id, execution_id=execution_id, diagnostic_id=diagnostic_id,
                    fact=fact, occurrence_key=occurrence_key, recovery=recovery, occurred_at=occurred_at)
                if recovery:
                    self._active_keys.discard(key)
                elif not fact and alarm_id:
                    self._active_keys.add(key)
            if alarm_id:
                logger.info("alarm code=%s alarm_id=%s diagnostic_id=%s execution_id=%s recovery=%s",
                            code, alarm_id, diagnostic_id, execution_id, recovery,
                            extra={"task_id": task_id or "-"})
            return alarm_id
        except Exception:
            self._failed_write()
            return None

    def start(self) -> None:
        self.start_writer()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="alarm-monitor", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._monitor_status = "failed"
            logger.exception("alarm monitor could not start")
            self.record("ALARM_MONITOR_FAILED", component="alarm_monitor")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        with self._health_lock:
            self._monitor_status = "stopped"

    def _condition(self, code: str, active: bool, *, component: str) -> None:
        with self._record_lock:
            # Explicit failure hooks may change this condition between monitor
            # samples. Compare persisted state, not only the last sample.
            if ((code, component, "default") in self._active_keys) == active:
                return
            self.record(code, component=component, recovery=not active)

    def sample(self) -> None:
        """Cheap probes only; never call camera SDK or serial controllers."""
        from workflow.alarm_task_hooks import reconcile_task_alarms
        from workflow.task_runtime import get_task_runtime_health

        with self._submission_lock:
            dropped = self._dropped_events
        if dropped != self._reported_drops:
            self._reported_drops = dropped
            self._failed_write()
        if self.store is None:
            self._open_store()
        if self.store is not None:
            try:
                self.store.probe()
                if self._write_failure:
                    generation = self._failure_generation
                    result = self.record("ALARM_RECORDING_INTERRUPTED", component="alarm_store", fact=True,
                                         occurrence_key=self._failure_id)
                    if result:
                        with self._health_lock:
                            if generation == self._failure_generation:
                                self._write_failure = False
                                self._failure_id = None
                self.store.prune()
            except Exception:
                self._failed_write()
        self._condition("ALARM_DISK_CONFIG_INVALID", self._disk_config_invalid, component="alarm_monitor")
        reconcile_task_alarms()
        worker = get_task_runtime_health()
        self._condition("TASK_WORKER_UNAVAILABLE", not bool(worker.get("worker_alive")), component="task_runtime")
        usage = shutil.disk_usage(self.path.parent)
        free_percent = 100.0 * usage.free / usage.total
        self._condition("DISK_SPACE_CRITICAL", free_percent < self.critical_percent, component="storage")
        self._condition("DISK_SPACE_LOW", free_percent < self.warning_percent, component="storage")
        self._condition("ALARM_MONITOR_FAILED", False, component="alarm_monitor")
        with self._health_lock:
            self._worker = worker
            self._disk = {"status": "critical" if free_percent < self.critical_percent else
                         "warning" if free_percent < self.warning_percent else "healthy",
                         "free_percent": round(free_percent, 2)}
            self._sample_time = time.time()
            self._monitor_status = "healthy"

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception:
                logger.exception("alarm monitor sample failed")
                self._condition("ALARM_MONITOR_FAILED", True, component="alarm_monitor")
                with self._health_lock:
                    self._monitor_status = "degraded"
            self._stop.wait(self.interval)

    def monitoring(self) -> dict[str, Any]:
        with self._submission_lock:
            dropped = self._dropped_events
        pending_gap = dropped != self._reported_drops
        writer_failed = (self._writer is not None and not self._writer.is_alive()
                         and not self._writer_stop.is_set())
        writer_status = ("failed" if writer_failed else "stopped" if self._writer_stop.is_set()
                         else "running" if self._writer is not None else "not_started")
        with self._health_lock:
            age = max(0, time.time() - self._sample_time) if self._sample_time is not None else None
            monitor_status = self._monitor_status
            if age is not None and age > max(15, self.interval * 3) and monitor_status == "healthy":
                monitor_status = "stale"
            if self._thread and not self._thread.is_alive() and not self._stop.is_set():
                monitor_status = "failed"
            return {
                "store": {"status": "degraded" if self._write_failure or pending_gap or writer_failed else "healthy",
                          "recording_gap_possible": self._write_failure or pending_gap or writer_failed,
                          "device_writer_status": writer_status,
                          "pending_device_events": self._pending.qsize(),
                          "dropped_device_events": dropped},
                "monitor": {"status": monitor_status,
                            "sampled_at": utc_timestamp(self._sample_time) if self._sample_time else None,
                            "age_seconds": age},
                "task_worker": dict(self._worker),
                "disk": {**self._disk, "warning_percent": self.warning_percent,
                         "critical_percent": self.critical_percent,
                         "thresholds_defaulted": self._disk_config_invalid},
                "hardware": {"status": "unknown", "reason": "no_active_hardware_probe"},
            }

    def query(self, **kwargs: Any) -> dict[str, Any]:
        try:
            if self.store is None:
                raise RuntimeError("alarm store unavailable")
            response = self.store.query(**kwargs)
            response["monitoring"] = self.monitoring()
            return response
        except AlarmQueryError:
            raise
        except Exception:
            logger.exception("alarm store query failed")
            raise AlarmQueryError(503, "ALARM_STORE_UNAVAILABLE", "报警记录暂时不可读取，请稍后重试") from None


def report_alarm(code: str, **kwargs: Any) -> str | None:
    try:
        return _service.record(code, **kwargs) if _service is not None else None
    except Exception:
        logger.exception("alarm hook failed")
        return None


def recover_alarm(code: str, **kwargs: Any) -> str | None:
    return report_alarm(code, recovery=True, **kwargs)


def report_device_alarm(code: str, **kwargs: Any) -> None:
    """Nonblocking hardware hook; persistence failure must never delay stopping."""
    service = _service
    if service is not None:
        try:
            service.enqueue_device_event(code, **kwargs)
        except Exception:
            # Even hook failures must not run a logging handler on a stop path.
            with service._submission_lock:
                service._dropped_events += 1


def recover_device_alarm(code: str, **kwargs: Any) -> None:
    report_device_alarm(code, recovery=True, **kwargs)


def initialize_alarm_service() -> AlarmService | None:
    global _service
    try:
        from workflow.path_guard import PROJECT_ROOT
        _service = AlarmService(
            Path(os.getenv("COLONY_ALARM_DB", str(PROJECT_ROOT / "data" / "alarms.sqlite3"))),
            device_id=os.getenv("COLONY_DEVICE_ID") or None,
            warning_percent=os.getenv("COLONY_ALARM_DISK_WARNING_PERCENT", "20"),
            critical_percent=os.getenv("COLONY_ALARM_DISK_CRITICAL_PERCENT", "10"))
        _service.start_writer()
        from workflow.alarm_task_hooks import reset_task_alarm_reconciliation
        reset_task_alarm_reconciliation()
    except Exception:
        logger.exception("alarm service initialization failed")
        _service = None
    return _service


def get_alarm_service() -> AlarmService:
    if _service is None:
        raise AlarmQueryError(503, "ALARM_SERVICE_UNAVAILABLE", "报警服务暂时不可用")
    return _service


def shutdown_alarm_service() -> None:
    global _service
    service, _service = _service, None
    if service:
        service.stop()
        service.stop_writer()
