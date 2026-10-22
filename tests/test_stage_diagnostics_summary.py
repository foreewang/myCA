from tools.summarize_stage_diagnostics import summarize


def test_old_results_do_not_imply_zero_retries():
    result = summarize({"point_timings": [{"timings_ms": {"move_total": 100}, "motion_timings_ms": {"settle": 40}}]})
    assert result["communication"]["retry_count"] is None
    assert result["communication_complete"] is False
    assert result["move_unattributed_ms"] == 60


def test_multiwell_excludes_first_point_per_well():
    def well(times):
        return {"result": {"capture_result": {"point_timings": [
            {"timings_ms": {"move_total": value}} for value in times
        ]}}}
    result = summarize({"wells": [well([100, 10, 20]), well([200, 30])]})
    assert result["point_count"] == 5
    assert result["move_total_ms"] == 360
    assert result["nonfirst_move_mean_ms"] == 20
