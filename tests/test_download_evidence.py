import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError

from orbis.crawler import browser
from orbis.crawler.interactor import ExplorationState


@pytest.fixture
def download_capture(monkeypatch, tmp_path):
    page_events, network_events, logs = {}, {}, []
    client = SimpleNamespace(
        send=AsyncMock(), detach=AsyncMock(),
        on=lambda name, callback: network_events.__setitem__(name, callback),
    )
    page = SimpleNamespace(
        url="http://localhost/", add_init_script=AsyncMock(), close=AsyncMock(),
        on=lambda name, callback: page_events.__setitem__(name, callback),
    )
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page), new_cdp_session=AsyncMock(return_value=client),
    )
    state = ExplorationState(emit=lambda name, **record: logs.append({"event": name, **record}))
    monkeypatch.setattr(browser, "_collect_dom", AsyncMock(return_value=[]))
    monkeypatch.setattr(browser, "_collect_inline_data", AsyncMock(return_value=[]))
    monkeypatch.setattr(browser, "_fill_selective_bodies", AsyncMock(return_value=([], set())))
    monkeypatch.setattr(browser, "_collect_init_script_events", AsyncMock(return_value=[]))

    def request(request_id, method, action_id):
        state.current_action = {"action_id": action_id}
        network_events["Network.requestWillBeSent"]({
            "requestId": request_id, "type": "Document",
            "request": {"method": method, "url": "http://localhost/export"},
        })

    async def capture(scenario):
        async def goto(*args, **kwargs):
            await scenario()
            state.current_action = None

        page.goto = goto
        return await asyncio.wait_for(browser.capture_page(
            context, page.url, max_scrolls=0, settle_ms=0, exploration=state,
            download_dir=tmp_path,
        ), timeout=1)

    return SimpleNamespace(capture=capture, request=request, events=page_events,
                           state=state, logs=logs, directory=tmp_path, page=page)


def download_item(save_as):
    return SimpleNamespace(url="http://localhost/export", save_as=save_as, cancel=AsyncMock())


def test_download_method_is_bound_before_later_same_url_request(download_capture):
    fixture = download_capture

    async def exercise():
        saving, finish = asyncio.Event(), asyncio.Event()

        async def save_as(path):
            saving.set()
            await finish.wait()
            path.write_bytes(b"report")

        async def scenario():
            fixture.request("post", "POST", 1)
            fixture.events["download"](download_item(save_as))
            await saving.wait()
            fixture.request("later-get", "GET", 2)
            finish.set()

        return await fixture.capture(scenario)

    result = asyncio.run(exercise())
    record = result.observations[0]
    assert record["action_id"] == 1
    assert record["method"] == "POST"
    assert record["method_evidence"] == "network_request"
    assert [(event.request_id, event.resource_type) for event in result.network_events] == [
        ("post", "Download"), ("later-get", "Document"),
    ]


def test_reverse_completion_keeps_each_downloads_request_evidence(download_capture):
    fixture = download_capture

    async def exercise():
        started, second_saved = asyncio.Event(), asyncio.Event()

        async def first_save(path):
            started.set()
            await second_saved.wait()
            path.write_bytes(b"first")

        async def second_save(path):
            path.write_bytes(b"second")
            second_saved.set()

        async def scenario():
            fixture.request("post", "POST", 1)
            fixture.events["download"](download_item(first_save))
            await started.wait()
            fixture.request("get", "GET", 2)
            fixture.events["download"](download_item(second_save))

        return await fixture.capture(scenario)

    result = asyncio.run(exercise())
    assert [(record["action_id"], record["method"]) for record in result.observations] == [(1, "POST"), (2, "GET")]
    assert [record["action_id"] for record in fixture.logs if record["event"] == "download_end"] == [2, 1]
    assert [event.resource_type for event in result.network_events] == ["Download", "Download"]


@pytest.mark.parametrize("anchor", [False, True])
def test_ambiguous_request_does_not_invent_method_or_anchor_evidence(download_capture, anchor):
    fixture = download_capture

    async def save_as(path):
        path.write_bytes(b"report")

    async def scenario():
        fixture.request("post", "POST", 1)
        fixture.request("get", "GET", 1)
        if anchor:
            fixture.state.current_action.update(kind="link", nav_url="http://localhost/export")
        fixture.events["download"](download_item(save_as))

    result = asyncio.run(fixture.capture(scenario))
    record = result.observations[0]
    assert record["status"] == "saved"
    assert "method" not in record
    assert "method_evidence" not in record
    assert [event.resource_type for event in result.network_events] == ["Document", "Document"]


def test_unique_same_action_request_wins_over_other_action(download_capture):
    fixture = download_capture

    async def save_as(path):
        path.write_bytes(b"report")

    async def scenario():
        fixture.request("post", "POST", 1)
        fixture.request("get", "GET", 2)
        fixture.state.current_action = {"action_id": 1}
        fixture.events["download"](download_item(save_as))

    result = asyncio.run(fixture.capture(scenario))
    assert result.observations[0]["method"] == "POST"
    assert result.network_events[0].resource_type == "Download"
    assert result.network_events[1].resource_type == "Document"


@pytest.mark.parametrize("action_id", [None, 2])
def test_other_action_request_is_not_download_method_evidence(download_capture, action_id):
    fixture = download_capture

    async def save_as(path):
        path.write_bytes(b"report")

    async def scenario():
        fixture.request("previous", "POST", 1)
        fixture.state.current_action = {
            "action_id": action_id, "kind": "link", "nav_url": "http://localhost/export",
        }
        fixture.events["download"](download_item(save_as))

    result = asyncio.run(fixture.capture(scenario))
    record = result.observations[0]
    assert record["status"] == "saved"
    assert "method" not in record and "method_evidence" not in record
    assert len(result.network_events) == 1
    assert result.network_events[0].resource_type == "Document"


def test_anchor_without_cdp_request_preserves_explicit_navigation_evidence(download_capture):
    fixture = download_capture

    async def save_as(path):
        path.write_bytes(b"report")

    async def scenario():
        fixture.state.current_action = {"action_id": 1, "kind": "link", "nav_url": "http://localhost/export"}
        fixture.events["download"](download_item(save_as))

    result = asyncio.run(fixture.capture(scenario))
    assert result.observations[0]["method_evidence"] == "anchor_navigation"
    assert [(event.method, event.source, event.status) for event in result.network_events] == [
        ("GET", "download_link", None),
    ]


@pytest.mark.parametrize("failure_type", [PlaywrightError, OSError])
def test_partial_file_is_removed_after_save_failure(download_capture, failure_type):
    fixture = download_capture
    completed = fixture.directory / "existing.bin"
    completed.write_bytes(b"previous download")

    async def save_as(path):
        path.write_bytes(b"incomplete")
        raise failure_type("private transfer failure")

    async def scenario():
        fixture.events["download"](download_item(save_as))

    result = asyncio.run(fixture.capture(scenario))
    record = result.observations[0]
    assert record["status"] == "failed"
    assert record["error"] == failure_type.__name__
    assert "sha256" not in record and "path" not in record
    assert fixture.state.download_counts == {}
    assert list(fixture.directory.iterdir()) == [completed]
    assert completed.read_bytes() == b"previous download"
    assert fixture.logs[-1]["event"] == "download_end"


def test_partial_file_is_removed_after_cancellation(download_capture):
    fixture = download_capture

    async def exercise():
        saving = asyncio.Event()

        async def save_as(path):
            path.write_bytes(b"incomplete")
            saving.set()
            await asyncio.Event().wait()

        async def scenario():
            fixture.events["download"](download_item(save_as))

        task = asyncio.create_task(fixture.capture(scenario))
        await saving.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert list(fixture.directory.iterdir()) == []
    assert fixture.state.download_counts == {}
    assert fixture.logs[-1]["event"] == "download_end"
    assert fixture.logs[-1]["status"] == "interrupted"
    fixture.page.close.assert_awaited_once()


def test_partial_removal_failure_is_diagnosed_without_replacing_transfer_failure(download_capture, monkeypatch):
    fixture = download_capture
    original_unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path.suffix == ".part":
            raise PermissionError("private cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)

    async def save_as(path):
        path.write_bytes(b"incomplete")
        raise PlaywrightError("private transfer failure")

    async def scenario():
        fixture.events["download"](download_item(save_as))

    result = asyncio.run(fixture.capture(scenario))
    record = result.observations[0]
    assert record["status"] == "failed" and record["error"] == "Error"
    assert record["cleanup_error"] == "PermissionError"
    assert Path(record["partial_path"]).read_bytes() == b"incomplete"
    assert "private cleanup failure" not in str(record)
    assert "private transfer failure" not in str(record)
    assert not list(fixture.directory.glob("*.bin"))


@pytest.mark.parametrize("operation", ["read_bytes", "replace"])
def test_verification_or_commit_failure_does_not_publish_completed_artifact(download_capture, monkeypatch, operation):
    fixture = download_capture
    original = getattr(Path, operation)

    def fail(path, *args, **kwargs):
        if path.suffix == ".part":
            raise OSError("private artifact failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, operation, fail)

    async def save_as(path):
        path.write_bytes(b"report")

    async def scenario():
        fixture.events["download"](download_item(save_as))

    result = asyncio.run(fixture.capture(scenario))
    record = result.observations[0]
    assert record["status"] == "failed" and record["error"] == "OSError"
    assert "sha256" not in record and "path" not in record
    assert fixture.state.download_counts == {}
    assert list(fixture.directory.iterdir()) == []


def test_completed_artifacts_have_exact_hash_size_and_independent_duplicate_counts(download_capture):
    fixture = download_capture
    payload = b"report\x00\xff"

    async def save_as(path):
        assert path.suffix == ".part"
        assert list(fixture.directory.glob("*.bin")) == []
        path.write_bytes(payload)

    async def scenario():
        fixture.events["download"](download_item(save_as))

    first = asyncio.run(fixture.capture(scenario)).observations[0]

    async def save_again(path):
        assert path.suffix == ".part"
        path.write_bytes(payload)

    async def again():
        fixture.events["download"](download_item(save_again))

    second = asyncio.run(fixture.capture(again)).observations[0]
    assert first["path"] != second["path"]
    assert first["content_seen_count"] == 1 and second["content_seen_count"] == 2
    for record in (first, second):
        assert record["status"] == "saved"
        assert record["sha256"] == hashlib.sha256(payload).hexdigest()
        assert record["size"] == len(payload)
        assert Path(record["path"]).read_bytes() == payload
    assert list(fixture.directory.glob("*.part")) == []
