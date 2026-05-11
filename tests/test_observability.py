"""tests/test_observability.py — Observability layer: JSON logging, brain log rotation,
/metrics, and /healthz."""

import json
import logging
import os
import sqlite3
import time

import pytest


# ── Fixture: restore root logger after obs.setup_logging calls ───────────────

@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Undo any setup_logging side-effects so other tests are unaffected."""
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    yield
    root.handlers = old_handlers
    root.level = old_level


# ── 1. obs.py ─────────────────────────────────────────────────────────────────

def test_json_formatter_valid_json():
    """_JsonFormatter produces one valid JSON object per record."""
    from obs import _JsonFormatter
    formatter = _JsonFormatter("test_svc")
    record = logging.LogRecord(
        name="test_logger", level=logging.INFO, pathname="",
        lineno=0, msg="hello %s", args=("world",), exc_info=None,
    )
    line = formatter.format(record)
    obj = json.loads(line)
    assert obj["msg"] == "hello world"
    assert obj["service"] == "test_svc"
    assert obj["level"] == "INFO"
    assert obj["logger"] == "test_logger"
    assert obj["ts"].endswith("Z")


def test_json_formatter_flattens_extra_kwargs():
    """Extra kwargs on the LogRecord are flattened into the JSON object."""
    from obs import _JsonFormatter
    formatter = _JsonFormatter("svc")
    record = logging.LogRecord(
        name="n", level=logging.WARNING, pathname="",
        lineno=0, msg="msg", args=(), exc_info=None,
    )
    record.trade_id = "abc-123"
    record.amount = 25.0
    obj = json.loads(formatter.format(record))
    assert obj["trade_id"] == "abc-123"
    assert obj["amount"] == 25.0


def test_setup_logging_installs_json_formatter(monkeypatch):
    """setup_logging installs _JsonFormatter on the root logger by default."""
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    import obs
    obs.setup_logging("svc")
    root = logging.getLogger()
    stream = [h for h in root.handlers
              if isinstance(h, logging.StreamHandler)
              and not isinstance(h, obs._ErrorRingBuffer)]
    assert stream, "No StreamHandler on root logger"
    assert isinstance(stream[0].formatter, obs._JsonFormatter)


def test_setup_logging_text_format(monkeypatch):
    """LOG_FORMAT=text installs a plain-text Formatter, not _JsonFormatter."""
    monkeypatch.setenv("LOG_FORMAT", "text")
    import obs
    obs.setup_logging("svc")
    root = logging.getLogger()
    stream = [h for h in root.handlers
              if isinstance(h, logging.StreamHandler)
              and not isinstance(h, obs._ErrorRingBuffer)]
    assert stream
    assert not isinstance(stream[0].formatter, obs._JsonFormatter)


def test_error_ring_buffer_captures_error():
    """_ErrorRingBuffer stores ERROR-level records."""
    from obs import _ErrorRingBuffer
    buf = _ErrorRingBuffer(maxsize=5)
    record = logging.LogRecord(
        name="n", level=logging.ERROR, pathname="",
        lineno=0, msg="critical %s", args=("failure",), exc_info=None,
    )
    buf.emit(record)
    last = buf.last()
    assert last is not None
    assert "critical failure" in last["msg"]
    assert "ts" in last
    assert "logger" in last


def test_error_ring_buffer_respects_maxsize():
    """_ErrorRingBuffer discards oldest entries beyond maxsize."""
    from obs import _ErrorRingBuffer
    buf = _ErrorRingBuffer(maxsize=3)
    for i in range(5):
        r = logging.LogRecord(
            name="n", level=logging.ERROR, pathname="",
            lineno=0, msg=f"error {i}", args=(), exc_info=None,
        )
        buf.emit(r)
    assert "error 4" in buf.last()["msg"]
    assert len(buf._buf) == 3


def test_get_last_error_none_before_setup():
    """get_last_error returns None when setup_logging hasn't been called."""
    import importlib
    import obs
    importlib.reload(obs)
    assert obs.get_last_error() is None


def test_get_last_error_after_error_logged():
    """get_last_error returns the last ERROR record after setup_logging."""
    import obs
    obs.setup_logging("test")
    logging.getLogger("obs_test").error("something broke badly")
    err = obs.get_last_error()
    assert err is not None
    assert "something broke badly" in err["msg"]


# ── 2. RotatingFileHandler in bot_strategy.py ────────────────────────────────

def test_brain_log_uses_rotating_file_handler():
    """brain logger is wired to a RotatingFileHandler with correct params."""
    from logging.handlers import RotatingFileHandler
    import bot_strategy  # noqa: F401 — registers handlers as side effect
    brain = logging.getLogger("brain")
    rfh = [h for h in brain.handlers if isinstance(h, RotatingFileHandler)]
    assert rfh, "brain logger missing RotatingFileHandler — still using FileHandler?"
    assert rfh[0].maxBytes == 5_000_000
    assert rfh[0].backupCount == 3


# ── 3 & 4. Flask /metrics and /healthz ───────────────────────────────────────

@pytest.fixture()
def _db_with_trade(tmp_path):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY,
        ts TEXT,
        fill_confirmed INTEGER,
        outcome TEXT,
        pnl_dollars REAL
    )""")
    conn.execute("INSERT INTO trades VALUES (1, datetime('now'), 1, 'win', 5.0)")
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture()
def _fresh_state(tmp_path):
    p = tmp_path / "bot_state.json"
    p.write_text(json.dumps({"phase": "watching"}))
    return str(p)


@pytest.fixture()
def flask_client(_db_with_trade, _fresh_state, monkeypatch):
    monkeypatch.setenv("BOT_DB_FILE",    _db_with_trade)
    monkeypatch.setenv("BOT_STATE_FILE", _fresh_state)
    import server
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


def test_metrics_returns_200(flask_client):
    assert flask_client.get("/metrics").status_code == 200


def test_metrics_has_required_keys(flask_client):
    """/metrics response contains all expected keys."""
    data = flask_client.get("/metrics").get_json()
    required = {
        "uptime_seconds", "last_trade_ts", "trade_count_24h",
        "fill_confirmed_rate_24h", "bot_enabled", "mode",
        "bot_state_age_seconds", "last_error_ts", "last_error_msg",
    }
    missing = required - data.keys()
    assert not missing, f"Missing from /metrics: {missing}"


def test_metrics_trade_count_reflects_db(flask_client):
    data = flask_client.get("/metrics").get_json()
    assert data["trade_count_24h"] == 1
    assert data["last_trade_ts"] is not None


def test_metrics_no_db_returns_200(tmp_path, monkeypatch):
    """/metrics must not raise when DB is missing."""
    monkeypatch.setenv("BOT_DB_FILE",    str(tmp_path / "nonexistent.db"))
    monkeypatch.setenv("BOT_STATE_FILE", str(tmp_path / "nostate.json"))
    import server
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        resp = c.get("/metrics")
    assert resp.status_code == 200


def test_healthz_ok_when_state_fresh(_fresh_state, tmp_path, monkeypatch):
    """/healthz → 200 when bot_state.json mtime is recent."""
    monkeypatch.setenv("BOT_STATE_FILE", _fresh_state)
    monkeypatch.setenv("BOT_DB_FILE",    str(tmp_path / "nodb.db"))
    import server
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        resp = c.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_healthz_503_when_state_stale(tmp_path, monkeypatch):
    """/healthz → 503 with age field when bot_state.json is > 120s old."""
    state = tmp_path / "bot_state.json"
    state.write_text(json.dumps({"phase": "watching"}))
    old_ts = time.time() - 200
    os.utime(str(state), (old_ts, old_ts))

    monkeypatch.setenv("BOT_STATE_FILE", str(state))
    monkeypatch.setenv("BOT_DB_FILE",    str(tmp_path / "nodb.db"))
    import server
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        resp = c.get("/healthz")
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "stale"
    assert data["age"] >= 180


def test_healthz_503_when_state_missing(tmp_path, monkeypatch):
    """/healthz → 503 when bot_state.json does not exist."""
    monkeypatch.setenv("BOT_STATE_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("BOT_DB_FILE",    str(tmp_path / "nodb.db"))
    import server
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        resp = c.get("/healthz")
    assert resp.status_code == 503
