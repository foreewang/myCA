import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from workflow.task_logging import current_task_id, with_task_logging


@pytest.mark.parametrize("task_id", ["200", "task_id", "<task:27badc98>"])
@pytest.mark.parametrize("enabled,task_enabled", [(True, True), (False, True), (True, False)])
def test_formatter_redacts_only_named_task_fields(monkeypatch, task_id, enabled, task_enabled):
    from workflow.logging_config import SafeFormatter
    from workflow.log_sanitizer import sanitize_log_detail

    monkeypatch.setenv("COLONY_LOG_REDACT_SENSITIVE", str(int(enabled)))
    monkeypatch.setenv("COLONY_LOG_REDACT_TASK_ID", str(int(task_enabled)))
    monkeypatch.setenv("COLONY_LOG_REDACT_PATHS", "1")
    message = 'event=task_completed elapsed_ms=200.0 status=200 task_id=%s payload={"task_id": "%s"}'
    error = RuntimeError("task_id=" + task_id + " path=C:/private/secret.txt")
    record = logging.LogRecord("workflow.run_task", logging.INFO, "", 1,
                               message, (task_id, task_id), (type(error), error, None))
    record.task_id = task_id
    record.request_id = "request-" + task_id
    record.process = 200
    expected_id = sanitize_log_detail("task_id=" + task_id).partition("=")[2]

    formatter = SafeFormatter()
    text = formatter.format(record)
    assert "pid=200 request_id=request-" + task_id + " task_id=" + expected_id in text
    assert "event=task_completed elapsed_ms=200.0 status=200" in text
    assert f'payload={{"task_id": "{expected_id}"}}' in text
    assert "RuntimeError: task_id=" + expected_id in text
    assert ("secret.txt" not in text) == enabled
    assert record.task_id == task_id and record.msg == message
    assert record.args == (task_id, task_id) and record.exc_text is None
    assert formatter.format(record) == text


def test_file_routes_and_shared_rollover(tmp_path, monkeypatch):
    from workflow import api_errors

    parent = logging.getLogger("workflow")
    uvicorn = logging.getLogger("uvicorn.error")
    old_parent, old_uvicorn = parent.handlers[:], uvicorn.handlers[:]
    devices = logging.getLogger("devices")
    old_devices = devices.handlers[:]
    old_devices_level, old_uvicorn_level = devices.level, uvicorn.level
    old_access_disabled = logging.getLogger("uvicorn.access").disabled
    old_level = parent.level
    parent.handlers = []
    uvicorn.handlers = []
    devices.handlers = []
    for constant, filename in (("API_LOG_PATH", "api_server.log"),
                               ("ACCESS_LOG_PATH", "api_access.log"),
                               ("TASK_LOG_PATH", "task.log")):
        monkeypatch.setattr(api_errors, constant, tmp_path / filename)
    try:
        api_errors.configure_api_file_logging()
        api_errors.configure_api_file_logging()
        logging.getLogger("workflow.file_io").warning("api-marker")
        uvicorn.warning("server-marker")
        api_errors.access_logger.info("access-marker")

        @with_task_logging("task")
        def execute(task):
            logging.getLogger("workflow.stage_executor").warning("executor-marker")

        execute({"task_id": "task-123"})
        api = (tmp_path / "api_server.log").read_text(encoding="utf-8")
        access = (tmp_path / "api_access.log").read_text(encoding="utf-8")
        task = (tmp_path / "task.log").read_text(encoding="utf-8")
        assert api.count("api-marker") == api.count("server-marker") == 1
        assert "access-marker" not in api and "executor-marker" not in api
        assert access.count("access-marker") == 1 and "executor-marker" not in access
        assert task.count("executor-marker") == 1 and "task_id=task-123" in task
        assert "api-marker" not in task and "access-marker" not in task
        handler = uvicorn.handlers[0]
        assert handler in parent.handlers
        handler.doRollover()
        uvicorn.warning("after-server")
        logging.getLogger("workflow.file_io").warning("after-workflow")
        rotated = next(tmp_path.glob("api_server.log.????-??-??")).read_text(encoding="utf-8")
        active = (tmp_path / "api_server.log").read_text(encoding="utf-8")
        assert "api-marker" in rotated and "after-server" not in rotated
        assert active.count("after-server") == active.count("after-workflow") == 1
    finally:
        for handler in parent.handlers:
            handler.close()
        parent.handlers, uvicorn.handlers = old_parent, old_uvicorn
        devices.handlers = old_devices
        devices.setLevel(old_devices_level)
        uvicorn.setLevel(old_uvicorn_level)
        logging.getLogger("uvicorn.access").disabled = old_access_disabled
        parent.setLevel(old_level)


def test_task_context_resets_after_failure_and_between_worker_tasks():
    @with_task_logging("raw_task_cfg")
    def execute(raw_task_cfg):
        assert current_task_id.get() == raw_task_cfg["task"]["task_id"]
        raise RuntimeError("failed")

    def worker(task_id):
        with pytest.raises(RuntimeError):
            execute(raw_task_cfg={"task": {"task_id": task_id}})
        assert current_task_id.get() == "-"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, ["a", "b", "c", "d"]))
    assert current_task_id.get() == "-"
