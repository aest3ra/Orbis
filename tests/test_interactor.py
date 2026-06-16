"""Tests for conservative dynamic interactions."""

from __future__ import annotations

import asyncio

import pytest

from orbis.config import ScopeConfig
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

    async def eval_on_selector(self, selector: str, _script: str, value: str) -> None:
        self.actions.append(("form", selector, value))


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    async def fake_settle(_page):
        return None

    async def fake_collect(page):
        return page.candidates

    monkeypatch.setattr(interactor, "_settle", fake_settle)
    monkeypatch.setattr(interactor, "_collect_candidates", fake_collect)


def test_get_form_submitted_but_post_form_ignored_in_same_pass() -> None:
    page = FakePage([
        {
            "kind": "form",
            "selector": "#search",
            "text": "Search",
            "form_method": "GET",
            "form_action": "https://example.com/search",
        },
        {
            "kind": "form",
            "selector": "#login",
            "text": "Login",
            "form_method": "POST",
            "form_action": "https://example.com/login",
        },
    ])

    count = asyncio.run(run_safe_interactions(page, _scope()))

    assert count == 1
    assert page.actions == [("form", "#search", "test")]


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
        {"kind": "form", "selector": "#file", "text": "Search", "has_file": True},
        {"kind": "form", "selector": "#hidden", "text": "Search", "has_hidden_submit": True},
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
            "nav_url": "https://example.com/next",
        },
    ])

    assert asyncio.run(run_safe_interactions(page, _scope())) == 0
    assert page.actions == []


def test_out_of_scope_get_form_ignored() -> None:
    page = FakePage([
        {
            "kind": "form",
            "selector": "#search",
            "text": "Search",
            "form_method": "GET",
            "form_action": "https://other.example/search",
        }
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


def test_action_cap_is_fixed() -> None:
    page = FakePage([
        {"kind": "button", "selector": f"#b{i}", "text": "Load more"}
        for i in range(20)
    ])

    count = asyncio.run(run_safe_interactions(page, _scope()))

    assert count == 12
    assert len(page.actions) == 12


class FakeTracker:
    def __init__(self) -> None:
        self.begins = 0
        self.commits: list[tuple] = []

    def begin(self) -> None:
        self.begins += 1

    def commit(self, action: str, selector: str, label: str) -> None:
        self.commits.append((action, selector, label))


def test_tracker_brackets_each_successful_action() -> None:
    page = FakePage([
        {"kind": "button", "selector": "#tab", "text": "Results tab"},
        {"kind": "button", "selector": "#more", "text": "Load more"},
    ])
    tracker = FakeTracker()

    count = asyncio.run(run_safe_interactions(page, _scope(), tracker=tracker))

    assert count == 2
    assert tracker.begins == 2
    assert tracker.commits == [
        ("button", "#tab", "Results tab"),
        ("button", "#more", "Load more"),
    ]


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
    async def leave_scope(page):
        page.url = "https://evil.com/"

    monkeypatch.setattr(interactor, "_settle", leave_scope)
    page = FakePage([
        {"kind": "button", "selector": "#one", "text": "Load more"},
        {"kind": "button", "selector": "#two", "text": "Load more"},
    ])

    count = asyncio.run(run_safe_interactions(page, _scope()))

    assert count == 1
    assert page.actions == [("click", "#one")]
