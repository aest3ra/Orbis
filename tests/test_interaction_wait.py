"""Interaction waiting must follow new activity and release its observers."""

import asyncio
import time

import pytest

from orbis.crawler import interactor


class ActivityPage:
    def __init__(self):
        self.listeners = {}
        self.last_mutation = 0.0
        self.observing = False

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        assert self.listeners.pop(event) is callback

    async def evaluate(self, script):
        if script == interactor._WATCH_DOM_JS:
            self.observing = True
            self.last_mutation = time.monotonic()
        elif script == interactor._DOM_QUIET_JS:
            return (time.monotonic() - self.last_mutation) * 1000
        else:
            self.observing = False


@pytest.fixture(autouse=True)
def short_waits(monkeypatch):
    monkeypatch.setattr(interactor, "_SETTLE_MS", 30)
    monkeypatch.setattr(interactor, "_MIN_OBSERVATION_MS", 30)
    monkeypatch.setattr(interactor, "_SETTLE_POLL_MS", 5)
    monkeypatch.setattr(interactor, "_SETTLE_TIMEOUT_MS", 250)


@pytest.mark.parametrize("completion_event", ["requestfinished", "requestfailed"])
def test_waits_for_request_started_inside_action(monkeypatch, completion_event):
    page = ActivityPage()
    completed = []

    async def exercise():
        request = object()

        def complete():
            completed.append(time.monotonic())
            page.listeners[completion_event](request)

        async def perform(_page, _candidate):
            page.listeners["request"](request)
            asyncio.get_running_loop().call_later(0.09, complete)
            return True

        monkeypatch.setattr(interactor, "_perform", perform)
        assert await interactor._perform_and_settle(page, {})
        assert completed
        assert time.monotonic() - completed[0] >= 0.025

    asyncio.run(exercise())
    assert not page.listeners and not page.observing


def test_dom_changes_extend_wait_without_network(monkeypatch):
    page = ActivityPage()

    async def exercise():
        async def mutate():
            for _ in range(4):
                await asyncio.sleep(0.02)
                page.last_mutation = time.monotonic()

        tasks = []

        async def perform(_page, _candidate):
            tasks.append(asyncio.create_task(mutate()))
            return True

        monkeypatch.setattr(interactor, "_perform", perform)
        assert await interactor._perform_and_settle(page, {})
        assert tasks[0].done()
        assert time.monotonic() - page.last_mutation >= 0.025

    asyncio.run(exercise())
    assert not page.listeners and not page.observing


def test_unfinished_request_has_bounded_wait(monkeypatch):
    page = ActivityPage()

    async def perform(_page, _candidate):
        page.listeners["request"](object())
        return True

    monkeypatch.setattr(interactor, "_perform", perform)
    started = time.monotonic()
    assert asyncio.run(interactor._perform_and_settle(page, {}))
    elapsed = time.monotonic() - started
    assert 0.20 <= elapsed < 1.0
    assert not page.listeners and not page.observing


def test_failed_action_removes_observers(monkeypatch):
    page = ActivityPage()

    async def perform(_page, _candidate):
        return False

    monkeypatch.setattr(interactor, "_perform", perform)
    assert not asyncio.run(interactor._perform_and_settle(page, {}))
    assert not page.listeners and not page.observing
