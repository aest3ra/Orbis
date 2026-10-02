import asyncio
import errno
import os
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from orbis import telemetry
from orbis.storage import db


@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_schema_creation_failure_disposes_engine(monkeypatch, failure_type, cleanup_fails):
    engine = Mock()
    failure = failure_type("private-schema-error")
    monkeypatch.setattr(db, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(db.SQLModel.metadata, "create_all", Mock(side_effect=failure))
    if cleanup_fails:
        engine.dispose.side_effect = OSError("private-disposal-error")

    with pytest.raises(failure_type) as raised:
        db.open_db("unused.db")

    assert raised.value is failure
    engine.dispose.assert_called_once_with()
    if cleanup_fails:
        assert failure.__notes__ == ["Database initialization cleanup failed: OSError"]


def test_schema_failure_releases_real_pooled_connection(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'scan.db'}")
    pool = engine.pool
    failure = RuntimeError("synthetic schema failure")

    def failed_schema(created_engine):
        with created_engine.connect():
            pass
        assert pool.checkedin() == 1
        raise failure

    monkeypatch.setattr(db, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(db.SQLModel.metadata, "create_all", failed_schema)
    try:
        with pytest.raises(RuntimeError) as raised:
            db.open_db(tmp_path / "scan.db")
        assert raised.value is failure
        assert pool.checkedin() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("failure_type", [OSError, asyncio.CancelledError])
def test_log_wrapper_failure_releases_real_descriptor(tmp_path, monkeypatch, failure_type):
    opened = []
    original_open = os.open
    failure = failure_type("private-file-wrapper-error")

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(telemetry.os, "open", tracked_open)
    monkeypatch.setattr(telemetry.os, "fdopen", Mock(side_effect=failure))
    try:
        with pytest.raises(failure_type) as raised:
            telemetry.ScanLog(str(tmp_path / "scan.db"), 1)
        assert raised.value is failure
        assert len(opened) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(opened[0])
        assert closed.value.errno == errno.EBADF
    finally:
        for descriptor in opened:
            try:
                os.close(descriptor)
            except OSError:
                pass


def test_log_descriptor_close_failure_does_not_mask_wrapper_failure(monkeypatch, tmp_path):
    failure = OSError("private-wrapper-error")
    monkeypatch.setattr(telemetry.os, "open", Mock(return_value=12345))
    monkeypatch.setattr(telemetry.os, "fdopen", Mock(side_effect=failure))
    close = Mock(side_effect=OSError("private-close-error"))
    monkeypatch.setattr(telemetry.os, "close", close)

    with pytest.raises(OSError) as raised:
        telemetry.ScanLog(str(tmp_path / "scan.db"), 1)

    assert raised.value is failure
    close.assert_called_once_with(12345)
    assert failure.__notes__ == ["Scan log initialization cleanup failed: OSError"]


def test_successful_construction_transfers_resource_ownership(monkeypatch, tmp_path):
    engine = Mock()
    monkeypatch.setattr(db, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(db.SQLModel.metadata, "create_all", Mock())
    assert db.open_db("unused.db") is engine
    engine.dispose.assert_not_called()
    trace = telemetry.ScanLog(str(tmp_path / "scan.db"), 1)
    try:
        trace.emit("scan_start")
        assert trace.path.read_text()
    finally:
        trace.close()
