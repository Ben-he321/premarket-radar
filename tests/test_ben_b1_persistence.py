"""Temporary checkpoint IO failures; no research account or market inputs."""
import pandas_market_calendars as mcal
import pytest

from src.ben_b1 import replay


@pytest.fixture
def saved_engine(tmp_path):
    schedule = mcal.get_calendar("NYSE").schedule("2026-01-01", "2026-12-31")
    path = tmp_path / "checkpoint.json"
    engine = replay.ReplayEngine(schedule, replay.ReplayConfig(synthetic_test=True), {}, path)
    engine.save()
    engine.process({"event_id": "MOCK_CALENDAR_ONLY", "kind": "CALENDAR_DAY",
                    "at": "2026-05-01T00:00:00-04:00", "payload": {}})
    return engine, schedule, path


def test_checkpoint_transient_permission_error_retries_then_restores_new_state(saved_engine, monkeypatch):
    engine, schedule, path = saved_engine
    original = replay.os.replace
    attempts, sleeps = [], []

    def blocked_once(source, target):
        attempts.append((source, target))
        if len(attempts) == 1:
            raise PermissionError("MOCK_WINDOWS_TRANSIENT_LOCK")
        return original(source, target)

    monkeypatch.setattr(replay.os, "replace", blocked_once)
    monkeypatch.setattr(replay.time, "sleep", sleeps.append)
    engine.save()
    restored = replay.ReplayEngine.restore(path, schedule)
    assert len(attempts) == 2 and sleeps == [.05]
    assert restored.to_dict() == engine.to_dict()
    assert restored.state["events_processed"] == 1


def test_checkpoint_permanent_permission_error_is_bounded_and_preserves_old_file(saved_engine, monkeypatch):
    engine, schedule, path = saved_engine
    old_bytes = path.read_bytes()
    attempts, sleeps = [], []

    def always_blocked(source, target):
        attempts.append((source, target))
        raise PermissionError("MOCK_WINDOWS_PERSISTENT_LOCK")

    monkeypatch.setattr(replay.os, "replace", always_blocked)
    monkeypatch.setattr(replay.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError, match="MOCK_WINDOWS_PERSISTENT_LOCK"):
        engine.save()
    assert len(attempts) == 6
    assert sleeps == [.05, .10, .20, .40, .80]
    assert path.read_bytes() == old_bytes
    restored = replay.ReplayEngine.restore(path, schedule)
    assert restored.state["events_processed"] == 0
    assert engine.state["events_processed"] == 1
