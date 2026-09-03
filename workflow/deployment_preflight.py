"""Fail closed when the production interpreter or locked runtime has drifted."""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping

from workflow.path_guard import PROJECT_ROOT


EXPECTED_PYTHON = (3, 10)
LOCK_FILES = {
    "cpu": PROJECT_ROOT / "requirements-lock-py310-cpu.txt",
    "gpu": PROJECT_ROOT / "requirements-lock-py310-gpu.txt",
}
RUNTIME_IMPORTS = (
    "fastapi",
    "starlette",
    "pydantic",
    "uvicorn",
    "numpy",
    "cv2",
    "PIL",
    "yaml",
    "pymodbus",
    "serial",
    "onnxruntime",
)


def _normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name).strip()).lower()


def parse_exact_lock(path: str | Path) -> dict[str, tuple[str, str]]:
    """Return normalized name -> (declared name, exact version)."""

    lock_path = Path(path)
    expected: dict[str, tuple[str, str]] = {}
    for line_number, raw_line in enumerate(lock_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line or line.count("==") != 1:
            raise RuntimeError(f"{lock_path}:{line_number} must use one exact == version: {line!r}")
        name, version = (part.strip() for part in line.split("==", 1))
        if not name or not version or any(marker in version for marker in (";", " ", "\t")):
            raise RuntimeError(f"{lock_path}:{line_number} is not an unconditional exact pin: {line!r}")
        normalized = _normalized_distribution_name(name)
        if normalized in expected:
            raise RuntimeError(f"{lock_path}:{line_number} duplicates distribution {name!r}")
        expected[normalized] = (name, version)
    if not expected:
        raise RuntimeError(f"deployment lock is empty: {lock_path}")
    return expected


def select_runtime_profile(
    version_getter: Callable[[str], str] = importlib.metadata.version,
) -> tuple[str | None, list[str]]:
    installed: list[str] = []
    for profile, distribution in (("cpu", "onnxruntime"), ("gpu", "onnxruntime-gpu")):
        try:
            version_getter(distribution)
        except importlib.metadata.PackageNotFoundError:
            continue
        installed.append(profile)
    if len(installed) == 1:
        return installed[0], []
    if not installed:
        return None, ["missing vision runtime: install exactly one locked CPU or GPU profile"]
    return None, ["onnxruntime and onnxruntime-gpu must not be installed together"]


def validate_locked_distributions(
    expected: Mapping[str, tuple[str, str]],
    version_getter: Callable[[str], str] = importlib.metadata.version,
) -> tuple[list[str], dict[str, str]]:
    issues: list[str] = []
    actual_versions: dict[str, str] = {}
    for normalized, (declared_name, expected_version) in sorted(expected.items()):
        try:
            actual_version = version_getter(declared_name)
        except importlib.metadata.PackageNotFoundError:
            issues.append(f"missing distribution: {declared_name}=={expected_version}")
            continue
        actual_versions[normalized] = actual_version
        if actual_version != expected_version:
            issues.append(
                f"version drift: {declared_name} expected {expected_version}, installed {actual_version}"
            )
    return issues, actual_versions


def _pip_check() -> list[str]:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=60.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"pip check could not complete: {type(exc).__name__}: {exc}"]
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        return [f"pip check failed: {output or f'exit code {completed.returncode}'}"]
    return []


def run_preflight() -> tuple[list[str], dict[str, object]]:
    issues: list[str] = []
    if sys.version_info[:2] != EXPECTED_PYTHON:
        issues.append(
            f"CPython {EXPECTED_PYTHON[0]}.{EXPECTED_PYTHON[1]} is required, "
            f"running {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )
    pointer_bits = struct.calcsize("P") * 8
    if pointer_bits != 64:
        issues.append(f"64-bit Python is required, running {pointer_bits}-bit")

    profile, profile_issues = select_runtime_profile()
    issues.extend(profile_issues)
    lock_path: Path | None = LOCK_FILES.get(profile or "")
    versions: dict[str, str] = {}
    if lock_path is not None:
        try:
            expected = parse_exact_lock(lock_path)
        except (OSError, RuntimeError) as exc:
            issues.append(str(exc))
        else:
            version_issues, versions = validate_locked_distributions(expected)
            issues.extend(version_issues)

    for module_name in RUNTIME_IMPORTS:
        try:
            importlib.import_module(module_name)
        except BaseException as exc:
            issues.append(f"runtime import failed: {module_name}: {type(exc).__name__}: {exc}")

    issues.extend(_pip_check())
    manifest: dict[str, object] = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "python_bits": pointer_bits,
        "profile": profile,
        "lock_file": str(lock_path) if lock_path is not None else None,
        "versions": versions,
    }
    return issues, manifest


def main() -> None:
    issues, manifest = run_preflight()
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    if issues:
        for issue in issues:
            print(f"[ERROR] {issue}", file=sys.stderr)
        raise SystemExit(1)
    print("Deployment runtime preflight passed.")


if __name__ == "__main__":
    main()
