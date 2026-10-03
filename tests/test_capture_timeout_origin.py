import asyncio
import errno
import json
import os
from types import SimpleNamespace

import pytest
from playwright.async_api import FileChooser as PublicFileChooser
from playwright._impl._connection import ChannelOwner, Connection
from playwright._impl._element_handle import ElementHandle
from playwright._impl._file_chooser import FileChooser
from playwright._impl._frame import Frame
from playwright._impl._helper import TimeoutSettings

from orbis.config import InteractionConfig
from orbis.crawler import browser
from orbis.telemetry import ScanLog
from tests.test_download_evidence import download_capture, download_item


@pytest.mark.parametrize("timing", ["early", "late"])
@pytest.mark.parametrize("mode", ["normal", "operation_none", "operation_future", "driver_timeout", "deadline", "cancel"])
def test_public_filechooser_timeout_origin(download_capture, monkeypatch, timing, mode):
    fixture = download_capture
    upload = fixture.directory / "upload.txt"
    upload.write_text("local fixture")
    trace = ScanLog(str(fixture.directory / "scan.db"), 1)
    fixture.state.emit = trace.emit

    async def exercise():
        loop = asyncio.get_running_loop()
        entered, chooser_done, first_started, finish_first = [asyncio.Event() for _ in range(4)]
        transport = SimpleNamespace(on_error_future=loop.create_future())
        connection = Connection(None, lambda *args: None, transport, loop)
        frame = Frame(connection, "Frame", "frame", {"name": "", "url": fixture.page.url, "loadStates": []})
        frame._page = SimpleNamespace(
            context=ChannelOwner(connection, "BrowserContext", "context", {}),
            _timeout_settings=TimeoutSettings(None), _loop=loop, _dispatcher_fiber=None,
        )
        element = ElementHandle(frame, "ElementHandle", "input", {"preview": "input"})
        chooser = PublicFileChooser(FileChooser(frame._page, element, False))
        chooser_errors, cancellation, messages = [], [], []
        original_set_files = chooser.set_files

        async def set_files(*args, **kwargs):
            try:
                return await original_set_files(*args, **kwargs)
            except BaseException as error:
                chooser_errors.append(error)
                raise
            finally:
                chooser_done.set()

        chooser.set_files = set_files

        def send(message):
            method = message["method"]
            messages.append(method)
            if method == "ownerFrame":
                entered.set()
                if mode in {"deadline", "cancel"}:
                    return
            if method == "setInputFiles" and mode == "driver_timeout":
                response = {"error": {"error": {
                    "name": "TimeoutError", "message": "private driver timeout", "stack": "",
                }}}
            else:
                response = {"result": {"frame": {"guid": "frame"}} if method == "ownerFrame" else (
                    {"value": ""} if method == "getAttribute" else {}
                )}
            loop.call_soon(connection.dispatch, {"id": message["id"], **response})

        transport.send = send
        stat_failure = OSError(errno.ETIMEDOUT, "private filesystem timeout")
        original_stat = os.stat

        def stat(path, *args, **kwargs):
            if str(path) == str(upload) and mode in {"operation_none", "operation_future"}:
                raise stat_failure
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", stat)

        async def first_download(path):
            first_started.set()
            await finish_first.wait()
            path.write_bytes(b"first")

        async def goto(*args, **kwargs):
            if timing == "early":
                fixture.events["filechooser"](chooser)
            else:
                fixture.events["download"](download_item(first_download))

        fixture.page.goto = goto
        deadline = loop.time() + (0.03 if mode == "deadline" else 60) if mode in {
            "deadline", "operation_future",
        } else None

        async def capture():
            try:
                return await browser.capture_page(
                    fixture.context, fixture.page.url, max_scrolls=0, settle_ms=0,
                    exploration=fixture.state, download_dir=fixture.directory,
                    interaction=InteractionConfig(allow_write=True, upload_files=[upload]), deadline=deadline,
                )
            except asyncio.CancelledError as error:
                cancellation.append(error)
                raise

        task = asyncio.create_task(capture())
        try:
            if timing == "late":
                await asyncio.wait_for(first_started.wait(), timeout=1)
                await asyncio.sleep(0)
                fixture.events["filechooser"](chooser)
                await asyncio.wait_for(entered.wait(), timeout=1)
                if mode not in {"deadline", "cancel"}:
                    await asyncio.wait_for(chooser_done.wait(), timeout=1)
                    finish_first.set()
            await asyncio.wait_for(entered.wait(), timeout=1)
            if mode == "cancel":
                task.cancel("caller cancellation")
                with pytest.raises(asyncio.CancelledError) as raised:
                    await asyncio.wait_for(task, timeout=1)
                assert raised.value is cancellation[0]
                assert raised.value.args == ("caller cancellation",)
                assert task.cancelled()
            else:
                result = await asyncio.wait_for(task, timeout=1)
                assert result.error == (None if mode == "normal" else (
                    "ScanDeadlineExceeded" if mode == "deadline" else "TimeoutError"
                ))
                if mode == "operation_future":
                    assert loop.time() < deadline
                if mode in {"operation_none", "operation_future"}:
                    assert chooser_errors == [stat_failure]
                    assert type(stat_failure) is TimeoutError
                    assert "setInputFiles" not in messages
                if mode == "driver_timeout":
                    assert type(chooser_errors[0]).__module__ == "playwright._impl._errors"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for callback in connection._callbacks.values():
                callback.future.cancel()
            connection._callbacks.clear()
            connection.playwright_future.cancel()
            transport.on_error_future.cancel()
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    try:
        asyncio.run(exercise())
    finally:
        trace.close()
    rows = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert sum(row["event"] == "file_selection" for row in rows) == (mode == "normal")
    assert not list(fixture.directory.glob("*.part"))
    fixture.client.detach.assert_awaited_once()
    fixture.page.close.assert_awaited_once()
