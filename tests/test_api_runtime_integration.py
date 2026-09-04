"""Runtime integration checks for the production ASGI application.

Unlike the dependency-free AST contract tests, this module starts a real
Uvicorn server, executes HTTP requests over a loopback TCP socket, and lets the
FastAPI lifespan start and stop normally.  It is skipped only when the runtime
dependencies from requirements.txt are not installed.
"""
from __future__ import annotations

import importlib.util
import asyncio
import json
import multiprocessing
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DEPS_AVAILABLE = all(
    importlib.util.find_spec(module_name) is not None
    for module_name in ("fastapi", "pydantic", "uvicorn")
)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=payload,
        headers={"Content-Type": "application/json"} if payload is not None else {},
        method=method,
    )
    try:
        response = urllib.request.urlopen(request, timeout=3.0)
    except urllib.error.HTTPError as exc:
        return int(exc.code), json.loads(exc.read().decode("utf-8"))
    with response:
        return int(response.status), json.loads(response.read().decode("utf-8"))


class _ApiChainFakeConnection:
    """Inject deterministic native delays around the shared spawned fake worker."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def send(self, value: Any) -> None:
        self._connection.send(value)

    def recv(self) -> Any:
        request = self._connection.recv()
        if not isinstance(request, dict):
            return request

        request = dict(request)
        command = str(request.get("command") or "")
        event_path = os.getenv("COLONY_TEST_CAMERA_EVENT_PATH")
        if event_path:
            event = {
                "command": command,
                "pid": os.getpid(),
                "request_id": str(request.get("id") or ""),
                "timestamp": time.time(),
            }
            with Path(event_path).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")

        if command == "start_recording":
            payload = dict(request.get("payload") or {})
            settings = dict(payload.get("settings") or {})
            settings["_delay_s"] = float(
                os.getenv("COLONY_TEST_CAMERA_START_DELAY_S", "1.2")
            )
            settings["_stop_delay_s"] = float(
                os.getenv("COLONY_TEST_CAMERA_STOP_DELAY_S", "1.2")
            )
            settings["_write_part"] = True
            payload["settings"] = settings
            request["payload"] = payload
        return request

    def close(self) -> None:
        self._connection.close()


def _api_chain_fake_worker(connection: Any) -> None:
    """Spawn-safe worker target retaining the production supervisor protocol."""

    from tests.test_camera_process_supervisor import _fake_worker

    _fake_worker(_ApiChainFakeConnection(connection))


def _read_worker_commands(event_path: Path) -> list[str]:
    if not event_path.exists():
        return []
    commands: list[str] = []
    for line in event_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        commands.append(str(event.get("command") or ""))
    return commands


def _hold_camera_worker_lock(lock_path: str, ready, release) -> None:
    """Spawn-safe synthetic orphan that owns only the SDK process fence."""

    from workflow.process_guard import SingleInstanceLock

    lock = SingleInstanceLock(lock_path, owner=f"synthetic-orphan pid={os.getpid()}")
    lock.acquire()
    ready.set()
    try:
        release.wait(timeout=30.0)
    finally:
        lock.release()


@unittest.skipUnless(
    RUNTIME_DEPS_AVAILABLE,
    "install requirements.txt to run the real FastAPI/Uvicorn integration gate",
)
class ApiRuntimeIntegrationTests(unittest.TestCase):
    @staticmethod
    def _lifespan_patches(api_server, events: list[str], *, runtime_stop):
        lock = mock.Mock()
        lock.acquire.side_effect = lambda: events.append("lock.acquire")
        lock.release.side_effect = lambda: events.append("lock.release")
        return (
            lock,
            mock.patch.object(api_server, "assert_single_worker_config", return_value=None),
            mock.patch.object(
                api_server,
                "SingleInstanceLock",
                side_effect=lambda *args, **kwargs: lock,
            ),
            mock.patch.object(
                api_server,
                "recover_interrupted_task_records",
                side_effect=lambda: events.append("recover"),
            ),
            mock.patch.multiple(
                api_server,
                start_task_runtime_manager=mock.Mock(
                    side_effect=lambda: events.append("runtime.start")
                ),
                request_task_runtime_shutdown=mock.Mock(
                    side_effect=lambda: events.append("request.task")
                ),
                _request_stage_runtime_shutdown=mock.Mock(
                    side_effect=lambda: events.append("request.stage")
                ),
                _request_camera_runtime_shutdown=mock.Mock(
                    side_effect=lambda: events.append("request.camera")
                ),
            ),
            mock.patch.object(api_server, "stop_task_runtime_manager", side_effect=runtime_stop),
            mock.patch(
                "workflow.camera_process_supervisor.initialize_camera_process_supervisor",
                side_effect=lambda: events.append("camera.init"),
            ),
            mock.patch(
                "workflow.camera_executor.shutdown_recording_camera",
                side_effect=lambda: (events.append("camera.shutdown"), True)[1],
            ),
        )

    def test_lifespan_releases_camera_and_lock_when_runtime_stop_raises(self) -> None:
        import workflow.api_server as api_server

        events: list[str] = []

        def runtime_stop() -> None:
            events.append("runtime.stop")
            raise RuntimeError("injected runtime stop failure")

        patches = self._lifespan_patches(api_server, events, runtime_stop=runtime_stop)

        async def exercise() -> None:
            context = api_server._api_lifespan(api_server.app)
            await context.__aenter__()
            with self.assertRaisesRegex(RuntimeError, "injected runtime stop failure"):
                await context.__aexit__(None, None, None)

        with patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            asyncio.run(exercise())

        self.assertIn("runtime.stop", events)
        self.assertIn("camera.shutdown", events)
        self.assertIn("lock.release", events)
        for request_event in ("request.task", "request.stage", "request.camera"):
            self.assertIn(request_event, events)
            self.assertLess(events.index(request_event), events.index("runtime.stop"))
        self.assertLess(events.index("runtime.stop"), events.index("camera.shutdown"))
        self.assertLess(events.index("camera.shutdown"), events.index("lock.release"))

    def test_lifespan_publishes_all_shutdown_requests_when_one_request_fails(self) -> None:
        import workflow.api_server as api_server

        events: list[str] = []
        patches = self._lifespan_patches(
            api_server,
            events,
            runtime_stop=lambda: (events.append("runtime.stop"), True)[1],
        )

        def fail_task_request() -> None:
            events.append("request.task.failed")
            raise RuntimeError("injected task request failure")

        async def exercise() -> None:
            context = api_server._api_lifespan(api_server.app)
            await context.__aenter__()
            with self.assertRaisesRegex(RuntimeError, "injected task request failure"):
                await context.__aexit__(None, None, None)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            mock.patch.object(
                api_server,
                "request_task_runtime_shutdown",
                side_effect=fail_task_request,
            ),
        ):
            asyncio.run(exercise())

        self.assertIn("request.task.failed", events)
        self.assertIn("request.stage", events)
        self.assertIn("request.camera", events)
        self.assertIn("runtime.stop", events)
        self.assertIn("camera.shutdown", events)
        self.assertLess(events.index("request.stage"), events.index("runtime.stop"))
        self.assertLess(events.index("request.camera"), events.index("runtime.stop"))

    def test_lifespan_lock_failure_does_not_shutdown_existing_generation(self) -> None:
        import workflow.api_server as api_server
        from workflow.process_guard import SingleWorkerGuardError

        lock = mock.Mock()
        lock.acquire.side_effect = SingleWorkerGuardError(
            "API_SERVER_ALREADY_RUNNING",
            "already running",
        )
        with (
            mock.patch.object(api_server, "assert_single_worker_config", return_value=None),
            mock.patch.object(api_server, "SingleInstanceLock", return_value=lock),
            mock.patch.object(api_server, "close_hardware_admission_for_shutdown") as close_gate,
            mock.patch.object(api_server, "request_task_runtime_shutdown") as request_task,
            mock.patch.object(api_server, "_request_stage_runtime_shutdown") as request_stage,
            mock.patch.object(api_server, "_request_camera_runtime_shutdown") as request_camera,
            mock.patch.object(api_server, "stop_task_runtime_manager") as stop_runtime,
            mock.patch(
                "workflow.camera_executor.shutdown_recording_camera"
            ) as stop_camera,
        ):
            async def exercise() -> None:
                context = api_server._api_lifespan(api_server.app)
                with self.assertRaises(SingleWorkerGuardError):
                    await context.__aenter__()

            asyncio.run(exercise())

        close_gate.assert_not_called()
        request_task.assert_not_called()
        request_stage.assert_not_called()
        request_camera.assert_not_called()
        stop_runtime.assert_not_called()
        stop_camera.assert_not_called()
        lock.release.assert_not_called()

    def test_lifespan_restores_safety_latch_before_any_hardware_runtime(self) -> None:
        import workflow.api_server as api_server

        events: list[str] = []
        patches = self._lifespan_patches(
            api_server,
            events,
            runtime_stop=lambda: (events.append("runtime.stop"), True)[1],
        )

        async def exercise() -> None:
            context = api_server._api_lifespan(api_server.app)
            await context.__aenter__()
            await context.__aexit__(None, None, None)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            mock.patch.object(
                api_server,
                "restore_persisted_hardware_safety_latch",
                side_effect=lambda: events.append("safety.restore"),
            ),
            mock.patch(
                "workflow.stage_reciprocation.initialize_stage_reciprocation_controller",
                side_effect=lambda: events.append("stage.init"),
            ),
        ):
            asyncio.run(exercise())

        self.assertLess(events.index("lock.acquire"), events.index("safety.restore"))
        self.assertLess(events.index("safety.restore"), events.index("recover"))
        self.assertLess(events.index("safety.restore"), events.index("camera.init"))
        self.assertLess(events.index("safety.restore"), events.index("stage.init"))
        self.assertLess(events.index("safety.restore"), events.index("runtime.start"))

    def test_unpersisted_safety_latch_prevents_generation_quiescence(self) -> None:
        import workflow.api_server as api_server

        with (
            mock.patch.object(
                api_server,
                "task_runtime_manager_is_quiescent",
                return_value=True,
            ),
            mock.patch(
                "workflow.stage_reciprocation.stage_reciprocation_is_quiescent",
                return_value=True,
            ),
            mock.patch(
                "workflow.camera_process_supervisor.camera_process_supervisors_are_quiescent",
                return_value=True,
            ),
            mock.patch.object(
                api_server,
                "hardware_safety_shutdown_fence_ready",
                return_value=False,
            ),
        ):
            self.assertFalse(api_server._managed_runtime_is_quiescent())

    def test_lifespan_retains_generation_lock_when_managed_work_is_not_quiescent(self) -> None:
        import workflow.api_server as api_server

        events: list[str] = []
        patches = self._lifespan_patches(
            api_server,
            events,
            runtime_stop=lambda: (events.append("runtime.stop"), False)[1],
        )

        async def exercise() -> None:
            context = api_server._api_lifespan(api_server.app)
            await context.__aenter__()
            with self.assertRaisesRegex(RuntimeError, "generation lock retained"):
                await context.__aexit__(None, None, None)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            mock.patch.object(api_server, "_managed_runtime_is_quiescent", return_value=False),
            mock.patch.object(
                api_server,
                "_retain_instance_lock_until_quiescent",
                side_effect=lambda _lock: events.append("lock.retain"),
            ),
        ):
            asyncio.run(exercise())

        self.assertIn("camera.shutdown", events)
        self.assertIn("lock.retain", events)
        self.assertNotIn("lock.release", events)
        self.assertLess(events.index("camera.shutdown"), events.index("lock.retain"))

    def test_retained_generation_lock_blocks_restart_until_old_work_is_quiescent(self) -> None:
        import workflow.api_server as api_server
        from workflow.process_guard import SingleInstanceLock, SingleWorkerGuardError

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "api.lock"
            old_generation = SingleInstanceLock(path, owner="old")
            new_generation = SingleInstanceLock(path, owner="new")
            quiescent = threading.Event()
            old_generation.acquire()
            try:
                with mock.patch.object(
                    api_server,
                    "_managed_runtime_is_quiescent",
                    side_effect=quiescent.is_set,
                ):
                    api_server._retain_instance_lock_until_quiescent(old_generation)
                    with self.assertRaises(SingleWorkerGuardError):
                        new_generation.acquire()

                    quiescent.set()
                    deadline = time.monotonic() + 2.0
                    while old_generation.acquired and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertFalse(old_generation.acquired)
                    new_generation.acquire()
            finally:
                quiescent.set()
                old_generation.release()
                new_generation.release()

    def test_lifespan_stops_active_stage_motion_before_releasing_lock(self) -> None:
        import workflow.api_server as api_server
        from workflow.stage_reciprocation import stage_reciprocation_controller

        events: list[str] = []
        entered = threading.Event()
        observed_stop = threading.Event()
        cfg = {
            "targets": [{"well_name": "A1", "x": 100, "y": 200}],
            "limits": {
                "enabled": True,
                "x_min": 0,
                "x_max": 1000,
                "y_min": 0,
                "y_max": 1000,
                "safety_margin": 0,
            },
            "port": "COM3",
            "baudrate": 115200,
            "x_slave": 1,
            "y_slave": 2,
            "profile_vel": 10,
            "profile_acc": 20,
            "profile_dec": 30,
            "arrival_tolerance": 3,
            "settle_s": 0,
            "move_timeout_s": 5,
            "poll_s": 0.01,
            "max_cycles": None,
        }

        def blocking_move(**kwargs):
            entered.set()
            if kwargs["stop_event"].wait(timeout=3):
                observed_stop.set()
            return {
                "stopped_by_request": True,
                "before": {"x": {"current_pos": 0}, "y": {"current_pos": 0}},
                "after": {"x": {"current_pos": 0}, "y": {"current_pos": 0}},
            }

        def runtime_stop_after_stage_signal() -> bool:
            events.append("runtime.stop")
            if not observed_stop.wait(timeout=1.0):
                raise RuntimeError("stage did not observe phase-one stop before task join")
            return True

        patches = self._lifespan_patches(
            api_server,
            events,
            runtime_stop=runtime_stop_after_stage_signal,
        )

        async def exercise() -> None:
            context = api_server._api_lifespan(api_server.app)
            await context.__aenter__()
            stage_reciprocation_controller.start({})
            self.assertTrue(entered.wait(2.0))
            await context.__aexit__(None, None, None)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            mock.patch.object(
                api_server,
                "_request_stage_runtime_shutdown",
                side_effect=lambda: (
                    events.append("request.stage.actual"),
                    stage_reciprocation_controller.request_shutdown(),
                )[-1],
            ),
            mock.patch.object(stage_reciprocation_controller, "_normalize_cfg", return_value=cfg),
            mock.patch("workflow.stage_reciprocation.move_to_absolute", side_effect=blocking_move),
        ):
            asyncio.run(exercise())

        self.assertTrue(observed_stop.is_set())
        self.assertTrue(stage_reciprocation_controller.is_quiescent())
        self.assertIn("lock.release", events)
        self.assertLess(events.index("request.stage.actual"), events.index("runtime.stop"))
        self.assertLess(events.index("runtime.stop"), events.index("lock.release"))

    def test_lifespan_defers_cancellation_until_camera_and_lock_cleanup(self) -> None:
        import workflow.api_server as api_server

        events: list[str] = []
        runtime_entered = threading.Event()
        allow_runtime_stop = threading.Event()

        def runtime_stop() -> bool:
            events.append("runtime.stop.enter")
            runtime_entered.set()
            if not allow_runtime_stop.wait(timeout=3.0):
                raise RuntimeError("test did not release runtime stop")
            events.append("runtime.stop.exit")
            return True

        patches = self._lifespan_patches(api_server, events, runtime_stop=runtime_stop)

        async def exercise() -> None:
            context = api_server._api_lifespan(api_server.app)
            await context.__aenter__()
            exit_task = asyncio.create_task(context.__aexit__(None, None, None))
            deadline = time.monotonic() + 3.0
            while not runtime_entered.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertTrue(runtime_entered.is_set(), "runtime stop did not enter")
            exit_task.cancel()
            allow_runtime_stop.set()
            with self.assertRaises(asyncio.CancelledError):
                await exit_task

        with patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            asyncio.run(exercise())

        self.assertIn("runtime.stop.exit", events)
        self.assertIn("camera.shutdown", events)
        self.assertIn("lock.release", events)
        self.assertLess(events.index("runtime.stop.exit"), events.index("camera.shutdown"))
        self.assertLess(events.index("camera.shutdown"), events.index("lock.release"))

    def test_real_uvicorn_stays_observable_with_orphan_camera_worker_lock(self) -> None:
        import uvicorn

        import workflow.api_server as api_server
        import workflow.camera_process_supervisor as supervisor_module
        from workflow.hardware_guard import _reset_hardware_guard_for_tests
        from workflow.path_guard import PROJECT_ROOT as APP_ROOT
        from workflow.process_guard import SingleInstanceLock

        supervisor_module.reset_camera_process_supervisor_for_tests()
        _reset_hardware_guard_for_tests()
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        orphan = None
        server = None
        server_thread: threading.Thread | None = None

        try:
            with tempfile.TemporaryDirectory(prefix="orphan-camera-lock-") as temp_dir:
                lock_path = Path(temp_dir) / "camera_worker.lock"
                orphan = context.Process(
                    target=_hold_camera_worker_lock,
                    args=(str(lock_path), ready, release),
                    name="synthetic-orphan-camera-worker",
                )
                orphan.start()
                self.assertTrue(ready.wait(timeout=5.0))
                self.assertTrue(orphan.is_alive())

                port = _free_loopback_port()
                base_url = f"http://127.0.0.1:{port}"
                config = uvicorn.Config(
                    api_server.app,
                    host="127.0.0.1",
                    port=port,
                    workers=1,
                    lifespan="on",
                    log_level="warning",
                    access_log=False,
                )
                server = uvicorn.Server(config)
                server.install_signal_handlers = lambda: None
                server_thread = threading.Thread(
                    target=server.run,
                    name="api-orphan-camera-observability",
                    daemon=True,
                )

                with (
                    mock.patch.object(
                        supervisor_module,
                        "DEFAULT_CAMERA_WORKER_LOCK_PATH",
                        lock_path,
                    ),
                    mock.patch.dict(
                        os.environ,
                        {
                            "COLONY_API_WORKERS": "1",
                            "UVICORN_WORKERS": "1",
                            "WEB_CONCURRENCY": "1",
                            # This must not redirect the frozen production path.
                            "COLONY_CAMERA_WORKER_LOCK_PATH": str(
                                lock_path.with_name("bypass.lock")
                            ),
                        },
                        clear=False,
                    ),
                ):
                    server_thread.start()
                    deadline = time.monotonic() + 10.0
                    while (
                        not server.started
                        and server_thread.is_alive()
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.02)
                    self.assertTrue(server.started, "degraded Uvicorn startup did not finish")

                    status, payload = _json_request(base_url, "/health")
                    self.assertEqual((status, payload), (200, {"status": "ok"}))

                    status, payload = _json_request(
                        base_url,
                        "/api/camera/record/status",
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(payload.get("state"), "faulted")
                    self.assertEqual(payload.get("error_code"), "CAMERA_WORKER_LOCK_HELD")
                    self.assertIsNone(payload.get("worker_pid"))

                    status, hardware = _json_request(base_url, "/api/hardware/status")
                    self.assertEqual(status, 200)
                    self.assertFalse(hardware.get("safe"))
                    self.assertTrue(hardware.get("busy"))
                    self.assertEqual(
                        dict(hardware.get("safety_latch") or {}).get("error_code"),
                        "CAMERA_WORKER_LOCK_HELD",
                    )

                    status, rejected = _json_request(
                        base_url,
                        "/api/tasks/execute",
                        method="POST",
                        body={
                            "task": {
                                "task_id": "must-not-run-with-orphan-camera",
                                "task_type": "capture",
                            }
                        },
                    )
                    self.assertEqual(status, 503)
                    self.assertEqual(
                        dict(rejected.get("detail") or {}).get("error_code"),
                        "HARDWARE_SAFETY_LATCHED",
                    )

                    server.should_exit = True
                    server_thread.join(timeout=10.0)

                self.assertFalse(server_thread.is_alive())
                self.assertTrue(orphan.is_alive())
                api_probe = SingleInstanceLock(
                    APP_ROOT / "data" / "api_server.lock",
                    owner="post-degraded-api-probe",
                )
                api_probe.acquire()
                api_probe.release()
                release.set()
                orphan.join(timeout=5.0)
                self.assertFalse(orphan.is_alive())
        finally:
            if server is not None and server_thread is not None and server_thread.is_alive():
                server.should_exit = True
                server.force_exit = True
                server_thread.join(timeout=3.0)
            release.set()
            if orphan is not None:
                orphan.join(timeout=5.0)
                if orphan.is_alive():
                    orphan.terminate()
                    orphan.join(timeout=2.0)
            supervisor_module.reset_camera_process_supervisor_for_tests()
            _reset_hardware_guard_for_tests(
                clear_persisted_latch=True,
                forget_persistence_path=True,
            )

    def test_real_http_camera_chain_with_spawn_worker_and_slow_native_calls(self) -> None:
        import uvicorn

        import workflow.api_server as api_server
        import workflow.camera_process_supervisor as supervisor_module
        from workflow.camera_process_supervisor import CameraProcessSupervisor
        from workflow.hardware_guard import _reset_hardware_guard_for_tests
        from workflow.path_guard import PROJECT_ROOT as APP_ROOT
        from workflow.process_guard import SingleInstanceLock

        config_parent = APP_ROOT / "config"
        record_parent = APP_ROOT / "data" / "camera_records"
        record_parent.mkdir(parents=True, exist_ok=True)
        slow_native_delay_s = 1.2
        max_local_response_s = 0.6

        supervisor_module.reset_camera_process_supervisor_for_tests()
        _reset_hardware_guard_for_tests()
        supervisor: CameraProcessSupervisor | None = None
        server = None
        server_thread: threading.Thread | None = None

        try:
            with tempfile.TemporaryDirectory(
                prefix="api_chain_config_",
                dir=str(config_parent),
            ) as config_temp, tempfile.TemporaryDirectory(
                prefix="api_chain_record_",
                dir=str(record_parent),
            ) as record_temp:
                config_dir = Path(config_temp)
                record_dir = Path(record_temp)
                fake_sdk_dir = config_dir / "MvImport"
                fake_sdk_dir.mkdir()
                for required_file in (
                    "MvCameraControl_class.py",
                    "CameraParams_header.py",
                    "CameraParams_const.py",
                ):
                    (fake_sdk_dir / required_file).write_text("# test stub\n", encoding="utf-8")

                source_config = (config_parent / "camera.yaml").read_text(encoding="utf-8")
                rewritten_lines: list[str] = []
                sdk_path_replaced = False
                for line in source_config.splitlines():
                    if not sdk_path_replaced and line.lstrip().startswith("mvs_python_dir:"):
                        indentation = line[: len(line) - len(line.lstrip())]
                        rewritten_lines.append(
                            f'{indentation}mvs_python_dir: "{fake_sdk_dir.as_posix()}"'
                        )
                        sdk_path_replaced = True
                    else:
                        rewritten_lines.append(line)
                self.assertTrue(sdk_path_replaced, "camera fixture did not replace MVS path")

                camera_config_path = config_dir / "camera.yaml"
                camera_config_path.write_text(
                    "\n".join(rewritten_lines) + "\n",
                    encoding="utf-8",
                )
                final_path = record_dir / "integration-chain.avi"
                event_path = record_dir / "worker-events.jsonl"
                request_camera_path = camera_config_path.relative_to(APP_ROOT).as_posix()
                request_save_path = final_path.relative_to(APP_ROOT).as_posix()

                supervisor = CameraProcessSupervisor(
                    worker_target=_api_chain_fake_worker,
                    monitor_interval_s=0.05,
                )
                with supervisor_module._SUPERVISOR_LOCK:
                    supervisor_module._SUPERVISOR = supervisor
                    supervisor_module._SUPERVISOR_SHUTDOWN = False

                port = _free_loopback_port()
                base_url = f"http://127.0.0.1:{port}"
                config = uvicorn.Config(
                    api_server.app,
                    host="127.0.0.1",
                    port=port,
                    workers=1,
                    lifespan="on",
                    log_level="warning",
                    access_log=False,
                )
                server = uvicorn.Server(config)
                server.install_signal_handlers = lambda: None
                server_thread = threading.Thread(
                    target=server.run,
                    name="api-camera-chain-integration",
                    daemon=True,
                )

                worker_env = {
                    "COLONY_API_WORKERS": "1",
                    "UVICORN_WORKERS": "1",
                    "WEB_CONCURRENCY": "1",
                    "COLONY_TEST_CAMERA_EVENT_PATH": str(event_path),
                    "COLONY_TEST_CAMERA_START_DELAY_S": str(slow_native_delay_s),
                    "COLONY_TEST_CAMERA_STOP_DELAY_S": str(slow_native_delay_s),
                }

                def wait_for_worker_command(command: str, timeout_s: float = 8.0) -> None:
                    deadline = time.monotonic() + timeout_s
                    while time.monotonic() < deadline:
                        if command in _read_worker_commands(event_path):
                            return
                        time.sleep(0.01)
                    self.fail(f"spawned fake worker did not receive {command!r}")

                def wait_for_camera_state(state: str, timeout_s: float = 8.0) -> dict[str, object]:
                    deadline = time.monotonic() + timeout_s
                    last_payload: dict[str, object] = {}
                    while time.monotonic() < deadline:
                        response_status, last_payload = _json_request(
                            base_url,
                            "/api/camera/record/status",
                        )
                        self.assertEqual(response_status, 200)
                        if last_payload.get("state") == state:
                            return last_payload
                        if last_payload.get("state") == "faulted":
                            break
                        time.sleep(0.02)
                    self.fail(
                        f"camera did not reach {state!r}; last status={last_payload!r}"
                    )

                with mock.patch.dict(os.environ, worker_env, clear=False):
                    server_thread.start()
                    try:
                        deadline = time.monotonic() + 10.0
                        while (
                            not server.started
                            and server_thread.is_alive()
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.02)
                        self.assertTrue(
                            server.started,
                            "Uvicorn did not complete full-chain application startup",
                        )

                        started_at = time.monotonic()
                        response_status, accepted = _json_request(
                            base_url,
                            "/api/camera/record/start",
                            method="POST",
                            body={
                                "camera_path": request_camera_path,
                                "save_path": request_save_path,
                                "timeout_ms": 2500,
                            },
                        )
                        start_response_s = time.monotonic() - started_at
                        self.assertEqual(response_status, 202)
                        self.assertEqual(accepted.get("state"), "starting")
                        self.assertLess(start_response_s, max_local_response_s)

                        wait_for_worker_command("start_recording")
                        health_started_at = time.monotonic()
                        health_status, health_payload = _json_request(base_url, "/health")
                        health_elapsed_s = time.monotonic() - health_started_at
                        status_started_at = time.monotonic()
                        status_status, starting = _json_request(
                            base_url,
                            "/api/camera/record/status",
                        )
                        status_elapsed_s = time.monotonic() - status_started_at
                        self.assertEqual((health_status, health_payload), (200, {"status": "ok"}))
                        self.assertEqual(status_status, 200)
                        self.assertEqual(starting.get("state"), "starting")
                        self.assertLess(health_elapsed_s, max_local_response_s)
                        self.assertLess(status_elapsed_s, max_local_response_s)

                        recording = wait_for_camera_state("recording")
                        self.assertTrue(recording.get("recording"))
                        self.assertGreater(int(recording.get("worker_pid") or 0), 0)
                        self.assertEqual(recording.get("worker_restart_count"), 0)

                        stopped_at = time.monotonic()
                        response_status, accepted_stop = _json_request(
                            base_url,
                            "/api/camera/record/stop",
                            method="POST",
                        )
                        stop_response_s = time.monotonic() - stopped_at
                        self.assertEqual(response_status, 202)
                        self.assertEqual(accepted_stop.get("state"), "stopping")
                        self.assertLess(stop_response_s, max_local_response_s)

                        wait_for_worker_command("stop_recording")
                        health_started_at = time.monotonic()
                        health_status, health_payload = _json_request(base_url, "/health")
                        health_elapsed_s = time.monotonic() - health_started_at
                        status_started_at = time.monotonic()
                        status_status, stopping = _json_request(
                            base_url,
                            "/api/camera/record/status",
                        )
                        status_elapsed_s = time.monotonic() - status_started_at
                        self.assertEqual((health_status, health_payload), (200, {"status": "ok"}))
                        self.assertEqual(status_status, 200)
                        self.assertEqual(stopping.get("state"), "stopping")
                        self.assertLess(health_elapsed_s, max_local_response_s)
                        self.assertLess(status_elapsed_s, max_local_response_s)

                        idle = wait_for_camera_state("idle")
                        self.assertIsNone(idle.get("error"))
                        last_video = dict(idle.get("last_video") or {})
                        self.assertEqual(
                            Path(str(last_video.get("saved_path"))).resolve(),
                            final_path.resolve(),
                        )
                        self.assertGreater(int(last_video.get("frame_count") or 0), 0)
                        self.assertEqual(final_path.read_bytes(), b"fake-avi")
                        self.assertEqual(list(record_dir.glob("*.part.avi")), [])

                        hardware_status, hardware_payload = _json_request(
                            base_url,
                            "/api/hardware/status",
                        )
                        self.assertEqual(hardware_status, 200)
                        self.assertFalse(hardware_payload.get("busy"))
                    finally:
                        server.should_exit = True
                        server_thread.join(timeout=10.0)

                self.assertFalse(
                    server_thread.is_alive(),
                    "Uvicorn full-chain lifespan shutdown did not finish",
                )
                self.assertIsNone(supervisor.status().get("worker_pid"))
                commands = _read_worker_commands(event_path)
                self.assertEqual(commands.count("start_recording"), 1, commands)
                self.assertEqual(commands.count("stop_recording"), 1, commands)

                probe = SingleInstanceLock(
                    APP_ROOT / "data" / "api_server.lock",
                    owner="camera-chain-integration-probe",
                )
                probe.acquire()
                probe.release()
        finally:
            if server is not None and server_thread is not None and server_thread.is_alive():
                server.should_exit = True
                server.force_exit = True
                server_thread.join(timeout=3.0)
            supervisor_module.reset_camera_process_supervisor_for_tests()
            if supervisor is not None:
                supervisor.shutdown(timeout_s=1.0)
        _reset_hardware_guard_for_tests()

    def test_real_uvicorn_lifespan_routes_and_graceful_shutdown(self) -> None:
        import uvicorn

        import workflow.api_server as api_server
        from workflow.camera_record_service import CameraRecordServiceError
        from workflow.path_guard import PROJECT_ROOT as APP_ROOT
        from workflow.process_guard import SingleInstanceLock

        port = _free_loopback_port()
        base_url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(
            api_server.app,
            host="127.0.0.1",
            port=port,
            workers=1,
            lifespan="on",
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        server_thread = threading.Thread(
            target=server.run,
            name="api-runtime-integration",
            daemon=True,
        )

        worker_env = {
            "COLONY_API_WORKERS": "1",
            "UVICORN_WORKERS": "1",
            "WEB_CONCURRENCY": "1",
        }
        with mock.patch.dict(os.environ, worker_env, clear=False):
            server_thread.start()
            deadline = time.monotonic() + 10.0
            while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(server.started, "Uvicorn did not complete application startup")

            try:
                status, payload = _json_request(base_url, "/health")
                self.assertEqual(status, 200)
                self.assertEqual(payload, {"status": "ok"})

                status, payload = _json_request(base_url, "/api/camera/record/status")
                self.assertEqual(status, 200)
                self.assertEqual(payload.get("state"), "idle")
                self.assertIsNone(payload.get("worker_pid"))

                status, payload = _json_request(
                    base_url,
                    "/api/camera/record/start",
                    method="POST",
                    body={"timeout_ms": 15_001},
                )
                self.assertEqual(status, 422)
                self.assertEqual(
                    (payload.get("detail") or {}).get("error_code"),
                    "REQUEST_VALIDATION_FAILED",
                )

                def assert_unknown_field_rejected(
                    path: str,
                    body: dict[str, object],
                ) -> None:
                    response_status, response_payload = _json_request(
                        base_url,
                        path,
                        method="POST",
                        body=body,
                    )
                    self.assertEqual(response_status, 422, path)
                    self.assertEqual(
                        (response_payload.get("detail") or {}).get("error_code"),
                        "REQUEST_VALIDATION_FAILED",
                        path,
                    )

                with mock.patch.object(api_server, "start_camera_recording") as start_handler:
                    assert_unknown_field_rejected(
                        "/api/camera/record/start",
                        {
                            "save_path": "data/camera_records/integration.avi",
                            "save_paht": "typo",
                        },
                    )
                start_handler.assert_not_called()

                with mock.patch.object(api_server, "stop_camera_recording") as stop_handler:
                    assert_unknown_field_rejected(
                        "/api/camera/record/stop",
                        {"force": True},
                    )
                stop_handler.assert_not_called()

                from workflow.stage_reciprocation import stage_reciprocation_controller

                with mock.patch.object(stage_reciprocation_controller, "start") as stage_start:
                    assert_unknown_field_rejected(
                        "/api/stage/reciprocation/start",
                        {"point_a_x": 123},
                    )
                stage_start.assert_not_called()

                with mock.patch.object(stage_reciprocation_controller, "stop") as stage_stop:
                    assert_unknown_field_rejected(
                        "/api/stage/reciprocation/stop",
                        {"timeout_s": 1.0},
                    )
                stage_stop.assert_not_called()

                with mock.patch.object(api_server, "normalize_execute_task_request") as normalize:
                    assert_unknown_field_rejected(
                        "/api/tasks/execute",
                        {"task": {}, "persist_results": True},
                    )
                normalize.assert_not_called()

                with mock.patch.object(
                    api_server,
                    "start_camera_recording",
                    return_value={"state": "starting", "starting": True},
                ):
                    status, payload = _json_request(
                        base_url,
                        "/api/camera/record/start",
                        method="POST",
                        body={"save_path": "data/camera_records/integration.avi"},
                    )
                self.assertEqual(status, 202)
                self.assertEqual(payload.get("state"), "starting")

                with mock.patch.object(
                    api_server,
                    "stop_camera_recording",
                    return_value={"state": "stopping", "stopping": True},
                ) as stop_handler:
                    status, payload = _json_request(
                        base_url,
                        "/api/camera/record/stop",
                        method="POST",
                    )
                self.assertEqual(status, 202)
                self.assertEqual(payload.get("state"), "stopping")
                stop_handler.assert_called_once_with()

                service_error = CameraRecordServiceError(
                    409,
                    "CAMERA_RECORD_START_FAILED",
                    "相机录像启动失败",
                    log_detail="injected integration failure",
                )
                with mock.patch.object(
                    api_server,
                    "start_camera_recording",
                    side_effect=service_error,
                ):
                    status, payload = _json_request(
                        base_url,
                        "/api/camera/record/start",
                        method="POST",
                        body={"save_path": "data/camera_records/integration.avi"},
                    )
                self.assertEqual(status, 409)
                self.assertEqual(
                    (payload.get("detail") or {}).get("error_code"),
                    "CAMERA_RECORD_START_FAILED",
                )
            finally:
                server.should_exit = True
                server_thread.join(timeout=10.0)

        self.assertFalse(server_thread.is_alive(), "Uvicorn lifespan shutdown did not finish")

        # Prove that lifespan cleanup released the cross-process API lock.
        probe = SingleInstanceLock(APP_ROOT / "data" / "api_server.lock", owner="integration-probe")
        probe.acquire()
        probe.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)
