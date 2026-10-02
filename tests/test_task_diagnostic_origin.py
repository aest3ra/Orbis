import asyncio
import errno
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError

from orbis.crawler import browser
from orbis.telemetry import ScanLog
from tests.test_download_evidence import download_capture, download_item


@pytest.fixture
def task_diagnostic_capture(download_capture):
    fixture = download_capture
    trace = ScanLog(str(fixture.directory / "scan.db"), 1)
    original_file = trace._file
    state = SimpleNamespace(event=None, failure=None, boundary="write", calls=0, reached=None)

    class File:
        def write(self, data):
            self.fault = json.loads(data)["event"] == state.event
            if self.fault:
                state.calls += 1
                state.reached.set()
                if state.boundary == "write" and state.failure is not None:
                    raise state.failure
            return original_file.write(data)

        def flush(self):
            original_file.flush()
            if self.fault and state.boundary == "flush" and state.failure is not None:
                raise state.failure

        def close(self):
            original_file.close()

    trace._file = File()
    fixture.state.emit = trace.emit
    state.fixture = fixture
    state.trace = trace
    yield state
    trace.close()


def schedule_diagnostic(fixture, event):
    if event == "file_selection":
        fixture.events["filechooser"](SimpleNamespace(set_files=AsyncMock()))
    else:
        async def save(path):
            path.write_bytes(b"local diagnostic fixture")

        fixture.events["download"](download_item(save))


@pytest.mark.parametrize("timing", ["early", "late"])
@pytest.mark.parametrize("event", ["download_start", "download_end", "file_selection"])
@pytest.mark.parametrize("boundary", ["write", "flush"])
@pytest.mark.parametrize("failure_type", [TimeoutError, PlaywrightError, OSError])
def test_task_diagnostic_type_does_not_change_original_failure(task_diagnostic_capture, timing, event, boundary, failure_type):
    state = task_diagnostic_capture
    fixture = state.fixture
    failure = failure_type(errno.ETIMEDOUT, "private diagnostic fault") if failure_type is TimeoutError else failure_type("private diagnostic fault")
    state.event, state.boundary, state.failure = event, boundary, failure

    async def exercise():
        state.reached = asyncio.Event()
        started, finish = asyncio.Event(), asyncio.Event()

        async def hold_first_download(path):
            started.set()
            await finish.wait()
            path.write_bytes(b"first")

        async def scenario():
            if timing == "early":
                schedule_diagnostic(fixture, event)
            else:
                # The first task is included in capture's gather before the failing task exists.
                fixture.events["download"](download_item(hold_first_download))

        if timing == "late" and event in {"download_start", "download_end"}:
            state.event = None
        task = asyncio.create_task(fixture.capture(scenario))
        if timing == "late":
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.sleep(0)
            state.event = event
            schedule_diagnostic(fixture, event)
            await asyncio.wait_for(state.reached.wait(), timeout=1)
            state.event = None
            finish.set()
        with pytest.raises(failure_type) as raised:
            await task
        assert raised.value is failure
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(exercise())
    assert state.calls == 1
    rows = [json.loads(line) for line in state.trace.path.read_text().splitlines()]
    fault_rows = [row for row in rows if row["event"] == event]
    expected_healthy_rows = int(timing == "late" and event in {"download_start", "download_end"})
    assert len(fault_rows) == expected_healthy_rows + int(boundary == "flush")
    assert not list(fixture.directory.glob("*.part"))
    fixture.client.detach.assert_awaited_once()
    fixture.page.close.assert_awaited_once()


@pytest.mark.parametrize("event", ["download_start", "download_end", "file_selection"])
def test_successful_task_diagnostics_keep_existing_capture(task_diagnostic_capture, event):
    state = task_diagnostic_capture
    fixture = state.fixture
    state.event = event

    async def exercise():
        state.reached = asyncio.Event()

        async def scenario():
            schedule_diagnostic(fixture, event)

        result = await fixture.capture(scenario)
        assert result.error is None
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        return result

    result = asyncio.run(exercise())
    assert state.calls == 1
    assert [record["status"] for record in result.observations] == ([] if event == "file_selection" else ["saved"])
    fixture.client.detach.assert_awaited_once()
    fixture.page.close.assert_awaited_once()


@pytest.mark.parametrize("stop", ["error", "cancellation", "deadline"])
@pytest.mark.parametrize("event", ["download_start", "download_end", "file_selection"])
@pytest.mark.parametrize("failure_type", [TimeoutError, PlaywrightError])
def test_original_capture_stop_precedes_task_diagnostic(task_diagnostic_capture, stop, event, failure_type):
    state = task_diagnostic_capture
    fixture = state.fixture
    state.event, state.failure = event, failure_type("private diagnostic fault")
    primary = RuntimeError("private capture failure")

    async def exercise():
        state.reached = asyncio.Event()

        async def goto(*args, **kwargs):
            schedule_diagnostic(fixture, event)
            await state.reached.wait()
            if stop == "error":
                raise primary
            await asyncio.Event().wait()

        fixture.page.goto = goto

        async def capture():
            return await browser.capture_page(
                fixture.context, fixture.page.url, max_scrolls=0, settle_ms=0,
                exploration=fixture.state, download_dir=fixture.directory,
                deadline=asyncio.get_running_loop().time() + 0.03 if stop == "deadline" else None,
            )

        task = asyncio.create_task(capture())
        await asyncio.wait_for(state.reached.wait(), timeout=1)
        if stop == "cancellation":
            task.cancel("caller cancellation")
        if stop == "deadline":
            result = await task
            assert result.error == "ScanDeadlineExceeded"
            assert [row for row in result.observations if row.get("kind") == "capture_cleanup"] == [
                {"kind": "capture_cleanup", "errors": [failure_type.__name__]},
            ]
        else:
            with pytest.raises(RuntimeError if stop == "error" else asyncio.CancelledError) as raised:
                await task
            if stop == "error":
                assert raised.value is primary
            else:
                assert task.cancelled()
                assert raised.value.args == ("caller cancellation",)
            assert raised.value.__notes__ == [f"Page task failed: {failure_type.__name__}"]
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(exercise())
    assert state.calls == 1
    fixture.client.detach.assert_awaited_once()
    fixture.page.close.assert_awaited_once()
