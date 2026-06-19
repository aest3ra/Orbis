"""Regression: js_static must not catastrophically backtrack on adversarial JS.

A run of backslashes inside an unclosed string used to make _ASSIGNMENT_RE hang
(a ~45-char input took seconds; real minified bundles hung for many minutes,
GIL-held and uninterruptible). SIGALRM converts a regression into a fast test
failure instead of a hang.
"""

import signal
import time

import pytest

from orbis.analysis.js_static import extract_js_endpoints


def _guard(seconds: float):
    def handler(_s, _f):
        raise AssertionError("catastrophic backtracking regression (timed out)")
    signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)


def _clear():
    signal.setitimer(signal.ITIMER_REAL, 0)


def test_backslash_run_does_not_hang() -> None:
    body = 'x = "' + "\\" * 200 + ' /api/v1/users'
    _guard(3.0)
    try:
        t = time.monotonic()
        extract_js_endpoints(body)
        assert time.monotonic() - t < 3.0
    finally:
        _clear()


def test_many_assignments_with_quotes_do_not_hang() -> None:
    # mixed quotes + backslashes across many assignments (minified-like)
    body = ";".join(f'v{i} = "a\\\\b{i}" + \'c\\\\\' ' for i in range(2000))
    _guard(3.0)
    try:
        extract_js_endpoints(body)
    finally:
        _clear()


def test_still_extracts_normal_endpoints() -> None:
    body = 'const u = "/api/v1/users"; fetch("/api/v1/orders");'
    refs = extract_js_endpoints(body)
    paths = {r.raw_url for r in refs}
    assert any("/api/v1/users" in p for p in paths)
    assert any("/api/v1/orders" in p for p in paths)
