import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError

from orbis.crawler import browser
from orbis.crawler.interactor import ExplorationState


@pytest.fixture
def capture_resources(monkeypatch):
    client = SimpleNamespace(send=AsyncMock(), on=Mock(), detach=AsyncMock())
    page = SimpleNamespace(
        url="http://localhost/",
        add_init_script=AsyncMock(),
        on=Mock(),
        goto=AsyncMock(),
        close=AsyncMock(),
    )
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        new_cdp_session=AsyncMock(return_value=client),
    )
    monkeypatch.setattr(browser, "_collect_dom", AsyncMock(return_value=[]))
    monkeypatch.setattr(browser, "_collect_inline_data", AsyncMock(return_value=[]))
    monkeypatch.setattr(browser, "_fill_selective_bodies", AsyncMock(return_value=([], set())))
    monkeypatch.setattr(browser, "_collect_init_script_events", AsyncMock(return_value=[]))
    return context, page, client


async def capture(context, **kwargs):
    return await browser.capture_page(context, "http://localhost/", max_scrolls=0, settle_ms=0, **kwargs)


def pending_request(client):
    def register(name, callback):
        if name == "Network.requestWillBeSent":
            callback({"requestId": "synthetic", "type": "Fetch", "request": {
                "method": "GET", "url": "http://localhost/data",
            }})
    client.on.side_effect = register


@pytest.mark.parametrize("primary", [None, RuntimeError("private primary"), asyncio.CancelledError()])
def test_final_telemetry_failure_releases_resources_and_preserves_primary(capture_resources, primary):
    context, page, client = capture_resources
    pending_request(client)
    page.goto.side_effect = primary
    failure = OSError("private telemetry failure")
    state = ExplorationState(emit=Mock(side_effect=failure))
    expected = primary if primary is not None else failure

    with pytest.raises(type(expected)) as raised:
        asyncio.run(capture(context, exploration=state))

    assert raised.value is expected
    client.detach.assert_awaited_once_with()
    page.close.assert_awaited_once_with()
    if primary is not None:
        assert any("OSError" in note for note in primary.__notes__)
        assert "private" not in " ".join(primary.__notes__)


def test_detach_failure_still_closes_page(capture_resources):
    context, page, client = capture_resources
    failure = OSError("private detach failure")
    client.detach.side_effect = failure

    with pytest.raises(OSError) as raised:
        asyncio.run(capture(context))

    assert raised.value is failure
    page.close.assert_awaited_once_with()


@pytest.mark.parametrize("stage", ["detach", "page_close"])
@pytest.mark.parametrize("primary", [RuntimeError("private primary"), asyncio.CancelledError()])
def test_cleanup_failure_preserves_primary(capture_resources, stage, primary):
    context, page, client = capture_resources
    page.goto.side_effect = primary
    operation = client.detach if stage == "detach" else page.close
    operation.side_effect = OSError("private cleanup failure")

    with pytest.raises(type(primary)) as raised:
        asyncio.run(capture(context))

    assert raised.value is primary
    client.detach.assert_awaited_once_with()
    page.close.assert_awaited_once_with()
    assert any("OSError" in note for note in primary.__notes__)
    assert "private" not in " ".join(primary.__notes__)


def test_second_cleanup_failure_preserves_first(capture_resources):
    context, page, client = capture_resources
    failure = OSError("private detach failure")
    client.detach.side_effect = failure
    page.close.side_effect = RuntimeError("private close failure")

    with pytest.raises(OSError) as raised:
        asyncio.run(capture(context))

    assert raised.value is failure
    page.close.assert_awaited_once_with()
    assert any("RuntimeError" in note for note in failure.__notes__)
    assert "private" not in " ".join(failure.__notes__)


@pytest.mark.parametrize("stage", ["detach", "page_close"])
@pytest.mark.parametrize("primary_type", [None, RuntimeError, asyncio.CancelledError])
def test_cleanup_timeout_is_finite_and_reported(capture_resources, monkeypatch, caplog, stage, primary_type):
    context, page, client = capture_resources
    monkeypatch.setattr(browser, "CLEANUP_TIMEOUT_SEC", 0.01, raising=False)
    primary = primary_type("private primary") if primary_type is not None else None
    page.goto.side_effect = primary

    async def exercise():
        cancelled = asyncio.Event()

        async def blocked():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        operation = client.detach if stage == "detach" else page.close
        operation.side_effect = blocked
        if primary is None:
            result = await asyncio.wait_for(capture(context), timeout=0.5)
        else:
            with pytest.raises(type(primary)) as raised:
                await asyncio.wait_for(capture(context), timeout=0.5)
            assert raised.value is primary
            result = None
        assert cancelled.is_set()
        return result

    result = asyncio.run(exercise())
    if primary is None:
        assert result.error == "CaptureCleanupTimeout"
        assert "TimeoutError" in caplog.text
    else:
        assert any("TimeoutError" in note for note in primary.__notes__)
        assert "private" not in " ".join(primary.__notes__)
    client.detach.assert_awaited_once_with()
    page.close.assert_awaited_once_with()


@pytest.mark.parametrize("primary_type", [None, RuntimeError])
@pytest.mark.parametrize("close_fails", [False, True])
def test_fresh_cancellation_during_detach_propagates_and_closes_page(capture_resources, primary_type, close_fails):
    context, page, client = capture_resources
    if primary_type is not None:
        page.goto.side_effect = primary_type("private primary")
    if close_fails:
        page.close.side_effect = OSError("private close failure")

    async def exercise():
        entered = asyncio.Event()

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        client.detach.side_effect = blocked
        task = asyncio.create_task(capture(context))
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(task, timeout=0.5)
        if close_fails:
            assert any("OSError" in note for note in raised.value.__notes__)
            assert "private" not in " ".join(raised.value.__notes__)

    asyncio.run(exercise())
    page.close.assert_awaited_once_with()


def test_cleanup_timeout_keeps_existing_scan_deadline_result(capture_resources, monkeypatch):
    context, page, client = capture_resources
    monkeypatch.setattr(browser, "CLEANUP_TIMEOUT_SEC", 0.01, raising=False)

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    async def exercise():
        page.goto.side_effect = blocked
        client.detach.side_effect = blocked
        deadline = asyncio.get_running_loop().time() + 0.01
        return await asyncio.wait_for(capture(context, deadline=deadline), timeout=0.5)

    result = asyncio.run(exercise())
    assert result.error == "ScanDeadlineExceeded"
    page.close.assert_awaited_once_with()


def test_single_capture_cancellation_survives_cleanup_errors(capture_resources):
    context, page, client = capture_resources
    client.detach.side_effect = OSError("private detach failure")
    page.close.side_effect = RuntimeError("private close failure")

    async def exercise():
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        page.goto.side_effect = blocked
        task = asyncio.create_task(capture(context))
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(task, timeout=0.5)
        assert {note.rsplit(": ", 1)[-1] for note in raised.value.__notes__} == {"OSError", "RuntimeError"}
        assert "private" not in " ".join(raised.value.__notes__)

    asyncio.run(exercise())
    client.detach.assert_awaited_once_with()
    page.close.assert_awaited_once_with()


def test_callers_handled_exception_does_not_hide_cleanup_failure(capture_resources):
    context, page, client = capture_resources
    failure = OSError("private detach failure")
    client.detach.side_effect = failure

    async def exercise():
        try:
            raise RuntimeError("unrelated caller error")
        except RuntimeError:
            with pytest.raises(OSError) as raised:
                await capture(context)
            assert raised.value is failure

    asyncio.run(exercise())
    page.close.assert_awaited_once_with()


def test_expected_playwright_cleanup_failures_only_log_types(capture_resources, caplog):
    context, page, client = capture_resources
    client.detach.side_effect = PlaywrightError("private detach failure")
    page.close.side_effect = PlaywrightError("private close failure")

    result = asyncio.run(capture(context))

    assert result.error is None
    assert "Error" in caplog.text
    assert "private" not in caplog.text
    client.detach.assert_awaited_once_with()
    page.close.assert_awaited_once_with()
