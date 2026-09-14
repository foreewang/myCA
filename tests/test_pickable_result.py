from copy import deepcopy
import json

import pytest

from workflow.pickable_result import build_pickable_detect_result, pickable_output_path


def source_result():
    images = []
    # G1's original representative is invalid; two other observations are valid.
    for index, pickable, gid, confidence in [(1, False, "G1", 1), (2, True, "G1", .7),
                                             (3, True, "G1", .9), (4, True, "G2", .8),
                                             (5, "true", "G3", 1)]:
        images.append({"index": index, "stage_x_actual": index * 100, "stage_y_actual": 200,
                       "mm_per_pixel": {"x": .01, "y": .02}, "schema_version": 1,
                       "clones": [{"clone_id": "C01", "source_detection_id": f"I{index}:C01",
                                   "global_clone_id": gid, "is_pickable": pickable,
                                   "confidence": confidence, "offset_from_image_center_px": [10, -5]}]})
    return {"schema_version": 2, "task_id": "test", "images": images,
            "unique_clones": [
                {"global_clone_id": "G1", "representative": {"image_index": 1, "clone_id": "C01"},
                 "source_detections": ["I1:C01", "I2:C01", "I3:C01"]},
                {"global_clone_id": "G2", "source_detections": ["I4:C01"]},
                {"global_clone_id": "G3", "source_detections": ["I5:C01"]}]}


def test_unique_pickable_representatives_and_unchanged_coordinates():
    from workflow.compensate_executor import select_clone_for_compensation, _calc_compensate_target
    source = source_result()
    before = deepcopy(source)
    result = build_pickable_detect_result(source)
    assert source == before
    assert result["unique_clone_count"] == result["total_image_clone_count"] == 2
    assert result["eligible_observation_count"] == 3
    assert result["removed_duplicate_observation_count"] == 1
    assert [im["index"] for im in result["images"]] == [3, 4]
    image, clone = select_clone_for_compensation(result, {"mode": "image_and_clone", "purpose": "pick",
                                                         "image_index": 3, "clone_id": "C01"})
    assert clone == source["images"][2]["clones"][0]
    ctx = {"plate": {"pulses_per_mm": {"x": 100, "y": 200},
                     "x_stage_sign_for_view_right": -1, "y_stage_sign_for_view_down": -1}}
    assert _calc_compensate_target(ctx=ctx, params={}, image_item=image, clone_item=clone)["compensate_target"] == {"x": 310, "y": 180}
    assert result["candidate_groups"][0]["representative"]["image_index"] == 3
    assert build_pickable_detect_result(source) == result


def test_empty_and_missing_or_inconsistent_deduplication():
    assert build_pickable_detect_result({"images": [], "unique_clones": []})["images"] == []
    source = source_result()
    for image in source["images"]:
        image["clones"][0]["is_pickable"] = False
    assert build_pickable_detect_result(source)["total_clone_count"] == 0
    source.pop("unique_clones")
    with pytest.raises(ValueError, match="required"):
        build_pickable_detect_result(source)
    source = source_result()
    source["unique_clones"][1]["source_detections"] = ["I1:C01"]
    with pytest.raises(ValueError, match="invalid"):
        build_pickable_detect_result(source)
    source = source_result()
    source["unique_clones"].pop()
    with pytest.raises(ValueError, match="cover"):
        build_pickable_detect_result(source)


def test_paths_and_multiwell(tmp_path):
    from workflow.run_task import _derive_well_ctx_params
    from workflow.path_guard import normalize_task_paths, PathGuardError
    assert pickable_output_path({"save_dir": str(tmp_path)}) == tmp_path / "pickable_detect_result.json"
    assert pickable_output_path({"detect_output_json": str(tmp_path / "detect.json")}) == tmp_path / "pickable_detect_result.json"
    with pytest.raises(ValueError, match="overwrite"):
        pickable_output_path({"detect_output_json": str(tmp_path / "same.json"),
                              "pickable_output_json": str(tmp_path / "same.json")})
    with pytest.raises(PathGuardError):
        normalize_task_paths({"detect": {"pickable_output_json": "config/no.json"}})
    for well in ["A1", "A2"]:
        _, params = _derive_well_ctx_params({"task": {}}, {"save_dir": str(tmp_path),
            "task_id": "multi", "pickable_output_json": "shared.json"}, well)
        assert pickable_output_path(params) == tmp_path / well / "pickable_detect_result.json"


def test_download_and_read_endpoint(tmp_path, monkeypatch):
    import asyncio
    from workflow import api_server, path_guard
    from workflow.task_store import build_well_artifacts_from_result
    monkeypatch.setattr(path_guard, "DATA_ROOT", tmp_path)
    path = tmp_path / "pickable_detect_result.json"
    payload = build_pickable_detect_result(source_result())
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = {"well_name": "B2", "detect_result": {"pickable_result_json": str(path)}}
    wells = build_well_artifacts_from_result(result, {})
    monkeypatch.setattr(api_server, "read_task_record", lambda _: {"task_id": "test", "wells": wells})
    url = "/api/tasks/test/wells/B2/pickable-result"
    async def request(query=b""):
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        await api_server.app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "GET", "scheme": "http", "path": url,
            "raw_path": url.encode(), "query_string": query, "headers": [],
            "client": ("127.0.0.1", 1234), "server": ("test", 80), "root_path": ""}, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return start, body
    start, body = asyncio.run(request())
    assert start["status"] == 200 and json.loads(body) == payload
    assert b"content-disposition" not in dict(start["headers"])
    start, _ = asyncio.run(request(b"download=true"))
    assert b"attachment" in dict(start["headers"])[b"content-disposition"]
    path.unlink()
    assert asyncio.run(request())[0]["status"] == 404


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("custom", [True, False])
def test_detection_integration_with_real_deduplication(tmp_path, monkeypatch, valid, custom):
    from PIL import Image
    from workflow import detect_executor
    image_path = tmp_path / "source.bmp"
    Image.new("L", (100, 100)).save(image_path)
    monkeypatch.setattr(detect_executor, "run_detect_on_image", lambda *a, **k: {
        "schema_version": 1, "clones": [{"clone_id": "C01", "bbox": [40, 40, 20, 20],
        "center_px": [50, 50], "is_pickable": valid, "confidence": .9}]})
    params = {"task_id": "test", "plate_type": "12-well", "well_name": "B2", "objective_name": "4x",
              "detect_output_json": str(tmp_path / "detect_result.json")}
    candidate_path = tmp_path / ("custom.json" if custom else "pickable_detect_result.json")
    if custom:
        params["pickable_output_json"] = str(candidate_path)
    result = detect_executor.execute_detect_on_scan_result({"task": {"detect": {"save_overlay": False}},
        "plate": {"pulses_per_mm": 100, "x_stage_sign_for_view_right": -1, "y_stage_sign_for_view_down": -1}},
        params, {"scan_config": {"fov_mm": {"width": 1, "height": 1}},
        "captures": [{"index": i, "row_index": 0, "col_index": i,
                      "stage_x_target": 100, "stage_y_target": 200,
                      "capture_result": {"saved_path": str(image_path)}} for i in (1, 2)]})
    original = json.loads((tmp_path / "detect_result.json").read_text(encoding="utf-8"))
    assert original == result and result["total_image_clone_count"] == 2
    assert result["unique_clone_count"] == 1
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert candidate["total_clone_count"] == int(valid)
    assert candidate["removed_duplicate_observation_count"] == int(valid)
    assert candidate["image_count"] == int(valid)
