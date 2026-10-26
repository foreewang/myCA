"""Scan configuration integration; all motion/camera devices are simulated."""
import pytest

from tests.test_stop_shoot_scan import FakeClient, hardware, move
from workflow.scan_motion_session import ScanMotionSession, optimization_options


@pytest.mark.parametrize('enabled', [False, True])
def test_stage_parameter_reaches_both_connection_paths(hardware, enabled):
    session = ScanMotionSession(optimization_options({})) if enabled else None
    result = move(session, slave_switch_gap_ms=2, release_after_move=True)
    assert FakeClient.instances[-1].slave_switch_gap_ms == 2
    assert result['motion_params']['slave_switch_gap_ms'] == 2
    assert not FakeClient.instances[-1].is_connected()


def test_reused_session_rejects_gap_change_and_cleanup_stops_only_xy(hardware):
    session = ScanMotionSession(optimization_options({}))
    move(session, slave_switch_gap_ms=2)
    client = session.client
    with pytest.raises(RuntimeError, match='gap changed'):
        move(session, slave_switch_gap_ms=5)
    session.needs_stop = True
    client.events.clear()
    session.close()
    assert client.events == [('stop', 1), ('stop', 2), 'disconnect']


def test_invalid_gap_rejected_before_stage_connection(hardware):
    with pytest.raises(ValueError, match='slave_switch_gap_ms'):
        move(None, slave_switch_gap_ms='2')
    assert FakeClient.instances == []


@pytest.mark.parametrize('enabled', [False, True])
def test_scan_records_gap_and_yields_connection_to_autofocus(tmp_path, monkeypatch, hardware, enabled):
    from workflow import scan_executor as scan, stage_executor as stage
    from devices.motion.modbus import ModbusRTUClient
    monkeypatch.setattr(scan, 'move_to_absolute', stage.move_to_absolute)
    monkeypatch.setattr(scan, '_check_motion_guard', lambda *args: None)
    monkeypatch.setattr(scan, 'open_camera', lambda **kwargs: object())
    monkeypatch.setattr(scan, 'close_camera', lambda cam: None)
    monkeypatch.setattr(scan, 'capture_with_opened_camera', lambda **kwargs: {'saved_path': 'fake.bmp'})
    def autofocus(**kwargs):
        assert not FakeClient.instances[-1].is_connected()
        assert ModbusRTUClient().slave_switch_gap_ms == 0
        return {'status': 'success'}
    monkeypatch.setattr(scan, '_execute_autofocus_before_capture', autofocus)
    params = dict(task_id='test-gap', task_type='capture', plate_type='6-well', well_name='A2',
                  objective_name='4x', settle_s=0.5, save_dir=str(tmp_path),
                  filename_pattern='{index}.bmp', device_index=0,
                  autofocus_decision={'should_run': True},
                  autofocus_cfg={'trigger': {'scope': 'once_per_task'}},
                  motion={'port': 'fake', 'slave_switch_gap_ms': 2, 'stop_shoot': {'enabled': enabled}})
    points = [dict(index=i, row_index=0, col_index=i, stage_x_target=i*100,
                   stage_y_target=i*100, view_down_mm=0, view_right_mm=i) for i in (1, 2)]
    result = scan.execute_scan_capture({'plate': {}}, params, dict(points=points, reference={}, scan_config={}))
    assert result['slave_switch_gap_ms'] == 2
    assert all(p['slave_switch_gap_ms'] == 2 for p in result['point_timings'])
    assert all(c.slave_switch_gap_ms == 2 and not c.is_connected() for c in FakeClient.instances)
