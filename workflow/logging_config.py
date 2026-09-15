"""Process-local logging configuration shared by API and CLI entry points."""
from __future__ import annotations

import copy
import logging
import re
import threading
from datetime import date, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from workflow.log_sanitizer import sanitize_log_detail
from workflow.path_guard import PROJECT_ROOT
from workflow.task_logging import current_request_id, current_task_id

LOG_DIR = PROJECT_ROOT / "logs"
RETENTION_DAYS = 30
_LOCK = threading.RLock()


class DailyLogHandler(TimedRotatingFileHandler):
    """Local-midnight rotation with an age limit, including days without logs."""
    def __init__(self, filename):
        super().__init__(filename, when="midnight", interval=1,
                         backupCount=RETENTION_DAYS - 1, encoding="utf-8")
        # TimedRotatingFileHandler normally only prunes on rollover. Prune at
        # startup too, so a restart after a long idle period enforces retention.
        for path in self.getFilesToDelete():
            Path(path).unlink(missing_ok=True)

    def getFilesToDelete(self):
        cutoff = date.today() - timedelta(days=RETENTION_DAYS - 1)
        base = Path(self.baseFilename)
        expired = []
        for path in base.parent.iterdir():
            if not path.name.startswith(base.name + ".") or not path.is_file() or path.is_symlink():
                continue
            suffix = path.name[len(base.name) + 1:]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", suffix):
                try:
                    archive_date = date.fromisoformat(suffix)
                except ValueError:
                    continue
            elif re.fullmatch(r"[1-9]\d*", suffix):
                # Previous size-based archives have no date in their name.
                archive_date = datetime.fromtimestamp(path.stat().st_mtime).date()
            else:
                continue
            if archive_date < cutoff:
                expired.append(str(path))
        return sorted(expired)


class SafeFormatter(logging.Formatter):
    """Format a copy so sanitization never changes another handler's record."""
    def __init__(self):
        super().__init__(
            "%(asctime)s %(levelname)s [%(name)s] pid=%(process)d "
            "request_id=%(request_id)s task_id=%(task_id)s %(message)s"
        )

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")

    def format(self, record):
        record = copy.copy(record)
        record.task_id = getattr(record, "task_id", current_task_id.get())
        record.request_id = getattr(record, "request_id", current_request_id.get())
        record.msg = record.getMessage().replace("\n", "\\n").replace("\r", "\\r")
        record.args = ()
        record.exc_text = None
        rendered = super().format(record)
        # Only middleware-supplied route templates are safe to preserve. Raw
        # request paths, query strings and user payloads never enter this field.
        http_route = getattr(record, "http_route", None) if record.name == "workflow.access" else None
        if http_route:
            rendered = rendered.replace(http_route, "<HTTP_ROUTE>")
        # Redact explicitly named task_id fields, including this formatter's
        # header. Matching the bare ID across the whole line would also change
        # unrelated values such as elapsed_ms=200.0 for task_id=200.
        rendered = sanitize_log_detail(rendered).replace("\r", "\\r")
        return rendered.replace("<HTTP_ROUTE>", http_route) if http_route else rendered


class RouteFilter(logging.Filter):
    def __init__(self, destination):
        super().__init__()
        self.destination = destination

    def filter(self, record):
        name = record.name
        if name == "uvicorn.error" and record.exc_info and getattr(record.exc_info[1], "_colony_logged", False):
            return False
        module = name.removeprefix("workflow.").split(".")[0]
        if name == "workflow.access" or name.startswith("workflow.access."):
            destination = "access"
        elif name == "workflow.camera_process_supervisor" or name.startswith("uvicorn."):
            destination = "api"
        elif getattr(record, "task_id", current_task_id.get()) != "-" or (
            name.startswith("workflow.") and
            (module.endswith("_executor") or module in {"run_task", "task_runtime"})
        ):
            destination = "task"
        else:
            destination = "api"
        return destination == self.destination


def configure_logging(*, log_dir=None, paths=None, extra_loggers=()):
    """Attach shared handlers only to independent namespace parents."""
    directory = Path(log_dir) if log_dir is not None else LOG_DIR
    paths = paths or {"api": directory / "api_server.log",
                      "access": directory / "api_access.log", "task": directory / "task.log"}
    parents = [logging.getLogger(name) for name in ("workflow", "devices", "uvicorn.error")]
    for logger in extra_loggers:
        if not any(logger.name == p.name or logger.name.startswith(p.name + ".") for p in parents):
            parents.append(logger)
    with _LOCK:
        for destination, path in paths.items():
            path = Path(path).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = next((h for h in parents[0].handlers
                            if getattr(h, "_colony_api_log_path", None) == str(path)), None)
            if handler is None:
                handler = DailyLogHandler(path)
                handler.setLevel(logging.INFO)
                handler.setFormatter(SafeFormatter())
                handler.addFilter(RouteFilter(destination))
                handler._colony_api_log_path = str(path)
            for parent in parents:
                parent.addHandler(handler)
        for parent in parents:
            parent.setLevel(logging.INFO)
        # Application middleware owns access summaries. Uvicorn's independent
        # console access logger would otherwise produce a second request line.
        logging.getLogger("uvicorn.access").disabled = True
