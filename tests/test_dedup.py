"""Tests for orbis.storage.repo: saving and merging endpoint evidence."""

import json

import pytest
from sqlmodel import Session, select

from orbis.analysis.analyzer import MAX_SAMPLES, NormalizedEndpoint, NormalizedParam
from orbis.storage.db import Endpoint, Parameter, open_db
from orbis.storage.repo import create_scan, save_endpoints


@pytest.fixture()
def session(tmp_path):
    """In-memory-like SQLite session using a temp file."""
    engine = open_db(tmp_path / "test.db")
    with Session(engine) as s:
        yield s


def _norm(method, host, path, *, discovered_via=None, seen=1):
    return NormalizedEndpoint(
        method=method,
        host=host,
        path_template=path,
        sample_url=f"https://{host}{path}",
        route_kind="application_api",
        seen_count=seen,
        discovered_via=discovered_via,
    )


class TestSaveDiscoveredVia:
    def test_persists_interaction_tag(self, session) -> None:
        scan_id = create_scan(session, "https://ex.com")
        save_endpoints(session, scan_id, [
            _norm("GET", "ex.com", "/api/more", discovered_via="Load more"),
        ])
        row = session.exec(
            Endpoint.__table__.select().where(Endpoint.scan_id == scan_id)
        ).first()
        assert row.discovered_via == "Load more"

    def test_page_load_wins_across_pages(self, session) -> None:
        """First page reaches it via a click; a later page sees it on load."""
        scan_id = create_scan(session, "https://ex.com")
        save_endpoints(session, scan_id, [
            _norm("GET", "ex.com", "/api/items", discovered_via="Load more"),
        ])
        save_endpoints(session, scan_id, [
            _norm("GET", "ex.com", "/api/items", discovered_via=None),
        ])
        rows = list(session.exec(
            Endpoint.__table__.select().where(Endpoint.scan_id == scan_id)
        ))
        assert len(rows) == 1
        assert rows[0].discovered_via is None
        assert rows[0].seen_count == 2


def test_save_params_preserves_samples_across_pages(session) -> None:
    scan_id = create_scan(session, "https://ex.com")
    for values in (["first", "shared"], ["shared", "second"], []):
        ep = _norm("GET", "ex.com", "/api/search")
        ep.params[("query", "q")] = NormalizedParam(
            "query", "q", "string", values, 1,
        )
        save_endpoints(session, scan_id, [ep])
    param = session.exec(select(Parameter)).one()
    assert json.loads(param.sample_values_json) == ["first", "shared", "second"]
    assert param.seen_count == 3


def test_save_params_caps_union_of_samples(session) -> None:
    scan_id = create_scan(session, "https://ex.com")
    for i in range(MAX_SAMPLES + 2):
        ep = _norm("GET", "ex.com", "/api/search")
        ep.params[("query", "q")] = NormalizedParam(
            "query", "q", "string", [str(i)], 1,
        )
        save_endpoints(session, scan_id, [ep])
    param = session.exec(select(Parameter)).one()
    assert json.loads(param.sample_values_json) == [str(i) for i in range(MAX_SAMPLES)]


def test_static_sighting_does_not_clear_dynamic_interaction(session) -> None:
    scan_id = create_scan(session, "https://ex.com")
    dynamic = _norm("GET", "ex.com", "/api/search", discovered_via="Search")
    static = _norm("GET", "ex.com", "/api/search")
    static.source = "static_js"
    save_endpoints(session, scan_id, [dynamic, static])
    row = session.exec(select(Endpoint)).one()
    assert row.source == "dynamic"
    assert row.discovered_via == "Search"


def test_dynamic_upgrade_keeps_interaction_label(session) -> None:
    scan_id = create_scan(session, "https://ex.com")
    static = _norm("GET", "ex.com", "/api/search")
    static.source = "static_js"
    dynamic = _norm("GET", "ex.com", "/api/search", discovered_via="Search")
    save_endpoints(session, scan_id, [static, dynamic])
    row = session.exec(select(Endpoint)).one()
    assert row.source == "dynamic" and row.discovered_via == "Search"


