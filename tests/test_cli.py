"""Tests for CLI rendering helpers."""

from types import SimpleNamespace

from orbis.cli import _print_table


def test_endpoint_table_shows_probe_code_by_default(capsys) -> None:
    endpoint = SimpleNamespace(
        id=1,
        method="GET",
        host="example.com",
        path_template="/api/users",
        route_kind="application_api",
        source="passive",
        probe_status="verified",
        probe_code=405,
        seen_count=1,
    )

    _print_table([endpoint])

    output = capsys.readouterr().out
    assert "code" in output
    assert "405" in output
