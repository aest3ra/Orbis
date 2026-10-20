import json
from itertools import permutations
from urllib.parse import urlparse

import pytest
from sqlmodel import Session, select

from orbis.analysis.analyzer import (
    NormalizedEndpoint,
    NormalizedParam,
    _accumulate,
    _accumulate_wayback,
    _merge_endpoint,
)
from orbis.analysis.params import infer_type
from orbis.crawler.browser import NetworkEvent
from orbis.storage.db import Endpoint, Parameter, open_db
from orbis.storage.repo import collapse_scan_endpoints, create_scan, save_endpoints


@pytest.fixture()
def session(tmp_path):
    engine = open_db(tmp_path / "integrity.db")
    with Session(engine) as session:
        yield session


def _endpoint(path, kind="application_api", source="dynamic"):
    return NormalizedEndpoint(
        "GET", "local.invalid", path, f"http://local.invalid{path}", kind,
        source=source,
    )


def test_http_and_https_endpoints_remain_separate(session):
    scan_id = create_scan(session, "https://local.invalid")
    endpoints = {}
    for url in ("http://local.invalid/api/users", "https://local.invalid/api/users"):
        _accumulate(endpoints, NetworkEvent(url, "GET", url, "Fetch"), "application_api")
    save_endpoints(session, scan_id, list(endpoints.values()))
    rows = session.exec(select(Endpoint)).all()
    assert {row.host for row in rows} == {"http://local.invalid:80", "https://local.invalid:443"}


@pytest.mark.parametrize("reverse", [False, True])
def test_source_priority_preserves_observed_route_kind(session, reverse):
    scan_id = create_scan(session, "http://local.invalid")
    observations = [
        _endpoint("/api/report", source="static_js"),
        _endpoint("/api/report", kind="download"),
    ]
    if reverse:
        observations.reverse()
    for endpoint in observations:
        save_endpoints(session, scan_id, [endpoint])
    row = session.exec(select(Endpoint)).one()
    assert row.source == "dynamic"
    assert row.route_kind == "download"


@pytest.mark.parametrize("source", ["dynamic", "static_js", "wayback"])
@pytest.mark.parametrize("values", list(permutations(("all", "1", "true"))))
@pytest.mark.parametrize("batch_sizes", [(3,), (1, 2), (2, 1), (1, 1, 1)])
def test_parameter_types_survive_order_and_batching(session, source, values, batch_sizes):
    scan_id = create_scan(session, "http://local.invalid")
    start = 0
    for batch_size in batch_sizes:
        endpoints = {}
        for value in values[start:start + batch_size]:
            url = f"http://local.invalid/api/search?q={value}"
            if source == "dynamic":
                event = NetworkEvent(value, "GET", url, "Fetch")
                _accumulate(endpoints, event, "application_api")
            elif source == "wayback":
                _accumulate_wayback(endpoints, urlparse(url))
            else:
                _merge_endpoint(
                    endpoints, "GET", url, source=source,
                    params=[("query", "q", infer_type(value))],
                )
        save_endpoints(session, scan_id, list(endpoints.values()))
        start += batch_size
    param = session.exec(select(Parameter)).one()
    assert param.type_inferred == "mixed"
    if source != "static_js":
        assert param.seen_count == 3
        assert set(json.loads(param.sample_values_json)) == set(values)


@pytest.mark.parametrize("values", [("all", "1"), ("1", "all")])
def test_repeated_archived_query_values_merge_types(values):
    endpoints = {}
    _accumulate_wayback(
        endpoints, urlparse(f"http://local.invalid/api/search?q={values[0]}&q={values[1]}"),
    )
    param = next(iter(endpoints.values())).params[("query", "q")]
    assert param.type_inferred == "mixed"
    assert set(param.sample_values) == set(values)


@pytest.mark.parametrize("types", [
    ("int", "int", "int"),
    *permutations(("int", "string", "bool")),
    ("mixed", "int", "int"),
])
def test_page_collapse_merges_parameter_types(session, types):
    scan_id = create_scan(session, "http://local.invalid")
    for slug, type_name in zip(("alpha", "beta", "gamma"), types):
        endpoint = _endpoint(f"/tags/{slug}", kind="page_route")
        endpoint.params[("query", "q")] = NormalizedParam(
            "query", "q", type_name, [slug], 1,
        )
        save_endpoints(session, scan_id, [endpoint])
    assert collapse_scan_endpoints(session, scan_id) == 2
    param = session.exec(select(Parameter)).one()
    assert param.type_inferred == ("int" if len(set(types)) == 1 else "mixed")
    assert param.seen_count == 3
    assert set(json.loads(param.sample_values_json)) == {"alpha", "beta", "gamma"}


@pytest.mark.parametrize("kind", ["application_api", "download", "unknown"])
def test_non_page_siblings_retain_all_observed_rows(session, kind):
    scan_id = create_scan(session, "http://local.invalid")
    names = ("users", "orders", "settings", "health", "version", "search", "reports", "metrics")
    save_endpoints(session, scan_id, [_endpoint(f"/api/{name}", kind) for name in names])
    before = [row.model_dump() for row in session.exec(select(Endpoint)).all()]
    assert collapse_scan_endpoints(session, scan_id, threshold=8) == 0
    assert [row.model_dump() for row in session.exec(select(Endpoint)).all()] == before


@pytest.mark.parametrize("kind", ["application_api", "download", "unknown"])
@pytest.mark.parametrize("page_count", [2, 3])
def test_non_page_sibling_is_excluded_from_page_collapse(session, kind, page_count):
    scan_id = create_scan(session, "http://local.invalid")
    save_endpoints(session, scan_id, [_endpoint("/items/export", kind)])
    protected = session.exec(select(Endpoint)).one().model_dump()
    for slug in ("alpha", "beta", "gamma")[:page_count]:
        save_endpoints(session, scan_id, [_endpoint(f"/items/{slug}", "page_route")])
    assert collapse_scan_endpoints(session, scan_id) == (2 if page_count == 3 else 0)
    row = session.exec(select(Endpoint).where(Endpoint.path_template == "/items/export")).one()
    assert row.model_dump() == protected
    assert len(session.exec(select(Endpoint)).all()) == (2 if page_count == 3 else 3)


@pytest.mark.parametrize("kind", ["application_api", "download", "unknown"])
def test_existing_non_page_template_blocks_page_collapse(session, kind):
    scan_id = create_scan(session, "http://local.invalid")
    save_endpoints(session, scan_id, [_endpoint("/items/{slug}", kind)])
    for slug in ("alpha", "beta", "gamma"):
        save_endpoints(session, scan_id, [_endpoint(f"/items/{slug}", "page_route")])
    before = [row.model_dump() for row in session.exec(select(Endpoint)).all()]
    assert collapse_scan_endpoints(session, scan_id) == 0
    assert [row.model_dump() for row in session.exec(select(Endpoint)).all()] == before
