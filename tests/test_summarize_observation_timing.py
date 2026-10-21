from tools.summarize_observation_timing import summarize
from devices.motion.transaction_metrics import scan_communication


def test_single_multi_and_missing_measurements():
    comm = {}
    with scan_communication(comm, 0.005):
        comm["retry_count"] = 2
        comm["gap_wait_ms"] = 8.5
    scan = {"point_timings": [
        {"timings_ms": {"move_total": 1000}, "communication": comm},
        {"timings_ms": {"move_total": 500}, "communication": comm}],
        "captures": [{"motion_result": {"err_to_target": {"x": -12, "y": 8}}}]}
    single = {"task_id": "one", "status": "success", "capture_result": scan}
    result = summarize(single)
    assert result["move_total_ms"] == 1500
    assert result["nonfirst_move_mean_ms"] == 500
    assert result["retry_count"] == 4
    assert result["max_arrival_error_pulse"] == 12
    assert result["communication_complete"] is True
    multi = summarize({"wells": [{"result": single}, {"result": single}]})
    assert multi["retry_count"] == 8 and multi["point_count"] == 4
    assert multi["nonfirst_move_mean_ms"] == 500
    old = summarize({"point_timings": [{"timings_ms": {"move_total": 9}}]})
    assert old["retry_count"] is None and old["communication_complete"] is False
