"""Tests for CLI rendering helpers."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import Session
from typer.testing import CliRunner

from orbis import cli
from orbis.cli import _print_table, app
from orbis.storage.db import Endpoint, Scan, open_db


def test_endpoint_table_shows_probe_code_by_default(capsys) -> None:
    endpoint = SimpleNamespace(
        id=1,
        method="GET",
        host="example.com",
        path_template="/api/users",
        route_kind="application_api",
        source="wayback",
        probe_status="verified",
        probe_code=405,
        seen_count=1,
    )

    _print_table([endpoint])

    output = capsys.readouterr().out
    assert "code" in output
    assert "405" in output


def test_scan_help_has_no_removed_layer_toggles() -> None:
    result = CliRunner().invoke(app, ["scan", "--help"])

    assert result.exit_code == 0
    assert "--js-analysis" not in result.output
    assert "--no-js-analysis" not in result.output
    assert "--crawl-mode" not in result.output
    assert "--probe" not in result.output
    assert "--no-probe" not in result.output
    assert "--" + "pass" + "ive" not in result.output
    assert "--no-" + "pass" + "ive" not in result.output
    assert "--max-pages" not in result.output
    assert "--max-depth" not in result.output
    assert "--max-duration" not in result.output
    assert "--per-template" not in result.output
    assert "--max-scrolls" not in result.output
    assert "--wayback" in result.output
    assert "--no-wayback" in result.output


def test_list_help_has_no_filter_options() -> None:
    result = CliRunner().invoke(app, ["list", "--help"])

    assert result.exit_code == 0
    assert "--kind" not in result.output
    assert "--source" not in result.output
    assert "--probe-status" not in result.output


@pytest.fixture
def database_resources(tmp_path, monkeypatch):
    database = tmp_path / "cli.db"
    seed_engine = open_db(database)
    try:
        with Session(seed_engine) as session:
            session.add(Scan(id=1, target="http://localhost/"))
            session.add(Endpoint(id=1, scan_id=1, method="GET", host="localhost", path_template="/api/one",
                                 sample_url="http://localhost/api/one", route_kind="application_api"))
            session.commit()
    finally:
        seed_engine.dispose()
    resources = SimpleNamespace(database=database, engines=[], dispose_error=None,
                                scan=AsyncMock(return_value=1), output_dispose_counts=[])

    def tracked_open(path):
        engine = open_db(path)
        with engine.connect() as connection:
            raw = connection.connection.driver_connection
        original_dispose = engine.dispose
        checked_out_at_dispose = []

        def dispose():
            checked_out_at_dispose.append(engine.pool.checkedout())
            original_dispose()
            if resources.dispose_error is not None:
                raise resources.dispose_error

        engine.dispose = Mock(side_effect=dispose)
        resources.engines.append(SimpleNamespace(engine=engine, raw=raw, cleanup=original_dispose,
                                                 checked_out_at_dispose=checked_out_at_dispose))
        return engine

    factory = Mock(side_effect=tracked_open)
    resources.factory = factory
    monkeypatch.setattr(cli, "open_db", factory)
    monkeypatch.setattr(cli, "run_scan", resources.scan)
    yield resources
    for owned in resources.engines:
        owned.cleanup()


def cli_arguments(command, database):
    if command == "scan":
        return ["scan", "http://localhost/", "--db", str(database), "--no-wayback"]
    return [command, str(database)] + (["1"] if command == "inspect" else [])


def direct_command(command, database):
    if command == "scan":
        cli.scan("http://localhost/", db=database, auth=None, headless=True, wayback=False, verbose=False)
    elif command == "list":
        cli.list_cmd(database)
    else:
        cli.inspect(database, 1)


def assert_database_released(resources):
    owned, = resources.engines
    owned.engine.dispose.assert_called_once_with()
    assert owned.checked_out_at_dispose == [0]
    with pytest.raises(sqlite3.ProgrammingError):
        owned.raw.execute("SELECT 1")


@pytest.mark.parametrize("command", ["scan", "list", "inspect"])
@pytest.mark.parametrize("mode", ["success", "empty", "query_failure", "output_failure"])
def test_cli_releases_owned_database_before_output(database_resources, monkeypatch, command, mode):
    resources = database_resources
    if mode == "empty":
        engine = open_db(resources.database)
        try:
            with Session(engine) as session:
                session.delete(session.get(Endpoint, 1))
                session.commit()
        finally:
            engine.dispose()
    if mode == "query_failure":
        def fail_query(session, *args):
            return session.exec(text("SELECT * FROM missing_cli_test_table")).all()

        monkeypatch.setattr(cli, "get_endpoint_with_params" if command == "inspect" else "list_endpoints", fail_query)
    original_print = cli.print
    output_error = OSError("private output failure")

    def checked_print(*args, **kwargs):
        if resources.engines:
            resources.output_dispose_counts.append(resources.engines[0].engine.dispose.call_count)
            if mode == "output_failure":
                raise output_error
        return original_print(*args, **kwargs)

    monkeypatch.setattr(cli, "print", checked_print)
    result = CliRunner().invoke(app, cli_arguments(command, resources.database))

    if mode == "query_failure":
        assert isinstance(result.exception, OperationalError)
    elif mode == "output_failure":
        assert result.exception is output_error
    elif mode == "empty" and command == "inspect":
        assert result.exit_code == 1
        assert "not found" in result.output
    else:
        assert result.exit_code == 0
        assert "no " in result.output if mode == "empty" else "/api/one" in result.output
    assert_database_released(resources)
    assert all(count == 1 for count in resources.output_dispose_counts)
    assert resources.scan.await_count == (1 if command == "scan" else 0)


@pytest.mark.parametrize("command", ["scan", "list", "inspect"])
def test_successful_query_does_not_hide_disposal_failure(database_resources, command):
    resources = database_resources
    resources.dispose_error = OSError("private disposal failure")

    with pytest.raises(OSError) as raised:
        direct_command(command, resources.database)

    assert raised.value is resources.dispose_error
    assert_database_released(resources)


@pytest.mark.parametrize("command", ["list", "inspect"])
def test_missing_database_is_rejected_before_engine_creation(database_resources, command):
    resources = database_resources

    result = CliRunner().invoke(app, cli_arguments(command, resources.database.with_name("absent.db")))

    assert result.exit_code == 2
    resources.factory.assert_not_called()


def test_failed_scan_does_not_open_a_postprocessing_engine(database_resources):
    resources = database_resources
    failure = RuntimeError("scan did not complete")
    resources.scan.side_effect = failure

    with pytest.raises(RuntimeError) as raised:
        direct_command("scan", resources.database)

    assert raised.value is failure
    resources.factory.assert_not_called()


@pytest.mark.parametrize("command", ["scan", "list", "inspect"])
@pytest.mark.parametrize("stage", ["engine_creation", "session_entry"])
def test_cli_acquisition_failure_disposes_only_owned_engine(database_resources, monkeypatch, command, stage):
    resources = database_resources
    failure = RuntimeError("database acquisition failed")
    if stage == "engine_creation":
        resources.factory.side_effect = failure
    else:
        session = MagicMock()
        session.__enter__.side_effect = failure
        monkeypatch.setattr(cli, "Session", Mock(return_value=session))

    with pytest.raises(RuntimeError) as raised:
        direct_command(command, resources.database)

    assert raised.value is failure
    if stage == "engine_creation":
        assert resources.engines == []
    else:
        assert_database_released(resources)


@pytest.mark.parametrize("failure_type", [asyncio.CancelledError, KeyboardInterrupt])
@pytest.mark.parametrize("query_fails", [False, True])
def test_fresh_disposal_interruption_propagates(database_resources, monkeypatch, failure_type, query_fails):
    resources = database_resources
    interruption = failure_type("stop disposal")
    resources.dispose_error = interruption
    if query_fails:
        def fail_query(session):
            session.exec(text("SELECT 1")).one()
            raise RuntimeError("earlier query failure")

        monkeypatch.setattr(cli, "list_endpoints", fail_query)

    with pytest.raises(failure_type) as raised:
        direct_command("list", resources.database)

    assert raised.value is interruption
    assert_database_released(resources)


def test_successful_query_does_not_hide_real_session_close_failure(database_resources):
    with pytest.raises(ProgrammingError) as raised:
        with cli._database_session(database_resources.database) as session:
            session.exec(text("SELECT 1")).one()
            session.connection().connection.driver_connection.close()

    assert isinstance(raised.value.orig, sqlite3.ProgrammingError)
    assert getattr(raised.value, "__notes__", []) == []
    assert_database_released(database_resources)


@pytest.mark.parametrize("dispose_fails", [False, True])
def test_session_suppression_does_not_preserve_suppressed_body_failure(database_resources, monkeypatch, dispose_fails):
    failure = RuntimeError("suppressed body failure")
    session = MagicMock()
    session.__exit__.return_value = True
    monkeypatch.setattr(cli, "Session", Mock(return_value=session))
    if dispose_fails:
        database_resources.dispose_error = OSError("disposal failure")

    def exercise():
        with cli._database_session(database_resources.database):
            raise failure

    if dispose_fails:
        with pytest.raises(OSError) as raised:
            exercise()
        assert raised.value is database_resources.dispose_error
    else:
        exercise()
    assert getattr(failure, "__notes__", []) == []
    session.__exit__.assert_called_once()
    assert session.__exit__.call_args.args[1] is failure
    assert_database_released(database_resources)


def test_same_body_and_session_error_is_not_a_secondary_failure(database_resources, monkeypatch):
    failure = RuntimeError("same failure")
    session = MagicMock()
    session.__exit__.side_effect = failure
    monkeypatch.setattr(cli, "Session", Mock(return_value=session))

    with pytest.raises(RuntimeError) as raised:
        with cli._database_session(database_resources.database):
            raise failure

    assert raised.value is failure
    assert getattr(failure, "__notes__", []) == []
    assert_database_released(database_resources)
