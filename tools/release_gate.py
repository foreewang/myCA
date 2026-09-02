"""Run the complete software release suite and fail closed on skipped coverage."""
from __future__ import annotations

import compileall
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workflow.deployment_preflight import (  # noqa: E402
    parse_exact_lock,
    run_preflight,
    validate_locked_distributions,
)


TEST_LOCK = PROJECT_ROOT / "requirements-test-lock-py310.txt"
REQUIRED_REGRESSION_FILES = (
    "test_core_workflow.py",
    "test_dataset_inventory.py",
    "test_stage_reciprocation_accuracy.py",
    "test_vision_v2.py",
)
MINIMUM_RELEASE_TESTS = 200


class _NoSkipPytestPlugin:
    def __init__(self) -> None:
        self.collected = 0
        self.skipped: list[tuple[str, str]] = []

    def pytest_collection_finish(self, session) -> None:
        self.collected = len(session.items)

    def pytest_collectreport(self, report) -> None:
        if report.skipped:
            self.skipped.append((str(report.nodeid), str(report.longrepr)))

    def pytest_runtest_logreport(self, report) -> None:
        if report.skipped:
            self.skipped.append((str(report.nodeid), str(report.longrepr)))


def main() -> None:
    issues, manifest = run_preflight()
    try:
        test_expected = parse_exact_lock(TEST_LOCK)
    except (OSError, RuntimeError) as exc:
        issues.append(str(exc))
        test_versions: dict[str, str] = {}
    else:
        test_issues, test_versions = validate_locked_distributions(test_expected)
        issues.extend(f"test environment: {issue}" for issue in test_issues)
    manifest["test_lock_file"] = str(TEST_LOCK)
    manifest["test_versions"] = test_versions
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    if issues:
        for issue in issues:
            print(f"[ERROR] {issue}", file=sys.stderr)
        raise SystemExit(1)

    for relative in ("workflow", "devices", "vision", "third_party", "tools", "tests"):
        if not compileall.compile_dir(PROJECT_ROOT / relative, quiet=1):
            print(f"[ERROR] bytecode compilation failed under {relative}", file=sys.stderr)
            raise SystemExit(1)

    missing_regressions = [
        name for name in REQUIRED_REGRESSION_FILES if not (PROJECT_ROOT / "tests" / name).is_file()
    ]
    if missing_regressions:
        for name in missing_regressions:
            print(f"[ERROR] required regression suite is missing: tests/{name}", file=sys.stderr)
        raise SystemExit(1)

    # Third-party auto-loaded plugins make a release gate depend on whatever
    # happens to be installed on the build host.  The suite uses pytest core
    # only, so make collection deterministic and avoid pre-import rewrite noise.
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    try:
        import pytest
    except ImportError as exc:
        print(
            f"[ERROR] pytest is required; install {TEST_LOCK.name}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    plugin = _NoSkipPytestPlugin()
    exit_code = int(
        pytest.main(
            [
                "-q",
                str(PROJECT_ROOT / "tests"),
                "--disable-warnings",
                "--strict-markers",
            ],
            plugins=[plugin],
        )
    )
    if plugin.collected < MINIMUM_RELEASE_TESTS:
        print(
            f"[ERROR] release coverage unexpectedly shrank: "
            f"collected={plugin.collected}, required>={MINIMUM_RELEASE_TESTS}",
            file=sys.stderr,
        )
        exit_code = exit_code or 1
    if plugin.skipped:
        for test, reason in plugin.skipped:
            print(f"[ERROR] release gate skipped {test}: {reason}", file=sys.stderr)
        exit_code = exit_code or 1
    if exit_code:
        raise SystemExit(exit_code)
    print(f"Software release gate passed: tests_run={plugin.collected}, skipped=0")


if __name__ == "__main__":
    main()
