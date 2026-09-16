"""Exercise logging through application boundaries without physical hardware."""
import asyncio
import json
import logging
import multiprocessing
import threading

import pytest

from workflow.task_logging import current_request_id, current_task_id


@pytest.fixture
def log_files(tmp_path, monkeypatch):
    from workflow import api_server, api_errors, logging_config

    parents = [logging.getLogger(n) for n in ("workflow", "devices", "uvicorn.error")]
    saved = [(p, p.handlers[:], p.level) for p in parents]
    access_disabled = logging.getLogger("uvicorn.access").disabled
    for p in parents:
        p.handlers = []
    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path)
    for name, filename in (("API_LOG_PATH", "api_server.log"),
                           ("ACCESS_LOG_PATH", "api_access.log"), ("TASK_LOG_PATH", "task.log")):
        monkeypatch.setattr(api_errors, name, tmp_path / filename)
    monkeypatch.setenv("COLONY_LOG_REDACT_SENSITIVE", "0")
    api_server._configure_api_file_logging()
    try:
        yield tmp_path
    finally:
        for h in set(h for p in parents for h in p.handlers):
            h.close()
        for parent, handlers, level in saved:
            parent.handlers = handlers
            parent.setLevel(level)
        logging.getLogger("uvicorn.access").disabled = access_disabled


def read(directory, name):
    return (directory / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("redact", [False, True])
def test_task_access_ids_use_accepted_result_and_path_without_cross_request_leaks(log_files, monkeypatch, redact):
    from fastapi import FastAPI, HTTPException
    from workflow import api_server
    from workflow.api_errors import register_api_error_handlers
    from workflow.log_sanitizer import sanitize_log_detail
    from workflow.task_runtime import TaskRuntimeError

    monkeypatch.setenv("COLONY_LOG_REDACT_SENSITIVE", "1" if redact else "0")
    monkeypatch.setattr(api_server, "normalize_execute_task_request", lambda req: req)

    def submit(req, **kwargs):
        if req.task["task_id"] == "rejected-task":
            raise TaskRuntimeError(429, "TASK_QUEUE_FULL", "queue full")
        return {"task_id": "accepted-" + req.task["task_id"], "status": "accepted"}

    def read_record(task_id):
        if task_id == "missing-task":
            raise HTTPException(404, "missing task")
        return {"task_id": task_id, "status": "success"}

    monkeypatch.setattr(api_server, "submit_task_request", submit)
    monkeypatch.setattr(api_server, "read_task_record", read_record)
    monkeypatch.setattr(api_server, "build_task_result_response", lambda record, *a, **kw: record)
    monkeypatch.setattr(api_server, "cancel_task_request", lambda task_id: {"task_id": task_id})
    app = FastAPI()
    app.include_router(api_server.app.router)
    app.add_middleware(api_server.RequestLoggingMiddleware)
    register_api_error_handlers(app)
    cases = [
        ("POST", "/api/tasks/execute", {"task": {"task_id": "submitted-a"}}, "accepted-submitted-a", 202),
        ("POST", "/api/tasks/execute", {"task": {"task_id": "submitted-b"}}, "accepted-submitted-b", 202),
        ("GET", "/api/tasks/status-task/status", None, "status-task", 200),
        ("GET", "/api/tasks/result-task/result", None, "result-task", 200),
        ("POST", "/api/tasks/cancel-task/cancel", None, "cancel-task", 200),
        ("GET", "/api/tasks/missing-task/status", None, "missing-task", 404),
        ("GET", "/health", None, "-", 200),
        ("POST", "/api/tasks/execute", {"task": {"task_id": "rejected-task"}}, "-", 429),
        ("POST", "/api/tasks/execute", b"{", "-", 422),
    ]

    async def call(case):
        method, path, body, _, expected_status = case
        body = body if isinstance(body, bytes) else json.dumps(body).encode()
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json")],
                 "server": ("test", 80), "client": ("127.0.0.1", 1234)}
        messages = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == expected_status
        assert current_request_id.get() == current_task_id.get() == "-"
        return dict(start["headers"])[b"x-request-id"].decode()

    async def requests():
        return await asyncio.gather(*(call(case) for case in cases))

    ids = asyncio.run(requests())
    assert len(set(ids)) == len(cases)
    access = read(log_files, "api_access.log")
    assert access.count("event=request_completed") == len(cases)
    for case, request_id in zip(cases, ids):
        lines = [line for line in access.splitlines() if "request_id=" + request_id in line]
        assert len(lines) == 1
        assert sanitize_log_detail("task_id=" + case[3]) + " " in lines[0]
        if "/api/tasks/" in case[1] and case[1] != "/api/tasks/execute":
            assert "path=/api/tasks/{task_id}/" in lines[0]


def test_real_asgi_requests_have_one_summary_and_correlated_error(log_files, monkeypatch):
    monkeypatch.setenv("COLONY_LOG_REDACT_SENSITIVE", "1")
    import socket
    import time
    import urllib.request
    import urllib.error
    from concurrent.futures import ThreadPoolExecutor
    import uvicorn
    from fastapi import FastAPI
    from workflow.api_server import RequestLoggingMiddleware
    from workflow.api_errors import register_api_error_handlers

    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    register_api_error_handlers(app)

    @app.get("/sample/{value}")
    async def sample(value: str):
        if value == "fail":
            raise RuntimeError("unique-failure-marker")
        await asyncio.sleep(0)
        return {"value": value}

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_config=None, lifespan="off")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    def request(value):
        try:
            response = urllib.request.urlopen(f"http://127.0.0.1:{port}/sample/{value}", timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            response.read()
            return response.code, response.headers["X-Request-ID"]

    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(request, ("ok", "fail")))
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()
    assert [r[0] for r in responses] == [200, 500]
    ids = [r[1] for r in responses]
    assert ids[0] != ids[1]
    access = read(log_files, "api_access.log")
    api = read(log_files, "api_server.log")
    assert access.count("event=request_completed") == 2
    assert "path=/sample/{value}" in access and "Traceback" not in access
    assert all(access.count("request_id=" + i) == 1 for i in ids)
    assert "request_id=" + ids[1] in api
    assert api.count("event=request_failed") == 1
    assert "unique-failure-marker" in api
    assert current_request_id.get() == "-"


def test_all_main_files_rotate_automatically_with_one_shared_writer(log_files):
    parents = [logging.getLogger(n) for n in ("workflow", "devices", "uvicorn.error")]
    for handler in parents[0].handlers:
        assert all(sum(h is handler for h in parent.handlers) == 1 for parent in parents)
        assert handler.when == "MIDNIGHT"
        assert handler.backupCount == 29
    for index in range(2):
        if index == 1:
            import time
            for handler in parents[0].handlers:
                handler.rolloverAt = int(time.time()) - 1
        logging.getLogger("devices.motion.modbus").warning("api-rotation-%s", index)
        logging.getLogger("workflow.access").info("access-rotation-%s", index)
        logging.getLogger("workflow.stage_executor").warning("task-rotation-%s", index,
                                                             extra={"task_id": "rotation-task"})
    for filename, prefix in (("api_server.log", "api"), ("api_access.log", "access"), ("task.log", "task")):
        text = "".join(p.read_text(encoding="utf-8") for p in log_files.glob(filename + "*"))
        for index in range(2):
            assert text.count(f"{prefix}-rotation-{index}") == 1
        assert list(log_files.glob(filename + ".????-??-??"))


def test_request_context_lasts_through_body_failure(log_files):
    from workflow.api_server import RequestLoggingMiddleware
    sent = []
    observed = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        observed.append(current_request_id.get())
        assert read(log_files, "api_access.log") == ""
        raise RuntimeError("body failed")

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    async def exercise():
        with pytest.raises(RuntimeError, match="body failed"):
            await RequestLoggingMiddleware(app)({"type": "http", "method": "GET"}, receive, send)
        assert current_request_id.get() == "-"

    asyncio.run(exercise())
    line = read(log_files, "api_access.log")
    assert line.count("event=request_completed") == 1
    assert "status=200 outcome=failed" in line
    assert "request_id=" + observed[0] in line
    assert (b"x-request-id", observed[0].encode()) in sent[0]["headers"]


def test_runtime_threads_devices_stages_and_redaction(log_files, monkeypatch):
    from workflow import task_runtime as runtime
    from workflow.api_models import ExecuteTaskRequest
    from workflow.task_control import report_progress, TaskCanceled
    from workflow.log_sanitizer import sanitize_log_detail

    monkeypatch.setenv("COLONY_LOG_REDACT_SENSITIVE", "1")
    records = {}
    monkeypatch.setattr(runtime, "create_accepted_task_record_if_allowed",
                        lambda task, *a: records.setdefault(task["task_id"], {"task_id": task["task_id"]}))
    monkeypatch.setattr(runtime, "read_task_record", lambda task_id: records[task_id])
    monkeypatch.setattr(runtime, "update_task_record", lambda *a: None)
    monkeypatch.setattr(runtime, "write_task_record", lambda *a: None)
    monkeypatch.setattr(runtime, "finalize_success_record", lambda record, *a: record)
    monkeypatch.setattr(runtime, "finalize_failed_record", lambda record, *a: record)
    monkeypatch.setattr(runtime, "mark_record_canceled", lambda record, *a: record)
    monkeypatch.setattr(runtime, "is_task_cancel_requested", lambda *a: False)
    monkeypatch.setattr(runtime, "acquire_hardware_operation", lambda *a: None)
    monkeypatch.setattr(runtime, "release_hardware_operation", lambda *a: None)
    monitor_seen = {"private-task-" + suffix: threading.Event() for suffix in ("success", "fail", "cancel")}

    def progress(record):
        logging.getLogger("workflow.task_runtime").warning("monitor-marker")
        monitor_seen[record["task_id"]].set()
        return {}

    monkeypatch.setattr(runtime, "guess_current_progress", progress)
    seen = []

    def executor(**kwargs):
        task_id = kwargs["raw_task_cfg"]["task"]["task_id"]
        assert monitor_seen[task_id].wait(2)
        seen.append((current_task_id.get(), current_request_id.get()))
        # A real driver failure path: no serial connection is opened.
        from devices.motion.modbus import ModbusRTUClient
        assert ModbusRTUClient().read_holding_registers(1, 0) is None
        logging.getLogger(ModbusRTUClient.__module__).error("device-marker path=C:/private/secret.txt")
        report_progress({"task_id": task_id}, "capture", 10, "A1")
        report_progress({"task_id": task_id}, "capture", 20, "A1")
        if task_id.endswith("fail"):
            raise RuntimeError("task_id=" + task_id + " C:/private/secret.txt")
        if task_id.endswith("cancel"):
            raise TaskCanceled("canceled")
        return {}

    manager = runtime.TaskRuntimeManager()
    manager.start()
    try:
        for suffix in ("success", "fail", "cancel"):
            task_id = "private-task-" + suffix
            token = current_request_id.set("request-" + suffix)
            try:
                manager.submit(ExecuteTaskRequest(task={"task_id": task_id}), task_executor=executor)
            finally:
                current_request_id.reset(token)
        manager._queue.join()
    finally:
        assert manager.stop()
    task = read(log_files, "task.log")
    api = read(log_files, "api_server.log")
    assert len(seen) == 3
    assert all(t == "private-task-" + r.removeprefix("request-") for t, r in seen)
    assert "private-task-" not in task and "secret.txt" not in task
    assert task.count("event=task_queued") == task.count("event=task_started") == 3
    assert task.count("event=task_stage") == 3
    for event in ("task_completed", "task_failed", "task_canceled"):
        assert task.count("event=" + event) == 1
    assert "device-marker" not in api
    for suffix in ("success", "fail", "cancel"):
        safe = sanitize_log_detail("task_id=private-task-" + suffix)
        lines = [line for line in task.splitlines() if safe in line]
        assert any("device-marker" in line for line in lines)
        assert any("monitor-marker" in line for line in lines)
    logging.getLogger("devices.motion.modbus").error("manual-device-marker")
    assert "manual-device-marker" in read(log_files, "api_server.log")
    assert current_task_id.get() == current_request_id.get() == "-"


def test_cli_initializes_logging_and_preserves_json_output(log_files, monkeypatch, capsys):
    from workflow import run_task

    config = log_files / "task.json"
    config.write_text(json.dumps({"task": {"task_id": "cli-task", "task_type": "handoff"}}))
    monkeypatch.setattr("sys.argv", ["run_task", "--task", str(config)])
    monkeypatch.setattr(run_task, "run_handoff_task", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(run_task, "write_result", lambda *a: None)
    run_task.main()
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    task = read(log_files, "task.log")
    assert task.count("event=task_started") == task.count("event=task_completed") == 1
    assert "task_id=cli-task" in task and "event=task_stage" in task


def _worker_logging_probe(path):
    import os
    from workflow.camera_process_supervisor import _configure_camera_worker_logging
    os.environ["COLONY_CAMERA_WORKER_LOG_PATH"] = path
    os.environ["COLONY_LOG_REDACT_SENSITIVE"] = "1"
    _configure_camera_worker_logging()
    _configure_camera_worker_logging()
    token = current_task_id.set("private-camera-task")
    try:
        logging.getLogger("devices.camera_controller").error("worker-marker path=C:/private/secret.txt")
    finally:
        current_task_id.reset(token)
    handler = next(h for h in logging.getLogger().handlers
                   if getattr(h, "_colony_camera_worker_log_path", None) == path)
    handler.doRollover()
    logging.getLogger("devices.camera_controller").warning("worker-after-rollover")


def test_spawned_worker_has_independent_safe_log_and_rollover(log_files):
    path = str((log_files / "camera_worker.log").resolve())
    worker = multiprocessing.get_context("spawn").Process(target=_worker_logging_probe, args=(path,))
    worker.start()
    worker.join(15)
    if worker.is_alive():
        worker.terminate()
        worker.join(5)
        pytest.fail("worker logging probe did not exit")
    assert worker.exitcode == 0
    rotated = next(log_files.glob("camera_worker.log.????-??-??")).read_text(encoding="utf-8")
    assert rotated.count("worker-marker") == 1
    assert "private-camera-task" not in rotated and "secret.txt" not in rotated
    assert "worker-after-rollover" in read(log_files, "camera_worker.log")
    assert "worker-marker" not in read(log_files, "api_server.log")


def _worker_command_probe(connection, path):
    import os
    from workflow import camera_process_supervisor as supervisor
    os.environ["COLONY_CAMERA_WORKER_LOG_PATH"] = path
    os.environ["COLONY_LOG_REDACT_SENSITIVE"] = "1"

    def fail_open(settings):
        logging.getLogger("devices.camera_controller").error("ipc-device-marker")
        raise RuntimeError("controlled-open-failure")

    supervisor._build_controller = fail_open
    supervisor._camera_worker_main(connection)


def test_real_worker_loop_receives_and_clears_command_context(log_files):
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    worker = ctx.Process(target=_worker_command_probe,
                         args=(child, str(log_files / "camera_worker.log")))
    worker.start()
    child.close()
    try:
        assert parent.poll(10)
        assert parent.recv()["kind"] == "ready"
        for number, context in enumerate(({"task_id": "ipc-task", "trace_id": "ipc-request"}, {})):
            parent.send({"id": str(number), "command": "open", "payload": {}, **context})
            assert parent.poll(5)
            assert parent.recv()["ok"] is False
        parent.send({"id": "shutdown", "command": "shutdown", "payload": {}})
        assert parent.poll(5)
        assert parent.recv()["ok"] is True
        worker.join(5)
        assert worker.exitcode == 0
    finally:
        parent.close()
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
    lines = [line for line in read(log_files, "camera_worker.log").splitlines()
             if "ipc-device-marker" in line]
    assert len(lines) == 2
    assert "request_id=ipc-request" in lines[0] and "task_id=<task:" in lines[0]
    assert "request_id=- task_id=-" in lines[1]
