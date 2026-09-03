"""Artifact-level tests for the onsite camera recording acceptance tool."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from tools.camera_record_soak import (
    SoakFailure,
    _find_part_files,
    _verify_video_artifact,
    _write_report,
)


class CameraRecordSoakArtifactTests(unittest.TestCase):
    @staticmethod
    def _write_test_avi(path: Path, *, frames: int = 12, fps: float = 10.0) -> None:
        writer = None
        for codec in ("MJPG", "XVID"):
            candidate = cv2.VideoWriter(
                str(path),
                cv2.VideoWriter_fourcc(*codec),
                fps,
                (32, 24),
            )
            if candidate.isOpened():
                writer = candidate
                break
            candidate.release()
        if writer is None:
            raise AssertionError("candidate OpenCV runtime cannot encode a small AVI")
        try:
            for index in range(frames):
                frame = np.full((24, 32, 3), index * 10, dtype=np.uint8)
                writer.write(frame)
        finally:
            writer.release()

    def test_verifier_decodes_first_middle_last_and_cross_checks_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = root / "record.avi"
            self._write_test_avi(artifact)

            verified = _verify_video_artifact(
                {
                    "saved_path": str(artifact),
                    "frame_count": 12,
                    "frame_rate": 10.0,
                    "duration_s": 1.2,
                    "width": 32,
                    "height": 24,
                },
                expected_save_path=str(artifact),
                server_root=root,
            )

            self.assertGreater(verified["file_size"], 0)
            self.assertEqual(verified["container_frame_count"], 12)
            self.assertEqual(verified["decoded_frame_indices"], [0, 6, 11])
            self.assertEqual((verified["width"], verified["height"]), (32, 24))

    def test_verifier_rejects_path_and_frame_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact = root / "record.avi"
            self._write_test_avi(artifact)
            valid = {
                "saved_path": str(artifact),
                "frame_count": 12,
                "frame_rate": 10.0,
                "duration_s": 1.2,
                "width": 32,
                "height": 24,
            }

            with self.assertRaisesRegex(SoakFailure, "path mismatch"):
                _verify_video_artifact(
                    valid,
                    expected_save_path=str(root / "other.avi"),
                    server_root=root,
                )
            with self.assertRaisesRegex(SoakFailure, "frame count mismatch"):
                _verify_video_artifact(
                    {**valid, "frame_count": 100},
                    expected_save_path=str(artifact),
                    server_root=root,
                )

    def test_part_scan_and_atomic_report_are_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "records"
            output.mkdir()
            part = output / "record.deadbeef.part.avi"
            part.write_bytes(b"incomplete")
            self.assertEqual(_find_part_files(str(output), root), [str(part.resolve())])

            report_path = root / "reports" / "soak.json"
            _write_report(report_path, {"status": "passed", "cycles": [1]})
            self.assertEqual(
                json.loads(report_path.read_text(encoding="utf-8")),
                {"status": "passed", "cycles": [1]},
            )
            self.assertEqual(list(report_path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
