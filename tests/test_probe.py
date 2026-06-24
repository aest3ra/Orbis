"""Tests for active endpoint probing."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from orbis.config import ScopeConfig
from orbis.crawler.probe import (
    classify_probe,
    probe_candidates,
    probe_endpoint,
    probe_target_ok,
)
from orbis.crawler.scope import Scope
from orbis.storage.db import Endpoint, open_db
from orbis.storage.repo import (
    create_scan,
    list_unverified_endpoints,
    set_probe_result,
)


def _scope(domains: list[str] | None = None) -> Scope:
    return Scope(ScopeConfig(include_domains=domains or ["example.com"]))


def _limits(
    *,
    probe_max_requests: int = 500,
    probe_timeout_sec: float = 1,
    rate_limit_rps: float = 0,
):
    return SimpleNamespace(
        probe_max_requests=probe_max_requests,
        probe_timeout_sec=probe_timeout_sec,
        rate_limit_rps=rate_limit_rps,
    )


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.disposed = False

    async def text(self) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")

    async def dispose(self) -> None:
        self.disposed = True


class TargetClosedError(Exception):
    pass


class FakeRequest:
    def __init__(self, outcomes: list[int | BaseException], *, delay: float = 0) -> None:
        self.outcomes = outcomes
        self.delay = delay
        self.calls: list[tuple[str, int, float]] = []
        self.responses: list[FakeResponse] = []

    async def get(self, url: str, *, max_redirects: int, timeout: float) -> FakeResponse:
        self.calls.append((url, max_redirects, timeout))
        if self.delay:
            await asyncio.sleep(self.delay)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        resp = FakeResponse(outcome)
        self.responses.append(resp)
        return resp


class FakeContext:
    def __init__(self, outcomes: list[int | BaseException], *, delay: float = 0) -> None:
        self.request = FakeRequest(outcomes, delay=delay)


class TestClassifyProbe:
    @pytest.mark.parametrize("status", [200, 301, 302, 400, 401, 403, 405, 429, 500, 503])
    def test_response_statuses_are_verified(self, status: int) -> None:
        assert classify_probe(status) == "verified"

    @pytest.mark.parametrize("status", [404, 410, None])
    def test_missing_or_no_response_is_failed(self, status: int | None) -> None:
        assert classify_probe(status) == "failed"


class TestProbeTargetOk:
    @pytest.mark.parametrize("url", [
        "",
        None,
        "//example.com/api/users",
        "ws://example.com/api/users",
        "https://evil.com/api/users",
        "https://example.com/logout",
        "https://example.com/report.pdf",
        "https://example.com/api/users/{id}",
        "https://example.com/api/archive/{date}",
        "https://example.com/api/${tenant}/users",
    ])
    def test_blocks_unsafe_targets(self, url: str | None) -> None:
        assert probe_target_ok(url, _scope(["example.com"])) is False

    def test_allows_safe_in_scope_http_url(self) -> None:
        assert probe_target_ok("https://example.com/api/users", _scope()) is True

    @pytest.mark.parametrize("url", [
        "https://example.com/api/users?action=delete",
        "https://example.com/api/users?do=deactivate",
        "https://example.com/api/users?token=abc",
        "https://example.com/verify-email?next=/",
        "https://example.com/api/users?next=/confirm",
    ])
    def test_blocks_query_danger(self, url: str) -> None:
        assert probe_target_ok(url, _scope()) is False


class TestProbeEndpoint:
    def test_uses_get_without_redirects_and_does_not_read_body(self) -> None:
        context = FakeContext([302])

        result = asyncio.run(
            probe_endpoint(context, "https://example.com/redirect", timeout_sec=1)
        )

        assert result.status == 302
        assert context.request.calls == [
            ("https://example.com/redirect", 0, 1000)
        ]

    def test_timeout_returns_failed_result(self) -> None:
        context = FakeContext([200], delay=0.05)

        result = asyncio.run(
            probe_endpoint(context, "https://example.com/slow", timeout_sec=0.01)
        )

        assert result.status is None
        assert result.error == "TimeoutError"

    def test_context_closed_reraises(self) -> None:
        context = FakeContext([TargetClosedError("closed")])

        with pytest.raises(TargetClosedError):
            asyncio.run(
                probe_endpoint(context, "https://example.com/api", timeout_sec=1)
            )


class TestProbeCandidates:
    def test_skips_do_not_become_failed_or_spend_budget(self) -> None:
        context = FakeContext([200])
        candidates = [
            (1, "https://example.com/api/{id}"),
            (2, "https://evil.com/api"),
            (3, "https://example.com/api/users"),
        ]

        results = asyncio.run(
            probe_candidates(
                context,
                candidates,
                scope=_scope(["example.com"]),
                limits=_limits(probe_max_requests=1),
            )
        )

        assert results == [(3, "verified", 200)]
        assert [call[0] for call in context.request.calls] == [
            "https://example.com/api/users"
        ]

    def test_budget_counts_only_actual_requests_after_skips(self) -> None:
        context = FakeContext([200, 404, 500])
        candidates = [
            (1, "https://example.com/api/{id}"),
            (2, "https://example.com/logout"),
            (3, "https://example.com/api/a"),
            (4, "https://example.com/api/b"),
            (5, "https://example.com/api/c"),
        ]

        results = asyncio.run(
            probe_candidates(
                context,
                candidates,
                scope=_scope(),
                limits=_limits(probe_max_requests=2),
            )
        )

        assert results == [
            (3, "verified", 200),
            (4, "failed", 404),
        ]
        assert [call[0] for call in context.request.calls] == [
            "https://example.com/api/a",
            "https://example.com/api/b",
        ]

    def test_network_errors_are_nonfatal(self) -> None:
        context = FakeContext([OSError("offline"), 503])
        candidates = [
            (1, "https://example.com/api/a"),
            (2, "https://example.com/api/b"),
        ]

        results = asyncio.run(
            probe_candidates(
                context,
                candidates,
                scope=_scope(),
                limits=_limits(probe_max_requests=5),
            )
        )

        assert results == [
            (1, "failed", None),
            (2, "verified", 503),
        ]

    def test_target_closed_stops_loop_and_leaves_remaining_unreported(self) -> None:
        context = FakeContext([200, TargetClosedError("closed"), 500])
        candidates = [
            (1, "https://example.com/api/a"),
            (2, "https://example.com/api/b"),
            (3, "https://example.com/api/c"),
        ]

        results = asyncio.run(
            probe_candidates(
                context,
                candidates,
                scope=_scope(),
                limits=_limits(probe_max_requests=5),
            )
        )

        assert results == [(1, "verified", 200)]
        assert [call[0] for call in context.request.calls] == [
            "https://example.com/api/a",
            "https://example.com/api/b",
        ]

    def test_zero_candidates_and_all_skipped_return_empty(self) -> None:
        context = FakeContext([])
        assert asyncio.run(
            probe_candidates(
                context,
                [],
                scope=_scope(),
                limits=_limits(),
            )
        ) == []
        assert asyncio.run(
            probe_candidates(
                context,
                [(1, "https://example.com/logout")],
                scope=_scope(),
                limits=_limits(),
            )
        ) == []
        assert context.request.calls == []


def test_probe_repo_helpers_persist_status_and_code(tmp_path) -> None:
    engine = open_db(tmp_path / "probe.db")
    with Session(engine) as session:
        scan_id = create_scan(session, "https://example.com")
        verified = Endpoint(
            scan_id=scan_id,
            method="GET",
            host="example.com",
            path_template="/api/live",
            sample_url="https://example.com/api/live",
            route_kind="application_api",
            source="passive",
            probe_status="unverified",
        )
        skipped = Endpoint(
            scan_id=scan_id,
            method="GET",
            host="example.com",
            path_template="/api/seen",
            sample_url="https://example.com/api/seen",
            route_kind="application_api",
            source="dynamic",
            probe_status=None,
        )
        session.add(verified)
        session.add(skipped)
        session.commit()
        session.refresh(verified)

        assert [ep.id for ep in list_unverified_endpoints(session, scan_id)] == [
            verified.id
        ]

        set_probe_result(session, verified.id, "verified", 405)
        session.commit()
        session.refresh(verified)

        assert verified.probe_status == "verified"
        assert verified.probe_code == 405


def test_probe_endpoint_disposes_response() -> None:
    """Status-only probing must still free the server-side response buffer."""
    context = FakeContext([200])
    result = asyncio.run(
        probe_endpoint(context, "https://example.com/api", timeout_sec=1)
    )
    assert result.status == 200
    assert context.request.responses[0].disposed is True


class _FakeContextNoReq:
    def __init__(self) -> None:
        self.request = object()


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    async def new_context(self, **_kw) -> _FakeContextNoReq:
        return _FakeContextNoReq()

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    async def launch(self, **_kw) -> _FakeBrowser:
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _FakeChromium()


class _FakePlaywrightCM:
    async def __aenter__(self) -> _FakePlaywright:
        return _FakePlaywright()

    async def __aexit__(self, *_a) -> bool:
        return False


def test_run_scan_no_probe_sends_zero_requests(monkeypatch, tmp_path) -> None:
    """--no-probe must skip the probe stage entirely; default --probe runs it.

    max_pages=0 makes the crawl loop body never execute (no real network),
    isolating the post-loop probe gate.
    """
    from orbis.config import ScanConfig
    from orbis.crawler import runner as runner_mod

    monkeypatch.setattr(runner_mod, "async_playwright", lambda: _FakePlaywrightCM())

    calls: list = []

    async def _spy(context, candidates, *, scope, limits):
        calls.append(candidates)
        return []

    monkeypatch.setattr(runner_mod, "probe_candidates", _spy)

    def _config() -> ScanConfig:
        cfg = ScanConfig(target="https://example.com/")
        cfg.limits.max_pages = 0
        cfg.limits.max_duration_sec = 5
        return cfg

    asyncio.run(runner_mod.run_scan(
        _config(), db_path=str(tmp_path / "noprobe.db"),
        passive=False, probe=False,
    ))
    assert calls == []  # --no-probe: probe stage never entered

    asyncio.run(runner_mod.run_scan(
        _config(), db_path=str(tmp_path / "probe.db"),
        passive=False, probe=True,
    ))
    assert len(calls) == 1  # default: probe stage runs once
