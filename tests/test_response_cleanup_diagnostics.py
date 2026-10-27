import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import Session, select

from orbis.config import ScanConfig
from orbis.crawler import runner
from orbis.crawler.scope import Scope
from orbis.storage.db import Endpoint, Observation, open_db
from orbis.storage.repo import create_scan


class TargetClosedError(Exception):
    pass


class Response:
    def __init__(self, mode, code):
        self.mode = mode
        self.code = code
        self.status_reads = 0
        self.dispose_calls = 0
        self.dispose_entered = asyncio.Event()

    @property
    def status(self):
        self.status_reads += 1
        if self.mode == "status_error":
            raise ValueError("private status error")
        return self.code

    async def dispose(self):
        self.dispose_calls += 1
        self.dispose_entered.set()
        if isinstance(self.mode, BaseException):
            raise self.mode
        if self.mode in {"cleanup_error", "status_error"}:
            raise OSError("private cleanup error")
        if self.mode == "context_closed":
            raise TargetClosedError("private context error")
        if self.mode in {"deadline", "cancelled", "endpoint_timeout"}:
            await asyncio.Event().wait()


class Request:
    def __init__(self):
        self.responses = [Response("cleanup_error", 200), Response("normal", 204)]
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[len(self.calls) - 1]


@pytest.fixture
def resources(tmp_path, monkeypatch):
    database = str(tmp_path / "scan.db")
    engine = open_db(database)
    with Session(engine) as session:
        scan_id = create_scan(session, "http://localhost/")
        for name in ("alpha", "beta"):
            session.add(Endpoint(
                scan_id=scan_id, method="GET", host="localhost", path_template=f"/api/{name}",
                sample_url=f"http://localhost/api/{name}", route_kind="api",
                source="static_js", probe_status="unprobed",
            ))
        session.commit()
    request = Request()
    context = SimpleNamespace(request=request)
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
                                 stop=AsyncMock())
    monkeypatch.setattr(runner, "async_playwright", Mock(return_value=SimpleNamespace(
        start=AsyncMock(return_value=playwright),
    )))
    config = ScanConfig(target="http://localhost/")
    config.limits.max_pages = 0
    config.limits.max_duration_sec = 5
    config.limits.rate_limit_rps = 0
    trace = SimpleNamespace(emit=Mock(side_effect=lambda event, **fields: fields))
    yield SimpleNamespace(engine=engine, scan_id=scan_id, database=database, config=config,
                          request=request, browser=browser, playwright=playwright, trace=trace)
    engine.dispose()


async def crawl(resources):
    return await runner._crawl_scan(
        resources.config, resources.engine, resources.scan_id, Scope(resources.config.scope), None, None,
        db_path=resources.database, headless=True, wayback=False, trace=resources.trace,
    )


def stored(resources):
    with Session(resources.engine) as session:
        endpoints = session.exec(select(Endpoint).order_by(Endpoint.id)).all()
        observations = session.exec(select(Observation).where(Observation.kind == "probe_response_cleanup")).all()
        return ([(row.probe_status, row.probe_code) for row in endpoints],
                [json.loads(row.details_json) for row in observations],
                [row.page_url for row in observations])


def test_normal_responses_add_no_diagnostic_writes(resources, monkeypatch):
    resources.request.responses[0].mode = "normal"
    original_commit = Session.commit
    commits = []

    def count_commit(session):
        commits.append(1)
        original_commit(session)

    monkeypatch.setattr(Session, "commit", count_commit)
    asyncio.run(crawl(resources))

    rows, observations, urls = stored(resources)
    assert rows == [("responded", 200), ("responded", 204)]
    assert observations == urls == []
    assert len(commits) == 2
    assert [response.status_reads for response in resources.request.responses] == [1, 1]


def test_failed_status_read_does_not_fabricate_raw_status(resources):
    resources.request.responses[0].mode = "status_error"

    asyncio.run(crawl(resources))

    rows, observations, urls = stored(resources)
    assert rows == [("no_response", None), ("responded", 204)]
    assert observations == urls == []
    assert len(resources.request.calls) == 2
