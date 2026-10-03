import sqlite3

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session

from orbis.storage.db import Scan, open_db
from orbis.storage.repo import create_scan


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "scan.db"
    engine = open_db(path)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout=20")
    blocker = sqlite3.connect(path, timeout=0.02)
    try:
        yield engine, blocker
    finally:
        blocker.close()
        engine.dispose()


@pytest.mark.parametrize("expire_on_commit", [False, True])
@pytest.mark.parametrize("pending_scan", [False, True])
def test_committed_scan_id_does_not_require_another_database_read(
    database, expire_on_commit, pending_scan,
):
    engine, blocker = database
    with Session(engine, expire_on_commit=expire_on_commit) as session:
        if pending_scan:
            session.add(Scan(target="http://localhost/previous"))

        def lock_after_commit(current):
            blocker.execute("BEGIN EXCLUSIVE")

        event.listen(session, "after_commit", lock_after_commit, once=True)
        try:
            scan_id = create_scan(session, "http://localhost/current", "auth.json")
            assert blocker.in_transaction
        finally:
            blocker.rollback()

    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT id, target, auth_state_path FROM scan ORDER BY id"
        ).all()
    assert len(rows) == 1 + pending_scan
    assert rows[-1] == (scan_id, "http://localhost/current", "auth.json")
    if pending_scan:
        assert rows[0].target == "http://localhost/previous"
        assert rows[0].id != scan_id


@pytest.mark.parametrize("expire_on_commit", [False, True])
@pytest.mark.parametrize("pending_scan", [False, True])
def test_failed_commit_does_not_return_a_scan_id(database, expire_on_commit, pending_scan):
    engine, blocker = database
    with Session(engine, expire_on_commit=expire_on_commit) as session:
        if pending_scan:
            session.add(Scan(target="http://localhost/previous"))

        def hold_reader_lock(current, context):
            blocker.execute("BEGIN")
            blocker.execute("SELECT * FROM scan").fetchall()

        event.listen(session, "after_flush_postexec", hold_reader_lock, once=True)
        try:
            with pytest.raises(OperationalError) as raised:
                create_scan(session, "http://localhost/current")
            assert raised.value.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY
            assert raised.value.statement is None
        finally:
            blocker.rollback()
            session.rollback()

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM scan").scalar_one() == 0


def test_failed_insert_does_not_commit_other_pending_rows(database):
    engine, blocker = database
    blocker.execute(
        "CREATE TRIGGER reject_current BEFORE INSERT ON scan "
        "WHEN NEW.target = 'http://localhost/current' "
        "BEGIN SELECT RAISE(ABORT, 'fixture insert failure'); END"
    )
    blocker.commit()
    with Session(engine) as session:
        session.add(Scan(target="http://localhost/previous"))
        with pytest.raises(IntegrityError):
            create_scan(session, "http://localhost/current")
        session.rollback()

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM scan").scalar_one() == 0


@pytest.mark.parametrize("expire_on_commit", [False, True])
def test_before_commit_can_assign_scan_identity(database, expire_on_commit):
    engine, _ = database
    with Session(engine, expire_on_commit=expire_on_commit) as session:
        def assign_identity(current):
            for pending in current.new:
                if isinstance(pending, Scan):
                    pending.id = 77

        event.listen(session, "before_commit", assign_identity, once=True)
        scan_id = create_scan(session, "http://localhost/current")

    assert scan_id == 77
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT id FROM scan").scalar_one() == 77
