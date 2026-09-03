"""Run and archive the real HTTP camera-recording acceptance loop."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SoakFailure(RuntimeError):
    pass


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
            data = json.loads(raw.decode("utf-8")) if raw else {}
            return int(response.status), dict(data)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        detail = raw.decode("utf-8", errors="replace") if raw else str(exc)
        raise SoakFailure(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SoakFailure(f"{method} {path} failed: {exc}") from exc


def _camera_status(base_url: str, *, request_timeout_s: float) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    code, status = _request(
        base_url,
        "GET",
        "/api/camera/record/status",
        timeout_s=request_timeout_s,
    )
    elapsed = time.monotonic() - started
    if code != 200:
        raise SoakFailure(f"camera status returned unexpected HTTP {code}")
    return status, elapsed


def _wait_for_state(
    base_url: str,
    target: str,
    *,
    timeout_s: float,
    poll_s: float,
    request_timeout_s: float,
    status_max_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status, elapsed = _camera_status(base_url, request_timeout_s=request_timeout_s)
        if elapsed > status_max_s:
            raise SoakFailure(
                f"status latency {elapsed:.3f}s exceeded {status_max_s:.3f}s in state={status.get('state')}"
            )
        state = str(status.get("state") or "")
        if state == target:
            return status
        if state == "faulted" or (state == "idle" and target != "idle"):
            raise SoakFailure(
                f"camera reached {state} before {target}: "
                f"error_code={status.get('error_code')} error={status.get('error')}"
            )
        time.sleep(poll_s)
    raise SoakFailure(f"camera did not reach state={target} within {timeout_s:.1f}s")


def _build_payload(args: argparse.Namespace, cycle: int) -> dict[str, Any]:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    save_path = f"{args.output_dir.rstrip('/')}/soak_{stamp}_{cycle:03d}.avi"
    payload: dict[str, Any] = {
        "save_path": save_path,
        "camera_path": args.camera_path,
        "fps": args.fps,
        "bitrate_kbps": args.bitrate_kbps,
    }
    for key in (
        "serial_number",
        "ip",
        "mvs_python_dir",
        "pixel_format",
        "exposure_us",
        "gain",
        "timeout_ms",
    ):
        value = getattr(args, key)
        if value is not None:
            payload[key] = value
    return payload


def _server_artifact_path(value: Any, server_root: str | Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise SoakFailure("completed video did not provide saved_path")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(server_root) / path
    return path.resolve(strict=False)


def _verify_video_artifact(
    video: dict[str, Any],
    *,
    expected_save_path: str,
    server_root: str | Path,
) -> dict[str, Any]:
    """Decode representative frames and cross-check container/API metadata."""

    try:
        import cv2
    except ImportError as exc:
        raise SoakFailure(f"OpenCV runtime is unavailable for AVI verification: {exc}") from exc

    artifact = _server_artifact_path(video.get("saved_path"), server_root)
    expected = _server_artifact_path(expected_save_path, server_root)
    if artifact != expected:
        raise SoakFailure(
            f"completed video path mismatch: expected={expected}, returned={artifact}"
        )
    try:
        file_size = artifact.stat().st_size
    except OSError as exc:
        raise SoakFailure(f"completed AVI is unavailable: {artifact}: {exc}") from exc
    if file_size <= 0:
        raise SoakFailure(f"completed AVI is empty: {artifact}")

    api_frame_count = int(video.get("frame_count") or 0)
    api_fps = float(video.get("frame_rate") or 0.0)
    api_duration = float(video.get("duration_s") or 0.0)
    for name, value in (
        ("frame_count", float(api_frame_count)),
        ("frame_rate", api_fps),
        ("duration_s", api_duration),
    ):
        if not math.isfinite(value) or value <= 0:
            raise SoakFailure(f"completed video API metadata {name} is invalid: {value!r}")

    capture = cv2.VideoCapture(str(artifact))
    try:
        if not capture.isOpened():
            raise SoakFailure(f"OpenCV could not open completed AVI: {artifact}")
        container_frame_count = int(round(float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)))
        container_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        width = int(round(float(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0.0)))
        height = int(round(float(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0.0)))
        if container_frame_count <= 0:
            raise SoakFailure(f"AVI container reports no frames: {artifact}")
        if not math.isfinite(container_fps) or container_fps <= 0:
            raise SoakFailure(f"AVI container reports invalid FPS={container_fps!r}: {artifact}")
        if width <= 0 or height <= 0:
            raise SoakFailure(f"AVI container reports invalid dimensions {width}x{height}: {artifact}")

        sample_indices = sorted({0, container_frame_count // 2, container_frame_count - 1})
        decoded_indices: list[int] = []
        for frame_index in sample_indices:
            if not capture.set(cv2.CAP_PROP_POS_FRAMES, float(frame_index)):
                raise SoakFailure(f"AVI seek failed at frame {frame_index}: {artifact}")
            ok, frame = capture.read()
            if not ok or frame is None or getattr(frame, "size", 0) <= 0:
                raise SoakFailure(f"AVI decode failed at frame {frame_index}: {artifact}")
            if int(frame.shape[1]) != width or int(frame.shape[0]) != height:
                raise SoakFailure(
                    f"AVI decoded frame {frame_index} dimensions changed: "
                    f"expected={width}x{height}, actual={frame.shape[1]}x{frame.shape[0]}"
                )
            decoded_indices.append(frame_index)
    finally:
        capture.release()

    frame_tolerance = max(2, int(math.ceil(max(api_frame_count, container_frame_count) * 0.05)))
    if abs(api_frame_count - container_frame_count) > frame_tolerance:
        raise SoakFailure(
            f"AVI/API frame count mismatch: api={api_frame_count}, "
            f"container={container_frame_count}, tolerance={frame_tolerance}"
        )
    fps_tolerance = max(1.0, max(api_fps, container_fps) * 0.10)
    if abs(api_fps - container_fps) > fps_tolerance:
        raise SoakFailure(
            f"AVI/API FPS mismatch: api={api_fps:.3f}, container={container_fps:.3f}, "
            f"tolerance={fps_tolerance:.3f}"
        )
    container_duration = container_frame_count / container_fps
    duration_tolerance = max(2.0, max(api_duration, container_duration) * 0.20)
    if abs(api_duration - container_duration) > duration_tolerance:
        raise SoakFailure(
            f"AVI/API duration mismatch: api={api_duration:.3f}s, "
            f"container={container_duration:.3f}s, tolerance={duration_tolerance:.3f}s"
        )

    api_width = int(video.get("width") or 0)
    api_height = int(video.get("height") or 0)
    if api_width > 0 and api_width != width:
        raise SoakFailure(f"AVI/API width mismatch: api={api_width}, container={width}")
    if api_height > 0 and api_height != height:
        raise SoakFailure(f"AVI/API height mismatch: api={api_height}, container={height}")

    return {
        "path": str(artifact),
        "file_size": file_size,
        "container_frame_count": container_frame_count,
        "container_fps": container_fps,
        "container_duration_s": container_duration,
        "width": width,
        "height": height,
        "decoded_frame_indices": decoded_indices,
    }


def _find_part_files(output_dir: str, server_root: str | Path) -> list[str]:
    root = _server_artifact_path(output_dir, server_root)
    if not root.exists():
        return []
    return sorted(str(path.resolve(strict=False)) for path in root.rglob("*.part.avi"))


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _best_effort_stop(args: argparse.Namespace, cycle: int) -> None:
    """Stop a recording even when start polling or the soak body failed early."""

    deadline = time.monotonic() + args.transition_timeout_s
    while time.monotonic() < deadline:
        current, _ = _camera_status(
            args.base_url, request_timeout_s=args.request_timeout_s
        )
        state = str(current.get("state") or "")
        if state == "recording":
            stop_code, stopped = _request(
                args.base_url,
                "POST",
                "/api/camera/record/stop",
                timeout_s=args.request_timeout_s,
            )
            if stop_code != 202 or stopped.get("state") != "stopping":
                raise SoakFailure(
                    f"cycle {cycle}: invalid stop acceptance: HTTP {stop_code} {stopped}"
                )
            return
        if state in {"idle", "faulted", "stopping"}:
            return
        if state != "starting":
            raise SoakFailure(f"cycle {cycle}: cannot clean up unexpected camera state={state!r}")
        time.sleep(args.poll_s)
    raise SoakFailure(f"cycle {cycle}: start transition did not settle before cleanup timeout")


def _run_cycles(args: argparse.Namespace, report: dict[str, Any]) -> None:
    code, health = _request(args.base_url, "GET", "/health", timeout_s=args.request_timeout_s)
    if code != 200 or health.get("status") != "ok":
        raise SoakFailure(f"health check failed: HTTP {code} {health}")

    initial, initial_latency = _camera_status(
        args.base_url, request_timeout_s=args.request_timeout_s
    )
    if initial_latency > args.status_max_s:
        raise SoakFailure(f"initial status latency was {initial_latency:.3f}s")
    if initial.get("state") != "idle":
        raise SoakFailure(f"camera must be idle before soak, got {initial.get('state')}")
    initial_restarts = int(initial.get("worker_restart_count") or 0)
    report["initial_status"] = initial
    report["initial_status_latency_s"] = initial_latency

    for cycle in range(1, args.cycles + 1):
        payload = _build_payload(args, cycle)
        try:
            start_code, accepted = _request(
                args.base_url,
                "POST",
                "/api/camera/record/start",
                payload=payload,
                timeout_s=args.request_timeout_s,
            )
            if start_code != 202 or accepted.get("state") not in {"starting", "recording"}:
                raise SoakFailure(
                    f"cycle {cycle}: invalid start acceptance: HTTP {start_code} {accepted}"
                )

            recording = _wait_for_state(
                args.base_url,
                "recording",
                timeout_s=args.transition_timeout_s,
                poll_s=args.poll_s,
                request_timeout_s=args.request_timeout_s,
                status_max_s=args.status_max_s,
            )
            pid = recording.get("worker_pid")
            operation_id = recording.get("operation_id")

            record_deadline = time.monotonic() + args.record_seconds
            while time.monotonic() < record_deadline:
                status, elapsed = _camera_status(
                    args.base_url, request_timeout_s=args.request_timeout_s
                )
                if elapsed > args.status_max_s:
                    raise SoakFailure(
                        f"cycle {cycle}: status latency {elapsed:.3f}s exceeded limit during recording"
                    )
                if status.get("state") != "recording" or status.get("error"):
                    raise SoakFailure(f"cycle {cycle}: recording became unhealthy: {status}")
                time.sleep(args.poll_s)
        finally:
            try:
                _best_effort_stop(args, cycle)
            except (SoakFailure, KeyboardInterrupt) as cleanup_exc:
                # Preserve the primary failure/interrupt.  On a healthy body,
                # the following idle wait will still turn cleanup failure into
                # a hard acceptance failure.
                print(f"cleanup warning: {cleanup_exc}", file=sys.stderr, flush=True)

        completed = _wait_for_state(
            args.base_url,
            "idle",
            timeout_s=args.transition_timeout_s,
            poll_s=args.poll_s,
            request_timeout_s=args.request_timeout_s,
            status_max_s=args.status_max_s,
        )
        if completed.get("error"):
            raise SoakFailure(f"cycle {cycle}: stop failed: {completed}")
        video = dict(completed.get("last_video") or {})
        artifact = _verify_video_artifact(
            video,
            expected_save_path=str(payload["save_path"]),
            server_root=args.server_root,
        )
        part_files = _find_part_files(args.output_dir, args.server_root)
        if part_files:
            raise SoakFailure(
                f"cycle {cycle}: incomplete .part.avi files remain under output directory: {part_files}"
            )
        report["cycles"].append(
            {
                "cycle": cycle,
                "operation_id": operation_id,
                "worker_pid": pid,
                "requested": payload,
                "video": video,
                "artifact": artifact,
                "completed_status": completed,
            }
        )
        print(
            f"cycle={cycle}/{args.cycles} operation_id={operation_id} pid={pid} "
            f"frames={video.get('frame_count')} duration_s={video.get('duration_s')} "
            f"path={video.get('saved_path')}",
            flush=True,
        )

    final, _ = _camera_status(args.base_url, request_timeout_s=args.request_timeout_s)
    final_restarts = int(final.get("worker_restart_count") or 0)
    if not args.allow_restarts and final_restarts != initial_restarts:
        raise SoakFailure(
            f"worker restarted during normal soak: before={initial_restarts}, after={final_restarts}"
        )
    report["final_status"] = final
    report["worker_restart_count"] = final_restarts
    print(
        f"PASS cycles={args.cycles} worker_restart_count={final_restarts}",
        flush=True,
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    if args.report_path:
        report_path = Path(args.report_path)
        if not report_path.is_absolute():
            report_path = PROJECT_ROOT / report_path
    else:
        report_path = PROJECT_ROOT / "logs" / (
            f"camera_record_soak_{started_at.strftime('%Y%m%dT%H%M%SZ')}.json"
        )
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "base_url": args.base_url,
        "server_root": str(Path(args.server_root).resolve(strict=False)),
        "requested_cycles": args.cycles,
        "record_seconds": args.record_seconds,
        "cycles": [],
        "error": None,
        "report_path": str(report_path.resolve(strict=False)),
    }
    try:
        _run_cycles(args, report)
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    else:
        report["status"] = "passed"
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_report(report_path, report)
        print(f"report={report_path}", flush=True)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Camera recording HTTP soak/acceptance test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--record-seconds", type=float, default=10.0)
    parser.add_argument("--transition-timeout-s", type=float, default=120.0)
    parser.add_argument("--request-timeout-s", type=float, default=10.0)
    parser.add_argument("--status-max-s", type=float, default=2.0)
    parser.add_argument("--poll-s", type=float, default=0.5)
    parser.add_argument("--output-dir", default="data/camera_records/soak")
    parser.add_argument(
        "--server-root",
        default=str(PROJECT_ROOT),
        help="Local filesystem root used by the API for relative artifact paths",
    )
    parser.add_argument(
        "--report-path",
        help="Machine-readable JSON result path (default: logs/camera_record_soak_<UTC>.json)",
    )
    parser.add_argument("--camera-path", default="config/camera.yaml")
    parser.add_argument("--serial-number")
    parser.add_argument("--ip")
    parser.add_argument("--mvs-python-dir")
    parser.add_argument("--pixel-format")
    parser.add_argument("--exposure-us", type=float)
    parser.add_argument("--gain", type=float)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--bitrate-kbps", type=int, default=1000)
    parser.add_argument("--timeout-ms", type=int)
    parser.add_argument("--allow-restarts", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if any(
        value <= 0
        for value in (
            args.cycles,
            args.record_seconds,
            args.transition_timeout_s,
            args.request_timeout_s,
            args.status_max_s,
            args.poll_s,
        )
    ):
        print("cycle and timeout arguments must be positive", file=sys.stderr)
        return 2
    try:
        run(args)
    except (SoakFailure, KeyboardInterrupt) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
