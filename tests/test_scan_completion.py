import asyncio
import json

import pytest
from sqlmodel import Session, select

from orbis.config import ScanConfig
from orbis.crawler import runner
from orbis.storage.db import Endpoint, Observation, Scan, open_db
from orbis.storage.repo import create_scan


@pytest.mark.parametrize("failure_type", [None, RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("with_progress", [False, True])
def test_terminal_record_matches_persisted_scan(tmp_path, monkeypatch, failure_type, with_progress):
    failure = failure_type("synthetic scan stop") if failure_type else None
    database = tmp_path / "scan.db"

    async def crawl(config, engine, scan_id, *args, trace, **kwargs):
        with Session(engine) as session:
            other_id = create_scan(session, "http://localhost/other")
            session.add(Endpoint(scan_id=other_id, method="GET", host="localhost", path_template="/other",
                                 sample_url="http://localhost/other", route_kind="application_api"))
            if with_progress:
                for path in ("/api/one", "/api/two"):
                    session.add(Endpoint(scan_id=scan_id, method="GET", host="localhost", path_template=path,
                                         sample_url=f"http://localhost{path}", route_kind="application_api"))
            session.commit()
        if with_progress:
            for page_id, status in enumerate(("completed", "partial", "failed", "interrupted"), 1):
                trace.emit("page_start", page_id=page_id)
                trace.emit("page_end", page_id=page_id, status=status)
        if failure:
            raise failure
        return {"stop_reason": "max_pages", "pages": 3 if with_progress else 0, "endpoints": 2 if with_progress else 0}

    monkeypatch.setattr(runner, "_crawl_scan", crawl)
    operation = runner.run_scan(ScanConfig(target="http://localhost/"), db_path=str(database), wayback=False)
    if failure:
        with pytest.raises(failure_type) as raised:
            asyncio.run(operation)
        assert raised.value is failure
    else:
        assert asyncio.run(operation) == 1

    records = [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text().splitlines()]
    terminal = records[-1]
    assert terminal["event"] == "scan_end"
    expected_status = "completed" if failure is None else "cancelled" if failure_type is asyncio.CancelledError else "error"
    assert terminal["status"] == expected_status
    engine = open_db(database)
    try:
        with Session(engine) as session:
            scan = session.get(Scan, 1)
            assert scan.finished_at is not None
            assert scan.pages_crawled == terminal["pages"] == (3 if with_progress else 0)
            assert scan.endpoints_found == terminal["endpoints"] == (2 if with_progress else 0)
            observation = session.exec(select(Observation).where(Observation.scan_id == 1)).one()
            assert json.loads(observation.details_json) == terminal
            assert session.get(Scan, 2).finished_at is None
    finally:
        engine.dispose()
