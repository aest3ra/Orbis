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


@pytest.mark.parametrize("stage", ["detach", "page_close"])
def test_cleanup_timeout_is_finite_and_reported(capture_resources, monkeypatch, caplog, stage):
    """A hung detach or close is abandoned after CLEANUP_TIMEOUT_SEC and reported
    on the capture; the page's evidence is returned, not lost."""
    context, page, client = capture_resources
    monkeypatch.setattr(browser, "CLEANUP_TIMEOUT_SEC", 0.01, raising=False)

    async def exercise():
        cancelled = asyncio.Event()

        async def blocked():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        operation = client.detach if stage == "detach" else page.close
        operation.side_effect = blocked
        result = await asyncio.wait_for(capture(context), timeout=0.5)
        assert cancelled.is_set()
        return result

    result = asyncio.run(exercise())
    assert result.error == "CaptureCleanupTimeout"
    assert "timed out" in caplog.text
    client.detach.assert_awaited_once_with()
    page.close.assert_awaited_once_with()


def test_detach_failure_still_closes_page(capture_resources, caplog):
    """Teardown is best effort: a failing detach is logged by type only and the
    page is still closed."""
    context, page, client = capture_resources
    client.detach.side_effect = OSError("private detach failure")

    result = asyncio.run(capture(context))

    assert result.error is None
    assert "CDP detach failed: OSError" in caplog.text
    assert "private" not in caplog.text
    page.close.assert_awaited_once_with()
