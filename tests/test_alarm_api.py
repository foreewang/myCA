"""Real ASGI/HTTP contract with isolated files and no hardware operations."""
import asyncio
import importlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import workflow.alarm_service as alarms


@pytest.fixture
def api(tmp_path, monkeypatch):
    # api_server's import configures retention: never touch workstation logs.
    import workflow.api_errors as errors
    with monkeypatch.context() as patch:
        patch.setattr(errors, "API_LOG_PATH", tmp_path / "api.log")
        patch.setattr(errors, "ACCESS_LOG_PATH", tmp_path / "access.log")
        patch.setattr(errors, "TASK_LOG_PATH", tmp_path / "task.log")
        server = importlib.import_module("workflow.api_server")
    monkeypatch.setattr(server, "_configure_api_file_logging", lambda: None)
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("TASK_INDEX_DIR", str(tmp_path / "task_index"))
    monkeypatch.setenv("COLONY_ALARM_DB", str(tmp_path / "alarms.db"))
    monkeypatch.setenv("COLONY_ALARM_TOKEN", "test-only-credential")
    monkeypatch.setattr("workflow.camera_process_supervisor.initialize_camera_process_supervisor", lambda: None)
    monkeypatch.setattr("workflow.camera_executor.shutdown_recording_camera", lambda: None)
    monkeypatch.setattr("workflow.task_store.DEFAULT_TASK_INDEX_DIR", tmp_path / "task_index")
    service = alarms.AlarmService(tmp_path / "direct.db")
    monkeypatch.setattr(alarms, "_service", service)
    yield server
    alarms.shutdown_alarm_service()


async def request(app, path="/api/alarms", *, token="test-only-credential", method="GET"):
    path, _, query = path.partition("?")
    messages = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        messages.append(message)
    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
               "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
               "root_path": "", "query_string": query.encode(),
               "headers": [(b"authorization", ("Bearer " + token).encode())],
               "server": ("test", 80), "client": ("127.0.0.1", 1000)}, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], json.loads(body), dict(start["headers"])


def test_single_get_route_authorization_validation_and_no_mutations(api, monkeypatch):
    matching = [r for r in api.app.routes if getattr(r, "path", None) == "/api/alarms"]
    assert len(matching) == 1 and matching[0].methods == {"GET"}
    status, _, _ = asyncio.run(request(api.app, token="incorrect"))
    assert status == 401
    monkeypatch.delenv("COLONY_ALARM_TOKEN")
    assert asyncio.run(request(api.app))[0] == 503
    monkeypatch.setenv("COLONY_ALARM_TOKEN", "test-only-credential")
    assert asyncio.run(request(api.app, "/api/alarms?category=camera"))[0] == 400
    assert asyncio.run(request(api.app, "/api/alarms?mode=invalid"))[0] == 400
    assert asyncio.run(request(api.app, "/api/alarms?limit=501"))[0] == 422
    assert asyncio.run(request(api.app, method="POST"))[0] == 405
    # Validation failures and unauthorized queries are not production alarms.
    status, page, headers = asyncio.run(request(api.app))
    assert status == 200 and page["items"] == []
    assert headers[b"cache-control"] == b"no-store"


def test_successful_query_with_p0_and_read_failure_status(api, monkeypatch):
    alarms.report_alarm("MOTION_STOP_FAILED", component="motion")
    status, page, _ = asyncio.run(request(api.app))
    assert status == 200 and page["items"][0]["severity"] == "P0"
    monkeypatch.setattr(alarms.get_alarm_service().store, "query",
                        lambda **kw: (_ for _ in ()).throw(OSError("private internal path")))
    status, body, _ = asyncio.run(request(api.app))
    assert status == 503 and body["detail"]["error_code"] == "ALARM_STORE_UNAVAILABLE"
    assert "private" not in str(body)


def test_real_http_lifespan_and_backend_poller(api, tmp_path, monkeypatch):
    import uvicorn
    from tools.poll_alarms import AlarmPoller
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api.app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    poller = None
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        # Hold the actual supervisor state lock, as a blocked device/control
        # path could do. Alarm GET must remain independent of that lock.
        from workflow import camera_process_supervisor as camera
        supervisor = camera.CameraProcessSupervisor()
        monkeypatch.setattr(camera, "_SUPERVISOR", supervisor)
        with supervisor._state_lock:
            http_request = urllib.request.Request(f"http://127.0.0.1:{port}/api/alarms",
                                                  headers={"Authorization": "Bearer test-only-credential"})
            with urllib.request.urlopen(http_request, timeout=1) as response:
                assert response.status == 200
        # Actual fault/recovery entirely between polls still arrives as history.
        alarms.report_alarm("CAMERA_SDK_ERROR", component="camera")
        alarms.recover_alarm("CAMERA_SDK_ERROR", component="camera")
        poller = AlarmPoller(f"http://127.0.0.1:{port}", tmp_path / "backend.db",
                             "test-only-credential", allow_http=True, page_size=1)
        assert poller.poll_once()
        events = [json.loads(row[0]) for row in poller.db.execute("SELECT payload FROM events")]
        assert [e["transition"] for e in events if e["code"] == "CAMERA_SDK_ERROR"] == ["occurred", "recovered"]
        assert not poller.health()["communication_alarm"]
        assert not poller.db.execute("SELECT 1 FROM alarm_state WHERE active=1").fetchone()
        # HTTP stays alive while the task worker is unavailable, with explicit
        # observation instead of interpreting /health as worker health.
        api.stop_task_runtime_manager()
        alarms.get_alarm_service().sample()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            assert response.status == 200
        assert poller.poll_once()
        assert any(json.loads(row[0])["code"] == "TASK_WORKER_UNAVAILABLE"
                   for row in poller.db.execute("SELECT payload FROM alarm_state WHERE active=1"))
    finally:
        if poller:
            poller.close()
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive()
