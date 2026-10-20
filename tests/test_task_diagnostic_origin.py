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
