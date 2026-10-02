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
                           state=state, logs=logs, directory=tmp_path, page=page, context=context, client=client)


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


def test_download_end_logger_failure_preserves_direct_capture_cancellation(download_capture):
    fixture = download_capture

    def emit(name, **record):
        fixture.logs.append({"event": name, **record})
        if name == "download_end":
            raise OSError("private logger failure")

    fixture.state.emit = emit

    async def exercise():
        saving = asyncio.Event()

        async def save_as(path):
            path.write_bytes(b"incomplete")
            saving.set()
            await asyncio.Event().wait()

        async def goto(*args, **kwargs):
            fixture.events["download"](download_item(save_as))

        fixture.page.goto = goto
        task = asyncio.create_task(browser.capture_page(
            fixture.context, fixture.page.url, max_scrolls=0, settle_ms=0,
            exploration=fixture.state, download_dir=fixture.directory,
        ))
        await asyncio.wait_for(saving.wait(), timeout=1)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert task.cancelled()
        assert raised.value.__notes__ == ["Download finalization failed: OSError"]

    asyncio.run(exercise())
    assert list(fixture.directory.iterdir()) == []
    assert fixture.state.download_counts == {}
    assert fixture.logs[-1]["status"] == "interrupted"
    fixture.page.close.assert_awaited_once()


@pytest.mark.parametrize("transfer_failure", [None, PlaywrightError("private transfer failure")])
def test_download_end_logger_failure_without_propagating_primary_still_raises(download_capture, transfer_failure):
    fixture = download_capture
    logger_failure = OSError("private logger failure")

    def emit(name, **record):
        fixture.logs.append({"event": name, **record})
        if name == "download_end":
            raise logger_failure

    fixture.state.emit = emit

    async def save_as(path):
        path.write_bytes(b"report")
        if transfer_failure is not None:
            raise transfer_failure

    async def scenario():
        fixture.events["download"](download_item(save_as))

    with pytest.raises(OSError) as raised:
        asyncio.run(fixture.capture(scenario))

    assert raised.value is logger_failure
    assert fixture.logs[-1]["status"] == ("saved" if transfer_failure is None else "failed")
    assert len(list(fixture.directory.glob("*.bin"))) == (1 if transfer_failure is None else 0)
    assert not list(fixture.directory.glob("*.part"))
    fixture.page.close.assert_awaited_once()


def test_download_end_logger_failure_preserves_unexpected_transfer_failure(download_capture):
    fixture = download_capture
    transfer_failure = RuntimeError("private transfer failure")

    def emit(name, **record):
        fixture.logs.append({"event": name, **record})
        if name == "download_end":
            raise OSError("private logger failure")

    fixture.state.emit = emit

    async def save_as(path):
        path.write_bytes(b"incomplete")
        raise transfer_failure

    async def scenario():
        fixture.events["download"](download_item(save_as))

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(fixture.capture(scenario))

    assert raised.value is transfer_failure
    assert raised.value.__notes__ == ["Download finalization failed: OSError"]
    assert list(fixture.directory.iterdir()) == []
    assert fixture.logs[-1]["status"] == "failed"
    fixture.page.close.assert_awaited_once()


@pytest.mark.parametrize("stage", ["detach", "page_close"])
@pytest.mark.parametrize("event_name", ["download", "filechooser"])
def test_cleanup_does_not_start_new_download_or_filechooser_tasks(download_capture, stage, event_name):
    fixture = download_capture
    calls = []

    async def blocked(*args):
        calls.append(event_name)
        if event_name == "download":
            args[0].write_bytes(b"incomplete")
        await asyncio.Event().wait()

    incoming = download_item(blocked) if event_name == "download" else SimpleNamespace(set_files=blocked)

    async def during_cleanup():
        fixture.events[event_name](incoming)
        await asyncio.sleep(0)

    operation = fixture.client.detach if stage == "detach" else fixture.page.close
    operation.side_effect = during_cleanup

    async def scenario():
        pass

    async def exercise():
        result = await fixture.capture(scenario)
        pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        statuses_at_return = [record.get("status") for record in result.observations]
        files_at_return = list(fixture.directory.iterdir())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        assert pending == []
        assert statuses_at_return == []
        assert files_at_return == []

    asyncio.run(exercise())
    assert calls == []
    assert fixture.logs == []
    fixture.client.detach.assert_awaited_once()
    fixture.page.close.assert_awaited_once()


@pytest.mark.parametrize("primary_type", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("event_name", ["download", "filechooser"])
def test_existing_page_tasks_are_drained_when_capture_fails(download_capture, primary_type, event_name):
    fixture = download_capture
    primary = primary_type("primary capture failure")

    async def exercise():
        entered, exited = asyncio.Event(), asyncio.Event()

        async def blocked(*args):
            if event_name == "download":
                args[0].write_bytes(b"incomplete")
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()

        incoming = download_item(blocked) if event_name == "download" else SimpleNamespace(set_files=blocked)

        async def scenario():
            fixture.events[event_name](incoming)
            await entered.wait()
            raise primary

        with pytest.raises(primary_type) as raised:
            await fixture.capture(scenario)
        assert raised.value is primary
        assert exited.is_set()
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(exercise())
    assert list(fixture.directory.iterdir()) == []
    assert fixture.state.download_counts == {}
    if event_name == "download":
        assert fixture.logs[-1]["status"] == "interrupted"
    fixture.client.detach.assert_awaited_once()
    fixture.page.close.assert_awaited_once()


@pytest.mark.parametrize("late_event", ["filechooser_failure", "download_log_failure", "cancel_log_failure", "plain_cancellation"])
def test_page_task_created_during_gather_does_not_hide_failure(download_capture, late_event):
    fixture = download_capture
    failure = PlaywrightError("private file selection failure") if late_event == "filechooser_failure" else OSError("private logger failure")

    async def exercise():
        first_started, finish_first, second_started, second_stopped = [asyncio.Event() for _ in range(4)]

        async def save_first(path):
            first_started.set()
            await finish_first.wait()
            path.write_bytes(b"first")

        first = download_item(save_first)
        first.url = "http://localhost/first"

        def emit(name, **record):
            fixture.logs.append({"event": name, **record})
            if late_event in {"download_log_failure", "cancel_log_failure"} and name == "download_end" and record["url"].endswith("/second"):
                raise failure

        fixture.state.emit = emit

        async def set_files(files):
            second_started.set()
            raise failure

        async def save_second(path):
            path.write_bytes(b"second")
            second_started.set()
            if late_event in {"plain_cancellation", "cancel_log_failure"}:
                try:
                    await asyncio.Event().wait()
                finally:
                    second_stopped.set()

        second = download_item(save_second)
        second.url = "http://localhost/second"

        async def scenario():
            fixture.events["download"](first)

        task = asyncio.create_task(fixture.capture(scenario))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await asyncio.sleep(0)
        if late_event == "filechooser_failure":
            fixture.events["filechooser"](SimpleNamespace(set_files=set_files))
        else:
            fixture.events["download"](second)
        await asyncio.wait_for(second_started.wait(), timeout=1)
        finish_first.set()
        if late_event in {"plain_cancellation", "filechooser_failure"}:
            result = await task
            assert result.error == ("Error" if late_event == "filechooser_failure" else None)
            assert not [record for record in result.observations if record.get("kind") == "capture_cleanup"]
            if late_event == "plain_cancellation":
                assert second_stopped.is_set()
        else:
            with pytest.raises(type(failure)) as raised:
                await task
            assert raised.value is failure
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(exercise())
    assert not list(fixture.directory.glob("*.part"))
    fixture.client.detach.assert_awaited_once()
    fixture.page.close.assert_awaited_once()


@pytest.mark.parametrize("stop", ["cancellation", "deadline"])
@pytest.mark.parametrize("logger_fails", [False, True])
def test_download_cancellation_diagnostics_survive_capture_stopping_before_gather(download_capture, stop, logger_fails):
    fixture = download_capture

    def emit(name, **record):
        fixture.logs.append({"event": name, **record})
        if logger_fails and name == "download_end":
            raise OSError("private logger failure")

    fixture.state.emit = emit

    async def exercise():
        started = asyncio.Event()

        async def save_as(path):
            path.write_bytes(b"incomplete")
            started.set()
            await asyncio.Event().wait()

        async def goto(*args, **kwargs):
            fixture.events["download"](download_item(save_as))
            await asyncio.Event().wait()

        fixture.page.goto = goto
        deadline = asyncio.get_running_loop().time() + 0.03 if stop == "deadline" else None
        task = asyncio.create_task(browser.capture_page(
            fixture.context, fixture.page.url, max_scrolls=0, settle_ms=0,
            exploration=fixture.state, download_dir=fixture.directory, deadline=deadline,
        ))
        await asyncio.wait_for(started.wait(), timeout=1)
        if stop == "cancellation":
            task.cancel("caller cancellation")
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
            assert raised.value.args == ("caller cancellation",)
            notes = getattr(raised.value, "__notes__", [])
            assert notes == (["Download finalization failed: OSError"] if logger_fails else [])
            assert task.cancelled()
        else:
            result = await task
            assert result.error == "ScanDeadlineExceeded"
            cleanup = [record for record in result.observations if record.get("kind") == "capture_cleanup"]
            assert cleanup == ([{"kind": "capture_cleanup", "errors": ["OSError"]}] if logger_fails else [])
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(exercise())
    assert list(fixture.directory.iterdir()) == []
    fixture.client.detach.assert_awaited_once()
    fixture.page.close.assert_awaited_once()


@pytest.mark.parametrize("stop", ["cancellation", "deadline"])
def test_completed_filechooser_error_is_secondary_to_capture_stop(download_capture, stop):
    fixture = download_capture
    failure = PlaywrightError("private file selection failure")
    failure.add_note("private child note")

    async def exercise():
        failed = asyncio.Event()

        async def set_files(files):
            failed.set()
            raise failure

        async def goto(*args, **kwargs):
            fixture.events["filechooser"](SimpleNamespace(set_files=set_files))
            await asyncio.Event().wait()

        fixture.page.goto = goto
        deadline = asyncio.get_running_loop().time() + 0.03 if stop == "deadline" else None
        task = asyncio.create_task(browser.capture_page(
            fixture.context, fixture.page.url, max_scrolls=0, settle_ms=0,
            exploration=fixture.state, download_dir=fixture.directory, deadline=deadline,
        ))
        await asyncio.wait_for(failed.wait(), timeout=1)
        if stop == "cancellation":
            task.cancel("caller cancellation")
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
            assert raised.value.args == ("caller cancellation",)
            assert raised.value.__notes__ == ["Page task failed: Error"]
            assert task.cancelled()
        else:
            result = await task
            assert result.error == "ScanDeadlineExceeded"
            cleanup = [record for record in result.observations if record.get("kind") == "capture_cleanup"]
            assert cleanup == [{"kind": "capture_cleanup", "errors": ["Error"]}]
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(exercise())
    fixture.client.detach.assert_awaited_once()
    fixture.page.close.assert_awaited_once()


def test_already_handled_page_task_error_is_not_reported_twice(download_capture):
    fixture = download_capture

    async def set_files(files):
        raise PlaywrightError("private file selection failure")

    async def scenario():
        fixture.events["filechooser"](SimpleNamespace(set_files=set_files))

    result = asyncio.run(fixture.capture(scenario))
    assert result.error == "Error"
    assert not [record for record in result.observations if record.get("kind") == "capture_cleanup"]
    fixture.client.detach.assert_awaited_once()
    fixture.page.close.assert_awaited_once()


def test_all_page_tasks_are_drained_before_first_child_failure_propagates(download_capture):
    fixture = download_capture
    first_failure = OSError("private first child failure")
    second_failure = PlaywrightError("private second child failure")
    second_failure.add_note("private child note")

    async def exercise():
        first_started, finish_first, stopped = [asyncio.Event() for _ in range(3)]
        entered = [asyncio.Event() for _ in range(3)]

        async def save_as(path):
            first_started.set()
            await finish_first.wait()
            path.write_bytes(b"first")

        async def scenario():
            fixture.events["download"](download_item(save_as))

        task = asyncio.create_task(fixture.capture(scenario))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await asyncio.sleep(0)
        for index, failure in enumerate((first_failure, second_failure, None)):
            async def set_files(files, index=index, failure=failure):
                entered[index].set()
                if failure is not None:
                    raise failure
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

            fixture.events["filechooser"](SimpleNamespace(set_files=set_files))
        for event in entered:
            await asyncio.wait_for(event.wait(), timeout=1)
        finish_first.set()
        with pytest.raises(OSError) as raised:
            await task
        assert raised.value is first_failure
        assert raised.value.__notes__ == ["Page task failed: Error"]
        assert stopped.is_set()
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(exercise())
    fixture.client.detach.assert_awaited_once()
    fixture.page.close.assert_awaited_once()


@pytest.mark.parametrize("timing", ["early", "late"])
@pytest.mark.parametrize("failure_type", [PlaywrightError, TimeoutError, OSError])
@pytest.mark.parametrize("navigation_failed", [False, True])
def test_page_task_failure_policy_preserves_same_observations_regardless_of_timing(download_capture, timing, failure_type, navigation_failed):
    fixture = download_capture
    failure = failure_type("private file selection failure")

    async def exercise():
        started, finish, child_failed = [asyncio.Event() for _ in range(3)]

        async def save_as(path):
            started.set()
            if timing == "late":
                await finish.wait()
            path.write_bytes(b"completed download")

        async def set_files(files):
            child_failed.set()
            raise failure

        chooser = SimpleNamespace(set_files=set_files)

        async def scenario():
            fixture.request("observed", "GET", None)
            fixture.events["download"](download_item(save_as))
            if timing == "early":
                fixture.events["filechooser"](chooser)
            if navigation_failed:
                fixture.state.current_action = None
                raise PlaywrightError("private navigation failure")

        task = asyncio.create_task(fixture.capture(scenario))
        await asyncio.wait_for(started.wait(), timeout=1)
        if timing == "late":
            await asyncio.sleep(0)
            fixture.events["filechooser"](chooser)
            await asyncio.wait_for(child_failed.wait(), timeout=1)
            finish.set()
        if failure_type is OSError:
            with pytest.raises(OSError) as raised:
                await task
            assert raised.value is failure
        else:
            result = await task
            assert result.error == ("ScanDeadlineExceeded" if failure_type is TimeoutError else "Error")
            assert [(event.request_id, event.method, event.url) for event in result.network_events] == [
                ("observed", "GET", "http://localhost/export"),
            ]
            assert len(result.observations) == 1
            assert result.observations[0]["status"] == "saved"
            assert Path(result.observations[0]["path"]).read_bytes() == b"completed download"
            assert len([record for record in fixture.logs if record["event"] == "request_observed"]) == 1
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(exercise())
    assert len(list(fixture.directory.glob("*.bin"))) == 1
    assert not list(fixture.directory.glob("*.part"))
    fixture.client.detach.assert_awaited_once()
    fixture.page.close.assert_awaited_once()
