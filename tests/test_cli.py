"""Tests for CLI rendering helpers."""

from types import SimpleNamespace

from typer.testing import CliRunner

from orbis.cli import _print_table, app


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
