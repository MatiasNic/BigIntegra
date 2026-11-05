import json
import time
import urllib.error

import pytest

from app.core.auth import access_control as ac


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    path = tmp_path / "access_cache.json"
    monkeypatch.setattr(ac, "_CACHE_PATH", path)
    return path


def _online(monkeypatch, response):
    monkeypatch.setattr(ac, "post_access_json", lambda payload, timeout: response)


def _offline(monkeypatch):
    def _fail(payload, timeout):
        raise urllib.error.URLError("sin red")
    monkeypatch.setattr(ac, "post_access_json", _fail)


def test_offline_works_within_grace_period(cache_path, monkeypatch):
    _online(monkeypatch, {"ok": True, "diasGracia": 3})
    assert ac.check_access().kind == "online"

    _offline(monkeypatch)
    result = ac.check_access()
    assert result.ok and result.kind == "offline"
    assert "sin conexión" in result.message.lower()


def test_offline_blocked_after_grace_period(cache_path, monkeypatch):
    _online(monkeypatch, {"ok": True, "diasGracia": 2})
    ac.check_access()
    real_time = time.time
    monkeypatch.setattr(ac.time, "time", lambda: real_time() + 3 * 86400)

    _offline(monkeypatch)
    result = ac.check_access()
    assert not result.ok and result.can_retry
    assert "agotó" in result.message


def test_offline_blocked_when_grace_is_zero(cache_path, monkeypatch):
    _online(monkeypatch, {"ok": True, "diasGracia": 0})
    ac.check_access()
    _offline(monkeypatch)
    assert not ac.check_access().ok


def test_tampered_cache_is_rejected(cache_path, monkeypatch):
    _online(monkeypatch, {"ok": True, "diasGracia": 1})
    ac.check_access()
    data = json.loads(cache_path.read_text())
    data["diasGracia"] = 9999
    cache_path.write_text(json.dumps(data))

    _offline(monkeypatch)
    assert not ac.check_access().ok


def test_clock_rollback_is_rejected(cache_path, monkeypatch):
    _online(monkeypatch, {"ok": True, "diasGracia": 30})
    ac.check_access()
    real_time = time.time
    monkeypatch.setattr(ac.time, "time", lambda: real_time() - 5 * 86400)

    _offline(monkeypatch)
    result = ac.check_access()
    assert not result.ok
    assert "fecha y hora" in result.message


def test_denied_access_revokes_offline_use(cache_path, monkeypatch):
    _online(monkeypatch, {"ok": True, "diasGracia": 30})
    ac.check_access()
    _online(monkeypatch, {"ok": False, "mensaje": "Licencia vencida"})
    result = ac.check_access()
    assert not result.ok and result.kind == "denied" and not result.can_retry
    assert not cache_path.exists()

    _offline(monkeypatch)
    assert not ac.check_access().ok


def test_non_numeric_grace_days_does_not_crash(cache_path, monkeypatch):
    _online(monkeypatch, {"ok": True, "diasGracia": "7 días"})
    assert ac.check_access().ok


def test_invalid_response_is_reported_distinctly(cache_path, monkeypatch):
    def _html(payload, timeout):
        raise json.JSONDecodeError("bad", "<html>", 0)
    monkeypatch.setattr(ac, "post_access_json", _html)
    result = ac.check_access()
    assert not result.ok and result.kind == "invalid"
    assert "respondió con un error" in result.message


def test_slow_network_respects_total_deadline(cache_path, monkeypatch):
    monkeypatch.setattr(ac, "ACCESS_DEADLINE_SECONDS", 0.3)

    def _slow(payload, timeout):
        time.sleep(5)
        return {"ok": True}
    monkeypatch.setattr(ac, "post_access_json", _slow)
    started = time.monotonic()
    result = ac.check_access()
    assert time.monotonic() - started < 2
    assert result.kind == "network"


def test_recent_validation_allows_fast_start(cache_path, monkeypatch):
    assert not ac.has_recent_validation()  # sin caché: hay que esperar la planilla
    _online(monkeypatch, {"ok": True, "diasGracia": 0})
    ac.check_access()
    assert ac.has_recent_validation()


def test_recent_validation_expires_after_24h(cache_path, monkeypatch):
    _online(monkeypatch, {"ok": True, "diasGracia": 30})
    ac.check_access()
    real_time = time.time
    monkeypatch.setattr(ac.time, "time", lambda: real_time() + 25 * 3600)
    assert not ac.has_recent_validation()


def test_recent_validation_rejects_tampered_cache_and_denial(cache_path, monkeypatch):
    _online(monkeypatch, {"ok": True, "diasGracia": 30})
    ac.check_access()
    data = json.loads(cache_path.read_text())
    data["timestamp"] += 1
    cache_path.write_text(json.dumps(data))
    assert not ac.has_recent_validation()

    _online(monkeypatch, {"ok": True, "diasGracia": 30})
    ac.check_access()
    _online(monkeypatch, {"ok": False, "mensaje": "Baja"})
    ac.check_access()
    assert not ac.has_recent_validation()
