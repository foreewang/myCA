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
    "test_api_runtime_integration.py",
    "test_camera_api_contract.py",
    "test_camera_autofocus_contract.py",
    "test_camera_executor_contract.py",
    "test_camera_process_supervisor.py",
    "test_camera_record_service.py",
    "test_camera_record_soak.py",
    "test_camera_reliability.py",
    "test_config_validator_numbers.py",
    "test_config_validator_objectives.py",
    "test_core_workflow.py",
    "test_dataset_inventory.py",
    "test_deployment_preflight.py",
    "test_hardware_guard_camera.py",
    "test_hardware_safety_recovery.py",
    "test_scan_camera_lifecycle.py",
    "test_stage_executor_safety.py",
    "test_stage_reciprocation_accuracy.py",
    "test_task_runtime_shutdown.py",
    "test_vision_v2.py",
)
REQUIRED_RELEASE_TESTS = (
    "tests/test_api_runtime_integration.py::ApiRuntimeIntegrationTests::test_lifespan_retains_generation_lock_when_managed_work_is_not_quiescent",
    "tests/test_api_runtime_integration.py::ApiRuntimeIntegrationTests::test_lifespan_lock_failure_does_not_shutdown_existing_generation",
    "tests/test_api_runtime_integration.py::ApiRuntimeIntegrationTests::test_lifespan_publishes_all_shutdown_requests_when_one_request_fails",
    "tests/test_api_runtime_integration.py::ApiRuntimeIntegrationTests::test_retained_generation_lock_blocks_restart_until_old_work_is_quiescent",
    "tests/test_api_runtime_integration.py::ApiRuntimeIntegrationTests::test_lifespan_stops_active_stage_motion_before_releasing_lock",
    "tests/test_api_runtime_integration.py::ApiRuntimeIntegrationTests::test_lifespan_restores_safety_latch_before_any_hardware_runtime",
    "tests/test_api_runtime_integration.py::ApiRuntimeIntegrationTests::test_unpersisted_safety_latch_prevents_generation_quiescence",
    "tests/test_api_runtime_integration.py::ApiRuntimeIntegrationTests::test_real_http_camera_chain_with_spawn_worker_and_slow_native_calls",
    "tests/test_api_runtime_integration.py::ApiRuntimeIntegrationTests::test_real_uvicorn_stays_observable_with_orphan_camera_worker_lock",
    "tests/test_camera_executor_contract.py::CameraExecutorContractTests::test_sync_recording_cannot_replace_valid_video_with_empty_result",
    "tests/test_camera_process_supervisor.py::CameraProcessSupervisorTests::test_async_native_hang_recovers_without_api_process_restart",
    "tests/test_camera_process_supervisor.py::CameraProcessSupervisorTests::test_camera_worker_os_lock_blocks_orphan_and_second_sdk_owner",
    "tests/test_camera_process_supervisor.py::CameraProcessSupervisorTests::test_lifespan_supervisor_is_observable_and_fail_closed_with_orphan_lock",
    "tests/test_camera_process_supervisor.py::CameraProcessSupervisorTests::test_concurrent_singleton_shutdown_cannot_clear_another_teardown",
    "tests/test_camera_process_supervisor.py::CameraProcessSupervisorTests::test_shutdown_keeps_finalizing_token_until_delayed_commit_finishes",
    "tests/test_camera_process_supervisor.py::CameraProcessSupervisorTests::test_shutdown_latches_hardware_when_locked_command_worker_cannot_be_killed",
    "tests/test_camera_process_supervisor.py::CameraProcessSupervisorTests::test_unconfirmed_worker_death_blocks_quiescence_and_replacement",
    "tests/test_camera_api_contract.py::CameraApiContractTests::test_start_script_enforces_lifespan_and_bounded_graceful_shutdown",
    "tests/test_config_validator_numbers.py::ConfigValidatorNumberTests::test_autofocus_motor_requires_a_bounded_move_timeout",
    "tests/test_config_validator_numbers.py::ConfigValidatorNumberTests::test_autofocus_runtime_forwards_configured_motor_timeout",
    "tests/test_config_validator_numbers.py::ConfigValidatorNumberTests::test_autofocus_work_bounds_iteration_and_settle_time",
    "tests/test_config_validator_objectives.py::ObjectiveConfigValidationTests::test_repository_objectives_file_passes_complete_validation",
    "tests/test_deployment_preflight.py::DeploymentPreflightTests::test_release_gate_ignores_external_pytest_execution_controls",
    "tests/test_deployment_preflight.py::DeploymentPreflightTests::test_release_gate_distinguishes_collection_from_execution",
    "tests/test_hardware_guard_camera.py::HardwareGuardCameraTests::test_safety_latch_broadcasts_cancel_and_stage_stop_for_existing_owner",
    "tests/test_hardware_guard_camera.py::HardwareGuardCameraTests::test_pending_camera_owner_cannot_be_reaped_before_start_confirmation",
    "tests/test_hardware_guard_camera.py::HardwareGuardCameraTests::test_persisted_safety_latch_survives_process_state_reset",
    "tests/test_hardware_guard_camera.py::HardwareGuardCameraTests::test_invalid_persisted_latch_fails_closed",
    "tests/test_hardware_guard_camera.py::HardwareGuardCameraTests::test_persistence_failure_keeps_generation_fence_closed",
    "tests/test_hardware_guard_camera.py::HardwareGuardCameraTests::test_real_abrupt_process_exit_is_latched_by_next_generation",
    "tests/test_hardware_guard_camera.py::HardwareGuardCameraTests::test_clean_shutdown_is_the_only_path_that_removes_generation_marker",
    "tests/test_hardware_guard_camera.py::HardwareGuardCameraTests::test_restore_recreates_durable_latch_if_existing_marker_disappears",
    "tests/test_hardware_guard_camera.py::HardwareGuardCameraTests::test_pending_stage_owner_cannot_be_reaped_before_start_confirmation",
    "tests/test_hardware_guard_camera.py::HardwareGuardCameraTests::test_stage_status_sync_never_holds_hardware_admission_lock",
    "tests/test_core_workflow.py::test_task_runtime_manager_cannot_restart_while_old_worker_is_draining",
    "tests/test_core_workflow.py::test_process_guard_owner_refresh_does_not_grow_lock_file",
    "tests/test_core_workflow.py::test_task_runtime_rejects_synchronously_when_hardware_safety_is_latched",
    "tests/test_core_workflow.py::test_objective_executor_cancellation_marks_persisted_state_unknown",
    "tests/test_core_workflow.py::test_objective_executor_partial_switch_cannot_false_skip_last_confirmed_objective",
    "tests/test_core_workflow.py::test_objective_executor_stop_failure_latches_hardware_and_is_not_cancellation",
    "tests/test_core_workflow.py::test_autofocus_safe_cancel_maps_to_task_canceled_without_safety_latch[MotionCanceled]",
    "tests/test_core_workflow.py::test_autofocus_safe_cancel_maps_to_task_canceled_without_safety_latch[AutoFocusCanceled]",
    "tests/test_core_workflow.py::test_autofocus_stop_failure_latches_hardware_and_surfaces_failure",
    "tests/test_core_workflow.py::test_position_move_cancellation_requires_quick_stop_success_result[True-devices.motion.modbus]",
    "tests/test_core_workflow.py::test_position_move_cancellation_requires_quick_stop_success_result[True-third_party.XWJJJ260511.modbus]",
    "tests/test_core_workflow.py::test_position_move_cancellation_requires_quick_stop_success_result[False-devices.motion.modbus]",
    "tests/test_core_workflow.py::test_position_move_cancellation_requires_quick_stop_success_result[False-third_party.XWJJJ260511.modbus]",
    "tests/test_core_workflow.py::test_position_move_cancellation_requires_quick_stop_success_result[raises-devices.motion.modbus]",
    "tests/test_core_workflow.py::test_position_move_cancellation_requires_quick_stop_success_result[raises-third_party.XWJJJ260511.modbus]",
    "tests/test_core_workflow.py::test_position_move_ambiguous_trigger_failure_stops_and_surfaces_unknown_state[devices.motion.modbus]",
    "tests/test_core_workflow.py::test_position_move_ambiguous_trigger_failure_stops_and_surfaces_unknown_state[third_party.XWJJJ260511.modbus]",
    "tests/test_core_workflow.py::test_motor_manager_cancellation_check_failure_after_enable_attempts_stop[devices.motion.MotorManager-devices.motion.modbus]",
    "tests/test_core_workflow.py::test_motor_manager_cancellation_check_failure_after_enable_attempts_stop[third_party.XWJJJ260511.MotorManager-third_party.XWJJJ260511.modbus]",
    "tests/test_core_workflow.py::test_motion_timeouts_reject_non_finite_values_before_io[devices.motion.modbus]",
    "tests/test_core_workflow.py::test_motion_timeouts_reject_non_finite_values_before_io[third_party.XWJJJ260511.modbus]",
    "tests/test_core_workflow.py::test_scan_executor_propagates_live_cancel_into_active_stage_move",
    "tests/test_core_workflow.py::test_compensate_executor_propagates_live_cancel_into_active_stage_move",
    "tests/test_core_workflow.py::test_handoff_executor_propagates_live_cancel_into_active_stage_move",
    "tests/test_core_workflow.py::test_stage_reciprocation_shutdown_stops_and_joins_active_motion",
    "tests/test_stage_executor_safety.py::test_precommand_cancellation_does_not_open_motion_bus",
    "tests/test_stage_executor_safety.py::test_arrival_cancellation_quick_stops_both_axes",
    "tests/test_stage_executor_safety.py::test_settle_cancellation_quick_stops_both_axes_again",
    "tests/test_stage_executor_safety.py::test_interruptible_wait_uses_at_most_fifty_millisecond_slices",
    "tests/test_stage_executor_safety.py::test_quick_stop_failure_latches_hardware_and_raises_distinct_error",
    "tests/test_stage_executor_safety.py::test_ambiguous_stage_trigger_latches_unknown_motion_state",
    "tests/test_stage_executor_safety.py::test_stage_stop_does_not_relabel_existing_failure_as_stopped",
    "tests/test_stage_executor_safety.py::test_safety_latch_cancels_admitted_stage_before_thread_start",
    "tests/test_task_runtime_shutdown.py::test_shutdown_marks_never_started_queue_items_canceled",
    "tests/test_task_runtime_shutdown.py::test_shutdown_request_does_not_wait_for_submit_lock",
    "tests/test_task_runtime_shutdown.py::test_stop_timeout_includes_stalled_queue_record_cleanup",
    "tests/test_hardware_safety_recovery.py::test_success_holds_both_locks_and_writes_byte_exact_archive_and_receipt",
    "tests/test_hardware_safety_recovery.py::test_private_reset_helper_refuses_production_safety_path",
)
MINIMUM_RELEASE_TESTS = 250


class _NoSkipPytestPlugin:
    def __init__(self) -> None:
        self.collected = 0
        self.nodeids: set[str] = set()
        self.executed: set[str] = set()
        self.skipped: list[tuple[str, str]] = []

    def pytest_collection_finish(self, session) -> None:
        self.collected = len(session.items)
        self.nodeids = {str(item.nodeid).replace("\\", "/") for item in session.items}

    def pytest_collectreport(self, report) -> None:
        if report.skipped:
            self.skipped.append((str(report.nodeid), str(report.longrepr)))

    def pytest_runtest_logreport(self, report) -> None:
        if getattr(report, "when", None) == "call":
            self.executed.add(str(report.nodeid).replace("\\", "/"))
        if report.skipped:
            self.skipped.append((str(report.nodeid), str(report.longrepr)))


def _prepare_pytest_run() -> list[str]:
    """Return deterministic test arguments, ignoring host-level pytest controls."""

    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ.pop("PYTEST_ADDOPTS", None)
    os.environ.pop("PYTEST_PLUGINS", None)
    return [
        "-q",
        str(PROJECT_ROOT / "tests"),
        "--rootdir",
        str(PROJECT_ROOT),
        "-o",
        "addopts=",
        "--disable-warnings",
        "--strict-markers",
    ]


def main() -> None:
    # Several configuration-contract tests intentionally exercise the same
    # project-relative paths used by deployment commands.  Make the gate
    # independent of the caller's current working directory while rootdir
    # keeps pytest node IDs stable for the exact high-risk allowlist below.
    os.chdir(PROJECT_ROOT)
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
    pytest_args = _prepare_pytest_run()
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
        pytest.main(pytest_args, plugins=[plugin])
    )
    if plugin.collected < MINIMUM_RELEASE_TESTS:
        print(
            f"[ERROR] release coverage unexpectedly shrank: "
            f"collected={plugin.collected}, required>={MINIMUM_RELEASE_TESTS}",
            file=sys.stderr,
        )
        exit_code = exit_code or 1
    missing_required_tests = [
        nodeid
        for nodeid in REQUIRED_RELEASE_TESTS
        if nodeid not in plugin.nodeids
    ]
    if missing_required_tests:
        for nodeid in missing_required_tests:
            print(f"[ERROR] required release test was not collected: {nodeid}", file=sys.stderr)
        exit_code = exit_code or 1
    collected_files = {
        Path(nodeid.split("::", 1)[0]).name
        for nodeid in plugin.nodeids
    }
    uncollected_regressions = [
        name for name in REQUIRED_REGRESSION_FILES if name not in collected_files
    ]
    if uncollected_regressions:
        for name in uncollected_regressions:
            print(
                f"[ERROR] required regression suite collected no tests: tests/{name}",
                file=sys.stderr,
            )
        exit_code = exit_code or 1
    missing_executions = sorted(plugin.nodeids - plugin.executed)
    if missing_executions:
        preview = missing_executions[:20]
        for nodeid in preview:
            print(f"[ERROR] collected test was not executed: {nodeid}", file=sys.stderr)
        if len(missing_executions) > len(preview):
            print(
                f"[ERROR] {len(missing_executions) - len(preview)} additional collected tests were not executed",
                file=sys.stderr,
            )
        exit_code = exit_code or 1
    if plugin.skipped:
        for test, reason in plugin.skipped:
            print(f"[ERROR] release gate skipped {test}: {reason}", file=sys.stderr)
        exit_code = exit_code or 1
    if exit_code:
        raise SystemExit(exit_code)
    print(f"Software release gate passed: tests_run={len(plugin.executed)}, skipped=0")


if __name__ == "__main__":
    main()
