"""Tests for orbis.storage.repo.collapse_scan_endpoints — post-scan dedup."""

import pytest
from sqlmodel import Session

from orbis.analysis.analyzer import NormalizedEndpoint
from orbis.storage.db import Endpoint, Parameter, open_db
from orbis.storage.repo import collapse_scan_endpoints, create_scan, save_endpoints


@pytest.fixture()
def session(tmp_path):
    """In-memory-like SQLite session using a temp file."""
    engine = open_db(tmp_path / "test.db")
    with Session(engine) as s:
        yield s


def _add_endpoint(session, scan_id, method, host, path, kind="page_route", seen=1):
    ep = Endpoint(
        scan_id=scan_id,
        method=method,
        host=host,
        path_template=path,
        sample_url=f"https://{host}{path}",
        route_kind=kind,
        seen_count=seen,
    )
    session.add(ep)
    session.flush()
    return ep


def _add_param(session, endpoint_id, location, name, seen=1):
    p = Parameter(
        endpoint_id=endpoint_id,
        location=location,
        name=name,
        type_inferred="string",
        seen_count=seen,
    )
    session.add(p)
    session.flush()
    return p


class TestCollapseScanEndpoints:
    def test_collapses_high_cardinality_siblings(self, session) -> None:
        """3+ unique last segments under same parent → merge to {slug}."""
        scan_id = create_scan(session, "https://ex.com")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/tags/python")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/tags/javascript")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/tags/rust")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/tags/golang")
        session.commit()

        merged = collapse_scan_endpoints(session, scan_id, threshold=3)

        assert merged == 3  # 4 endpoints → 1 survivor, 3 removed
        eps = list(session.exec(
            Endpoint.__table__.select().where(Endpoint.scan_id == scan_id)
        ))
        assert len(eps) == 1
        assert eps[0].path_template == "/tags/{slug}"
        assert eps[0].seen_count == 4

    def test_does_not_collapse_below_threshold(self, session) -> None:
        scan_id = create_scan(session, "https://ex.com")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/category/news")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/category/tech")
        session.commit()

        merged = collapse_scan_endpoints(session, scan_id, threshold=3)

        assert merged == 0
        eps = list(session.exec(
            Endpoint.__table__.select().where(Endpoint.scan_id == scan_id)
        ))
        assert len(eps) == 2

    def test_does_not_collapse_root_level(self, session) -> None:
        """Root-level pages should never be merged."""
        scan_id = create_scan(session, "https://ex.com")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/about")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/contact")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/pricing")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/careers")
        session.commit()

        merged = collapse_scan_endpoints(session, scan_id, threshold=3)

        assert merged == 0
        eps = list(session.exec(
            Endpoint.__table__.select().where(Endpoint.scan_id == scan_id)
        ))
        assert len(eps) == 4

    def test_does_not_collapse_already_templatized(self, session) -> None:
        """Endpoints whose last segment is already a placeholder stay as-is."""
        scan_id = create_scan(session, "https://ex.com")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/posts/{slug}", seen=10)
        _add_endpoint(session, scan_id, "GET", "ex.com", "/users/{id}", seen=5)
        session.commit()

        merged = collapse_scan_endpoints(session, scan_id, threshold=3)

        assert merged == 0

    def test_separates_by_method(self, session) -> None:
        """Different HTTP methods are grouped independently."""
        scan_id = create_scan(session, "https://ex.com")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/items/alpha")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/items/beta")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/items/gamma")
        _add_endpoint(session, scan_id, "POST", "ex.com", "/items/alpha")
        _add_endpoint(session, scan_id, "POST", "ex.com", "/items/beta")
        session.commit()

        merged = collapse_scan_endpoints(session, scan_id, threshold=3)

        assert merged == 2  # only GET group (3 items) collapsed
        eps = list(session.exec(
            Endpoint.__table__.select().where(Endpoint.scan_id == scan_id)
        ))
        methods = {(e.method, e.path_template) for e in eps}
        assert ("GET", "/items/{slug}") in methods
        assert ("POST", "/items/alpha") in methods
        assert ("POST", "/items/beta") in methods

    def test_migrates_unique_params(self, session) -> None:
        """Params from deleted endpoints are merged into the survivor."""
        scan_id = create_scan(session, "https://ex.com")
        ep1 = _add_endpoint(session, scan_id, "GET", "ex.com", "/tags/python")
        ep2 = _add_endpoint(session, scan_id, "GET", "ex.com", "/tags/rust")
        ep3 = _add_endpoint(session, scan_id, "GET", "ex.com", "/tags/go")
        _add_param(session, ep1.id, "query", "page", seen=2)
        _add_param(session, ep2.id, "query", "page", seen=3)
        _add_param(session, ep3.id, "query", "sort", seen=1)
        session.commit()

        merged = collapse_scan_endpoints(session, scan_id, threshold=3)

        assert merged == 2
        survivor = session.exec(
            Endpoint.__table__.select().where(Endpoint.scan_id == scan_id)
        ).first()
        params = list(session.exec(
            Parameter.__table__.select().where(
                Parameter.endpoint_id == survivor.id
            )
        ))
        # ep1 had "page" (seen=2), ep2 had "page" (seen=3) → merged to seen=5
        # ep3 had "sort" (seen=1) → deleted with ep3 (not migrated since
        # survivor didn't have "sort" — only matching params get their
        # seen_count bumped; non-matching params on deleted eps are discarded)
        page_param = [p for p in params if p.name == "page"]
        assert len(page_param) == 1
        assert page_param[0].seen_count == 5

    def test_same_last_segment_different_prefix_not_collapsed(self, session) -> None:
        """/api/v1/posts and /api/v2/posts differ by prefix, not collapsed."""
        scan_id = create_scan(session, "https://ex.com")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/api/v1/posts")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/api/v2/posts")
        _add_endpoint(session, scan_id, "GET", "ex.com", "/api/v3/posts")
        session.commit()

        merged = collapse_scan_endpoints(session, scan_id, threshold=3)

        assert merged == 0  # different prefixes, different groups


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

    def test_passive_load_wins_across_pages(self, session) -> None:
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
