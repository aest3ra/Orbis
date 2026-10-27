"""Tests for conservative dynamic interactions."""

from __future__ import annotations

import asyncio

import pytest

from orbis.config import InteractionConfig, ScopeConfig
from orbis.crawler import interactor
from orbis.crawler.interactor import run_safe_interactions
from orbis.crawler.scope import Scope


def _scope(domains: list[str] | None = None) -> Scope:
    return Scope(ScopeConfig(include_domains=domains or ["example.com"]))


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "FakeLocator":
        return self

    async def fill(self, value: str, **_kwargs) -> None:
        self.page.actions.append(("fill", self.selector, value))

    async def press(self, key: str, **_kwargs) -> None:
        self.page.actions.append(("press", self.selector, key))

    async def click(self, **_kwargs) -> None:
        self.page.actions.append(("click", self.selector))

    async def is_visible(self, **_kwargs) -> bool:
        return True

    async def is_enabled(self, **_kwargs) -> bool:
        return True


class FakePage:
    def __init__(self, candidates: list[dict], url: str = "https://example.com/") -> None:
        self.url = url
        self.candidates = candidates
        self.actions: list[tuple] = []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def evaluate(self, _script):
        return None

    def on(self, _event, _callback):
        pass

    def remove_listener(self, _event, _callback):
        pass


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    async def fake_settle(_page, _network_quiet):
        return None

    async def fake_collect(page):
        return page.candidates

    monkeypatch.setattr(interactor, "_settle", fake_settle)
    monkeypatch.setattr(interactor, "_collect_candidates", fake_collect)


def test_search_input_enters_value() -> None:
    page = FakePage([
        {
            "kind": "input",
            "selector": "#q",
            "text": "search query",
        }
    ])

    count = asyncio.run(run_safe_interactions(page, _scope()))

    assert count == 1
    assert page.actions == [("fill", "#q", "test"), ("press", "#q", "Enter")]


def test_unsafe_candidates_ignored() -> None:
    page = FakePage([
        {"kind": "button", "selector": "#download", "text": "Download report"},
        {"kind": "button", "selector": "#delete", "text": "Delete account"},
        {
            "kind": "button",
            "selector": "#download-url",
            "text": "Load more",
            "action": "https://example.com/report.pdf",
        },
        {
            "kind": "button",
            "selector": "#logout-action",
            "text": "Load more",
            "action": "https://example.com/logout",
        },
        {
            "kind": "button",
            "selector": "#post-button",
            "text": "Search",
            "method": "POST",
            "action": "https://example.com/search",
        },
        {
            "kind": "button",
            "selector": "#nav",
            "text": "Load more",
            "nav_url": "https://other.example/next",
        },
    ])

    assert asyncio.run(run_safe_interactions(page, _scope())) == 0
    assert page.actions == []


def test_safe_buttons_are_clicked() -> None:
    page = FakePage([
        {"kind": "button", "selector": "#tab", "text": "Results tab"},
        {"kind": "button", "selector": "#modal", "text": "Open modal"},
        {"kind": "button", "selector": "#more", "text": "Load more"},
        {"kind": "button", "selector": "#next", "text": "Next"},
    ])

    count = asyncio.run(run_safe_interactions(page, _scope()))

    assert count == 4
    assert page.actions == [
        ("click", "#tab"),
        ("click", "#modal"),
        ("click", "#more"),
        ("click", "#next"),
    ]


def test_structural_buttons_are_clicked_without_label_keywords() -> None:
    page = FakePage([
        {"kind": "button", "selector": "#role-tab", "text": "", "role": "tab"},
        {"kind": "button", "selector": "#accordion", "text": "", "expanded": "false"},
        {"kind": "button", "selector": "#summary", "text": "", "tag": "summary"},
    ])

    count = asyncio.run(run_safe_interactions(page, _scope()))

    assert count == 3
    assert page.actions == [
        ("click", "#role-tab"),
        ("click", "#accordion"),
        ("click", "#summary"),
    ]


def test_action_cap_is_fixed() -> None:
    page = FakePage([
        {"kind": "button", "selector": f"#b{i}", "text": "Open modal"}
        for i in range(30)
    ])

    count = asyncio.run(run_safe_interactions(page, _scope()))

    assert count == interactor.MAX_ACTIONS_PER_PAGE
    assert len(page.actions) == interactor.MAX_ACTIONS_PER_PAGE


def test_failed_click_records_reason_without_exception_text(monkeypatch):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    async def timeout(self, **kwargs):
        raise PlaywrightTimeoutError("private-cookie in browser error")

    monkeypatch.setattr(FakeLocator, "click", timeout)
    page = FakePage([{"kind": "button", "selector": "#tab", "text": "Results tab"}])
    events = []
    state = interactor.ExplorationState(emit=lambda event, **record: events.append((event, record)))
    assert asyncio.run(run_safe_interactions(page, _scope(), state=state)) == 0
    record = state.records[0]
    assert record["status"] == "failed" and record["failure_reason"] == "action_timeout"
    assert record["duration_ms"] >= 0 and record["action_id"] == 1
    assert record["observed_progress"] is False
    assert "private-cookie" not in str(events)
    assert [name for name, _ in events] == ["action_start", "action_end", "exploration_end"]


def test_interrupted_action_keeps_start_and_end_records(monkeypatch):
    async def cancel(_page, _candidate):
        raise asyncio.CancelledError()

    monkeypatch.setattr(interactor, "_perform", cancel)
    page = FakePage([{"kind": "button", "selector": "#tab", "text": "Results tab"}])
    events = []
    state = interactor.ExplorationState(emit=lambda event, **record: events.append((event, record)))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_safe_interactions(page, _scope(), state=state))
    assert [name for name, _ in events] == ["action_start", "action_end"]
    assert state.records[0]["status"] == "interrupted"
    assert state.current_action is None


class FakeTracker:
    def __init__(self) -> None:
        self.begins = 0
        self.commits: list[tuple] = []

    def begin(self) -> None:
        self.begins += 1

    def commit(self, tag: str) -> None:
        self.commits.append(tag)


def test_tracker_brackets_each_successful_action() -> None:
    page = FakePage([
        {"kind": "button", "selector": "#tab", "text": "Results tab"},
        {"kind": "button", "selector": "#more", "text": "Load more"},
    ])
    tracker = FakeTracker()

    count = asyncio.run(run_safe_interactions(page, _scope(), tracker=tracker))

    assert count == 2
    assert tracker.begins == 2
    assert tracker.commits == ["Results tab", "Load more"]


def test_tracker_untouched_for_unsafe_candidates() -> None:
    page = FakePage([
        {"kind": "button", "selector": "#delete", "text": "Delete account"},
    ])
    tracker = FakeTracker()

    count = asyncio.run(run_safe_interactions(page, _scope(), tracker=tracker))

    assert count == 0
    assert tracker.begins == 0
    assert tracker.commits == []


def test_out_of_scope_after_action_stops(monkeypatch) -> None:
    async def leave_scope(page, _network_quiet):
        page.url = "https://evil.com/"

    monkeypatch.setattr(interactor, "_settle", leave_scope)
    page = FakePage([
        {"kind": "button", "selector": "#one", "text": "Load more"},
        {"kind": "button", "selector": "#two", "text": "Load more"},
    ])

    count = asyncio.run(run_safe_interactions(page, _scope()))

    assert count == 1
    assert page.actions == [("click", "#one")]


def test_rediscovery_acts_on_ui_revealed_by_earlier_clicks(monkeypatch) -> None:
    """A modal that only appears after a tab click is acted on next round."""
    rounds = [
        [{"kind": "button", "selector": "#tab", "text": "Results tab"}],
        [
            {"kind": "button", "selector": "#tab", "text": "Results tab"},
            {"kind": "button", "selector": "#modal", "text": "Open modal"},
        ],
    ]
    calls = {"n": 0}

    async def staged_collect(_page):
        idx = min(calls["n"], len(rounds) - 1)
        calls["n"] += 1
        return rounds[idx]

    monkeypatch.setattr(interactor, "_collect_candidates", staged_collect)
    page = FakePage([])

    count = asyncio.run(run_safe_interactions(page, _scope(), max_actions=2))

    # The newly revealed modal is selected before the tab already visible in the prior state.
    assert count == 2
    assert page.actions == [("click", "#tab"), ("click", "#modal")]


def test_same_button_can_be_used_in_distinct_screen_states(monkeypatch) -> None:
    page = FakePage([{"kind": "button", "selector": "#toggle", "text": "Next", "expanded": "false"}])
    original_click = FakeLocator.click

    async def change_state(self, **kwargs):
        await original_click(self, **kwargs)
        page.candidates = [{"kind": "button", "selector": "#toggle", "text": "Next",
                            "expanded": "true" if len(page.actions) == 1 else "false"}]

    monkeypatch.setattr(FakeLocator, "click", change_state)
    count = asyncio.run(run_safe_interactions(page, _scope()))

    assert count == 2
    assert page.actions == [("click", "#toggle"), ("click", "#toggle")]


class FakePaginationTracker:
    """Reports a fixed sequence of "requests fired" counts per commit."""

    def __init__(self, fired_sequence: list[int]) -> None:
        self.fired = list(fired_sequence)
        self.begins = 0
        self.commits: list[tuple] = []

    def begin(self) -> None:
        self.begins += 1

    def commit(self, tag: str) -> int:
        self.commits.append(tag)
        return self.fired.pop(0) if self.fired else 0


def test_pagination_repeats_while_requests_keep_firing() -> None:
    page = FakePage([
        {"kind": "button", "selector": "#more", "text": "Load more"},
    ])
    # two productive clicks, then a dry one → walking stops at the third.
    tracker = FakePaginationTracker([3, 2, 0])

    count = asyncio.run(run_safe_interactions(page, _scope(), tracker=tracker))

    assert count == 3
    assert page.actions == [("click", "#more")] * 3
    assert len(tracker.commits) == 3


def test_pagination_capped_by_max_repeats() -> None:
    page = FakePage([
        {"kind": "button", "selector": "#more", "text": "Load more"},
    ])
    # always productive — bounded only by MAX_PAGINATION_REPEATS.
    tracker = FakePaginationTracker([9] * 10)

    count = asyncio.run(run_safe_interactions(page, _scope(), tracker=tracker))

    assert count == interactor.MAX_PAGINATION_REPEATS


def test_non_paginator_clicked_once_even_with_tracker() -> None:
    page = FakePage([
        {"kind": "button", "selector": "#tab", "text": "Results tab"},
    ])
    tracker = FakePaginationTracker([5] * 10)

    count = asyncio.run(run_safe_interactions(page, _scope(), tracker=tracker))

    assert count == 1
    assert page.actions == [("click", "#tab")]


def test_repeat_cap_bounds_unproductive_toggles(monkeypatch) -> None:
    """Three accordion toggles each flip their own state and never yield a
    request. Per-screen-state history would re-open and re-close them until the
    budget is gone; the per-URL cap holds each at two executions."""
    opened = [False, False, False]

    def candidates():
        return [{"kind": "button", "selector": f"#t{i}", "key": f"#t{i}", "text": f"Section {i}",
                 "expanded": "true" if opened[i] else "false"} for i in range(3)]

    page = FakePage(candidates())
    original_click = FakeLocator.click

    async def flip(self, **kwargs):
        await original_click(self, **kwargs)
        index = int(self.selector[2:])
        opened[index] = not opened[index]
        page.candidates = candidates()

    monkeypatch.setattr(FakeLocator, "click", flip)
    count = asyncio.run(run_safe_interactions(page, _scope(), max_actions=20))

    assert count == 6
    assert all(page.actions.count(("click", f"#t{i}")) == 2 for i in range(3))


def test_productive_candidate_may_repeat_beyond_cap(monkeypatch) -> None:
    """A control whose latest execution still produced new request routes is not
    capped; it stops one execution after it runs dry."""
    clicks = {"n": 0}

    def candidates():
        return [{"kind": "button", "selector": "#more", "key": "#more", "text": "More",
                 "expanded": str(clicks["n"])}]

    page = FakePage(candidates())
    original_click = FakeLocator.click

    async def advance(self, **kwargs):
        await original_click(self, **kwargs)
        clicks["n"] += 1
        page.candidates = candidates()

    monkeypatch.setattr(FakeLocator, "click", advance)
    exploration = interactor.ExplorationState()

    async def observe():
        exploration.records[-1]["new_request_routes"] = 1 if clicks["n"] <= 4 else 0

    count = asyncio.run(run_safe_interactions(page, _scope(), state=exploration, observe=observe, max_actions=20))

    assert count == 5
    assert [row["attempt_number"] for row in exploration.records] == [1, 2, 3, 4, 5]


def test_never_executed_candidate_outranks_a_repeat(monkeypatch) -> None:
    """An input whose execution changes the screen state would be eligible again
    with a higher priority than the submit button; the submit button must still
    come first because it has never run on this URL."""
    flips = {"n": 0}

    def candidates():
        return [
            # "name" rather than a search-like label: a search input also presses Enter,
            # which would add a second recorded action for the same candidate.
            {"kind": "input", "selector": "#q", "key": "#q", "text": "name", "type": "text",
             "expanded": str(flips["n"])},
            {"kind": "button", "selector": "#save", "key": "#save", "text": "Save", "label": "Save",
             "button_type": "button"},
        ]

    page = FakePage(candidates())
    original_fill = FakeLocator.fill

    async def fill(self, value, **kwargs):
        await original_fill(self, value, **kwargs)
        flips["n"] += 1
        page.candidates = candidates()

    monkeypatch.setattr(FakeLocator, "fill", fill)
    options = InteractionConfig(allow_write=True)
    asyncio.run(run_safe_interactions(page, _scope(), options=options, max_actions=6))

    assert [action[:2] for action in page.actions][:3] == [("fill", "#q"), ("click", "#save"), ("fill", "#q")]


def test_failed_executions_do_not_consume_the_cap(monkeypatch) -> None:
    """A submit control covered twice (not visible) must still run once it is
    visible again in a later screen state; only completed executions count
    toward MAX_REPEATS_PER_CANDIDATE."""
    flips = {"n": 0}
    visibility_checks = {"save": 0}

    def candidates():
        return [
            {"kind": "button", "selector": "#save", "key": "#save", "text": "Save", "label": "Save",
             "button_type": "button"},
            {"kind": "button", "selector": "#toggle", "key": "#toggle", "text": "Section",
             "expanded": str(flips["n"])},
        ]

    page = FakePage(candidates())
    original_click = FakeLocator.click

    async def click(self, **kwargs):
        await original_click(self, **kwargs)
        if self.selector == "#toggle":
            flips["n"] += 1
            page.candidates = candidates()

    async def is_visible(self, **_kwargs):
        if self.selector == "#save":
            visibility_checks["save"] += 1
            return visibility_checks["save"] > 2
        return True

    monkeypatch.setattr(FakeLocator, "click", click)
    monkeypatch.setattr(FakeLocator, "is_visible", is_visible)
    asyncio.run(run_safe_interactions(page, _scope(), options=InteractionConfig(allow_write=True), max_actions=10))

    assert ("click", "#save") in page.actions
    assert visibility_checks["save"] == 3


def _site_wide_pages(exploration, yields):
    """Three pages that share a 'Compose' button revealing a Title input, plus one
    page-specific link each. ``yields`` says whether the widget's input produces a
    new request route when filled."""
    orders = []
    for name in ("a", "b", "c"):
        link = {"kind": "link", "selector": f"#{name}", "key": f"#{name}", "text": f"Detail {name}",
                "label": f"Detail {name}", "nav_url": f"https://example.com/{name}/detail", "method": "GET"}
        compose = {"kind": "button", "selector": "#compose", "key": "#compose", "text": "Compose",
                   "label": "Compose", "button_type": "button"}
        title = {"kind": "input", "selector": "#title", "key": "#title", "text": "Title", "label": "Title",
                 "name": "title", "type": "text"}
        page = FakePage([compose, link], url=f"https://example.com/{name}")
        original_click = FakeLocator.click

        async def click(self, **kwargs):
            await original_click(self, **kwargs)
            if self.selector == "#compose":
                page.candidates = [compose, title, link]

        FakeLocator.click = click

        async def observe():
            action = exploration.current_action
            yielded = action["kind"] == "link" or (action["kind"] == "input" and yields)
            exploration.records[-1]["new_request_routes"] = 1 if yielded else 0

        try:
            asyncio.run(run_safe_interactions(page, _scope(), options=InteractionConfig(allow_write=True),
                                              state=exploration, observe=observe, max_actions=3))
        finally:
            FakeLocator.click = original_click
        orders.append([selector for _, selector, *_ in page.actions])
    return orders


def test_site_wide_widget_goes_after_page_controls_once_dry_on_two_urls() -> None:
    exploration = interactor.ExplorationState()
    orders = _site_wide_pages(exploration, yields=False)
    # Pages a and b: the Compose widget runs first by priority and yields nothing.
    assert orders[0][0] == "#compose" and orders[1][0] == "#compose"
    # Page c: the page's own link comes first; the widget still gets its turn.
    assert orders[2][0] == "#c"
    assert "#compose" in orders[2]


def test_site_wide_widget_that_keeps_yielding_is_not_demoted() -> None:
    exploration = interactor.ExplorationState()
    orders = _site_wide_pages(exploration, yields=True)
    assert [order[0] for order in orders] == ["#compose", "#compose", "#compose"]
