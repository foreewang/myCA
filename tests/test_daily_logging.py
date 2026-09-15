from datetime import date, datetime, time, timedelta
import logging
import os

import pytest

from workflow import logging_config


@pytest.mark.parametrize("today", [date(2026, 3, 1), date(2026, 1, 1)])
def test_retention_is_30_calendar_days_even_with_sparse_logs(tmp_path, monkeypatch, today):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return today

    monkeypatch.setattr(logging_config, "date", FixedDate)
    cutoff = today - timedelta(days=29)
    keep = tmp_path / f"task.log.{cutoff.isoformat()}"
    expired = tmp_path / f"task.log.{(cutoff - timedelta(days=1)).isoformat()}"
    unrelated = tmp_path / f"other.log.{(cutoff - timedelta(days=60)).isoformat()}"
    invalid = tmp_path / "task.log.2026-02-31"
    legacy_recent = tmp_path / "task.log.1"
    legacy_expired = tmp_path / "task.log.2"
    for path in (keep, expired, unrelated, invalid, legacy_recent, legacy_expired):
        path.write_text("archived", encoding="utf-8")
    for path, day in ((legacy_recent, cutoff), (legacy_expired, cutoff - timedelta(days=1))):
        timestamp = datetime.combine(day, time()).timestamp()
        os.utime(path, (timestamp, timestamp))

    handler = logging_config.DailyLogHandler(tmp_path / "task.log")
    try:
        assert not expired.exists() and not legacy_expired.exists()
        assert all(p.exists() for p in (keep, unrelated, invalid, legacy_recent))
        assert handler.when == "MIDNIGHT" and not handler.utc
        assert handler.suffix == "%Y-%m-%d"
    finally:
        handler.close()


def test_rollover_prunes_expired_dates_and_keeps_new_record(tmp_path):
    import time as clock

    path = tmp_path / "api_server.log"
    handler = logging_config.DailyLogHandler(path)
    try:
        handler.emit(logging.makeLogRecord({"msg": "before midnight"}))
        # Add an expired archive after startup to exercise rollover cleanup.
        expired = tmp_path / f"api_server.log.{(date.today() - timedelta(days=30)).isoformat()}"
        expired.write_text("expired", encoding="utf-8")
        handler.rolloverAt = int(clock.time()) - 1
        handler.emit(logging.makeLogRecord({"msg": "after midnight"}))
        assert not expired.exists()
        archives = list(tmp_path.glob("api_server.log.????-??-??"))
        assert len(archives) == 1
        assert "before midnight" in archives[0].read_text(encoding="utf-8")
        assert "after midnight" in path.read_text(encoding="utf-8")
        assert "before midnight" not in path.read_text(encoding="utf-8")
    finally:
        handler.close()
