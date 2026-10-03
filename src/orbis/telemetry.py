"""Append-only scan diagnostics, separate from endpoint evidence."""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

SCHEMA_VERSION = 1
_URL_RE = re.compile(r"(?:https?|wss?)://[^\s\"<>]+", re.IGNORECASE)
_PRIVATE_FIELDS = {"authorization", "cookie", "cookies", "headers", "request_headers",
                   "response_headers", "post_data", "response_body", "password", "api_key", "value"}


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def safe_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "[invalid_url]"
    host = parsed.netloc.rsplit("@", 1)[-1]
    query = urlencode([(name, "[redacted]") for name, _ in parse_qsl(parsed.query, keep_blank_values=True)])
    return urlunsplit((parsed.scheme, host, parsed.path, query, ""))


def clean(value):
    if isinstance(value, dict):
        return {key: "[redacted]" if key.lower() in _PRIVATE_FIELDS else clean(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, str):
        return _URL_RE.sub(lambda match: safe_url(match.group()), value)
    return value


class ScanLog:
    def __init__(self, db_path: str, scan_id: int) -> None:
        self.scan_id = scan_id
        self.run_id = uuid4().hex
        self.path = Path(db_path).resolve().with_suffix(f".scan-{scan_id}-{self.run_id[:8]}.jsonl")
        self.started = time.monotonic()
        self.sequence = 0
        self.totals: Counter = Counter()
        descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            self._file = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException as failure:
            try:
                os.close(descriptor)
            except OSError as exc:
                failure.add_note(f"Scan log initialization cleanup failed: {type(exc).__name__}")
            raise

    def emit(self, event: str, **details) -> dict:
        self.sequence += 1
        record = clean({"schema_version": SCHEMA_VERSION, "run_id": self.run_id,
                        "scan_id": self.scan_id, "sequence": self.sequence,
                        "timestamp": timestamp(), "elapsed_ms": round((time.monotonic() - self.started) * 1000, 1),
                        "event": event, **details})
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()
        self.totals[event] += 1
        if event == "page_end" and details.get("status") in {"completed", "partial", "failed"}:
            self.totals["pages_crawled"] += 1
        if event == "action_end":
            self.totals[f"actions_{details['status']}"] += 1
            self.totals["actions_with_http_errors"] += bool(details.get("http_error_count"))
            self.totals["actions_without_observed_progress"] += details.get("observed_progress") is False
        if event == "download_end":
            self.totals["duplicate_downloads"] += details.get("content_seen_count", 0) > 1
        return record

    def close(self) -> None:
        self._file.close()
