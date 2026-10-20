import asyncio
import errno
import os
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from orbis import telemetry
from orbis.storage import db


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


@pytest.mark.parametrize("failure_type", [RuntimeError, OSError])
def test_schema_creation_failure_disposes_engine(monkeypatch, failure_type):
    engine = Mock()
    failure = failure_type("private-schema-error")
    monkeypatch.setattr(db, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(db.SQLModel.metadata, "create_all", Mock(side_effect=failure))

    with pytest.raises(failure_type) as raised:
        db.open_db("unused.db")

    assert raised.value is failure
    engine.dispose.assert_called_once_with()
