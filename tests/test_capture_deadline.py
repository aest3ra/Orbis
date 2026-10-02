import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orbis.crawler.browser import capture_page


def capture_context():
    client = SimpleNamespace(send=AsyncMock(), on=Mock(), detach=AsyncMock())
    page = SimpleNamespace(
        url="http://localhost/",
        add_init_script=AsyncMock(),
        on=Mock(),
        close=AsyncMock(),
    )
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        new_cdp_session=AsyncMock(return_value=client),
    )
    return context, page, client


@pytest.mark.parametrize("stage", ["new_page", "init_script", "cdp_session", "network_enable"])
def test_deadline_covers_browser_setup(stage):
    context, page, client = capture_context()

    async def exercise():
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()

        operation = {
            "new_page": context.new_page,
            "init_script": page.add_init_script,
            "cdp_session": context.new_cdp_session,
            "network_enable": client.send,
        }[stage]
        operation.side_effect = blocked
        return await asyncio.wait_for(
            capture_page(context, "http://localhost/", deadline=time.monotonic() + 0.02),
            timeout=0.5,
        )

    result = asyncio.run(exercise())
    assert result.error == "ScanDeadlineExceeded"
    assert result.final_url == "http://localhost/"
    assert result.network_events == []
    assert page.close.await_count == (0 if stage == "new_page" else 1)
    assert client.detach.await_count == (1 if stage == "network_enable" else 0)


def test_setup_cancellation_propagates_after_page_cleanup():
    context, page, client = capture_context()

    async def exercise():
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        context.new_cdp_session.side_effect = blocked
        task = asyncio.create_task(capture_page(context, "http://localhost/"))
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    page.close.assert_awaited_once()
    client.detach.assert_not_awaited()


def test_page_creation_error_is_not_replaced_by_cleanup_error():
    context, page, client = capture_context()
    context.new_page.side_effect = RuntimeError("page creation failed")
    with pytest.raises(RuntimeError, match="page creation failed"):
        asyncio.run(capture_page(context, "http://localhost/"))
    page.close.assert_not_awaited()
    client.detach.assert_not_awaited()
