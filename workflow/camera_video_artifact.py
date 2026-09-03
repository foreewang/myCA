"""Validate and atomically publish camera video artifacts."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class CameraVideoArtifactError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = str(error_code)


def validate_and_promote_video(
    *,
    reported_path: str | Path,
    expected_part_path: str | Path,
    final_path: str | Path,
    frame_count: int,
) -> Path:
    """Fail closed before replacing a previously valid final AVI.

    The expected part path is generated per recording session.  Requiring an
    exact match prevents a stale or compromised worker response from publishing
    another session's file.
    """

    reported_text = str(reported_path or "").strip()
    expected_text = str(expected_part_path or "").strip()
    final_text = str(final_path or "").strip()
    if not reported_text or not expected_text or not final_text:
        raise CameraVideoArtifactError(
            "CAMERA_RECORD_PATH_INVALID",
            "camera recording path is empty",
        )

    part = Path(reported_text)
    expected = Path(expected_text)
    final = Path(final_text)
    if part.resolve(strict=False) != expected.resolve(strict=False):
        raise CameraVideoArtifactError(
            "CAMERA_RECORD_PATH_MISMATCH",
            f"camera worker returned an unexpected video path: {part}",
        )
    if int(frame_count) <= 0:
        raise CameraVideoArtifactError(
            "CAMERA_RECORD_EMPTY",
            "camera worker stopped without recording any frame",
        )

    try:
        part_stat = part.lstat()
    except OSError as exc:
        raise CameraVideoArtifactError(
            "CAMERA_RECORD_FILE_MISSING",
            f"camera recording part file is unavailable: {part}: {exc}",
        ) from exc
    if not stat.S_ISREG(part_stat.st_mode):
        raise CameraVideoArtifactError(
            "CAMERA_RECORD_FILE_INVALID",
            f"camera recording artifact is not a regular file: {part}",
        )
    if part_stat.st_size <= 0:
        raise CameraVideoArtifactError(
            "CAMERA_RECORD_FILE_EMPTY",
            f"camera recording part file is empty: {part}",
        )

    if part.resolve(strict=False) == final.resolve(strict=False):
        return final
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(part), str(final))
    except OSError as exc:
        raise CameraVideoArtifactError(
            "CAMERA_RECORD_PROMOTE_FAILED",
            f"camera recording could not be promoted to {final}: {exc}",
        ) from exc
    return final
