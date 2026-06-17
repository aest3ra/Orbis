#!/usr/bin/env python3
"""Build a NodeBB OpenAPI oracle and optionally compare it to an Orbis DB.

The local NodeBB OpenAPI root files in benchmarks/results contain path-level
$ref entries. This helper resolves those refs from NodeBB's public/openapi tree
on GitHub raw, counts the actual operations, normalizes route prefixes, and
writes a JSON report for benchmark scoring.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

import yaml


VALID_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
PARAM_SEGMENT_RE = re.compile(r"^\{[^}/]+\}$")
OPENAPI_PARAM_RE = re.compile(r"\{[^}/]+\}")
COLON_PARAM_RE = re.compile(r":([^/]+)")
NUMERIC_RE = re.compile(r"\d+$")
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
LONG_HEX_RE = re.compile(r"[0-9a-fA-F]{16,}$")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
SLUG_RE = re.compile(r"^\d+-.{8,}$")
EMBEDDED_NUM_RE = re.compile(r"\d{4,}")

DEFAULT_RAW_BASE = "https://raw.githubusercontent.com/NodeBB/NodeBB/master/public/openapi/"
DEFAULT_READ_SPEC = Path("benchmarks/results/nodebb-read.yaml")
DEFAULT_WRITE_SPEC = Path("benchmarks/results/nodebb-write.yaml")
DEFAULT_OUT = Path("benchmarks/results/nodebb/openapi-coverage.json")
DEFAULT_HOST = "localhost:34567"
FORBIDDEN_ORACLE_SOURCES = {"static_openapi", "static_docs"}


@dataclass(frozen=True)
class ExpectedEndpoint:
    api: str
    method: str
    raw_path: str
    path_template: str
    ref: str | None
    operation_id: str | None = None
    summary: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObservedEndpoint:
    method: str
    host: str
    path_template: str
    source: str
    route_kind: str
    seen_count: int
    scan_id: int | None = None


@dataclass(frozen=True)
class Match:
    method: str
    expected_path: str
    observed_path: str
    mode: str
    api: str
    host: str
    source: str


class RefResolver:
    def __init__(self, raw_base: str, timeout: float, openapi_dir: Path | None = None) -> None:
        self.raw_base = raw_base.rstrip("/") + "/"
        self.timeout = timeout
        self.openapi_dir = openapi_dir
        self.cache: dict[str, Any] = {}

    def resolve(self, ref: str) -> Any:
        ref_path, _pointer = ref.split("#", 1) if "#" in ref else (ref, "")
        ref_path = ref_path.lstrip("/")
        if ref_path in self.cache:
            return self.cache[ref_path]

        if self.openapi_dir is not None:
            path = self.openapi_dir / ref_path
            try:
                body = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(f"failed to read {path}: {exc}") from exc
        else:
            url = urljoin(self.raw_base, ref_path)
            try:
                with urlopen(url, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8")
            except (HTTPError, URLError, TimeoutError) as exc:
                raise RuntimeError(f"failed to fetch {url}: {exc}") from exc

        loaded = yaml.safe_load(body)
        self.cache[ref_path] = loaded
        return loaded


def main() -> int:
    args = parse_args()

    ref_errors: list[dict[str, str]] = []
    resolver = RefResolver(args.raw_base, args.timeout, args.openapi_dir)
    expected = []
    expected.extend(
        load_expected(
            args.read_spec,
            api="read",
            resolver=resolver,
            prefix=args.read_prefix,
            ref_errors=ref_errors,
        )
    )
    expected.extend(
        load_expected(
            args.write_spec,
            api="write",
            resolver=resolver,
            prefix=args.write_prefix,
            ref_errors=ref_errors,
        )
    )

    db_path = select_db_path(args.db, args.results_dir)
    observed: list[ObservedEndpoint] = []
    db_error: str | None = None
    forbidden_sources: dict[str, int] = {}

    if db_path is not None and db_path.exists():
        try:
            observed = load_orbis(
                db_path,
                scan_id=args.scan_id,
                host=args.host,
                sources=args.source,
                route_kind=args.route_kind,
            )
            forbidden_sources = count_forbidden_sources(
                db_path,
                scan_id=args.scan_id,
                host=args.host,
            )
        except sqlite3.Error as exc:
            db_error = str(exc)

    report = build_report(
        expected,
        observed,
        args=args,
        db_path=db_path,
        db_error=db_error,
        forbidden_sources=forbidden_sources,
        ref_errors=ref_errors,
    )
    write_report(report, args.out)

    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    if ref_errors:
        print(f"warning: {len(ref_errors)} OpenAPI refs failed to resolve", file=sys.stderr)
    if db_error:
        print(f"warning: could not read Orbis DB: {db_error}", file=sys.stderr)
    elif db_path is None or not db_path.exists():
        print("note: no NodeBB Orbis DB found; wrote oracle-only report", file=sys.stderr)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-spec", type=Path, default=DEFAULT_READ_SPEC)
    parser.add_argument("--write-spec", type=Path, default=DEFAULT_WRITE_SPEC)
    parser.add_argument("--raw-base", default=DEFAULT_RAW_BASE)
    parser.add_argument(
        "--openapi-dir",
        type=Path,
        help="Local NodeBB public/openapi directory; skips GitHub raw fetches when set",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--read-prefix", default="/api")
    parser.add_argument("--write-prefix", default="/api/v3")
    parser.add_argument("--db", type=Path, help="Optional Orbis SQLite DB to compare")
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path("benchmarks/results/nodebb"),
        help="Directory to search for a NodeBB *.db when --db is omitted",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--scan-id", type=int)
    parser.add_argument(
        "--source",
        action="append",
        help="Only compare endpoints from this Orbis source; repeat for multiple sources",
    )
    parser.add_argument(
        "--route-kind",
        default="application_api",
        help="Only compare this route_kind; use 'all' to disable",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser.parse_args()


def load_expected(
    root_path: Path,
    *,
    api: str,
    resolver: RefResolver,
    prefix: str,
    ref_errors: list[dict[str, str]],
) -> list[ExpectedEndpoint]:
    spec = load_yaml_file(root_path)
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise ValueError(f"{root_path} has no paths object")

    endpoints: list[ExpectedEndpoint] = []
    for raw_path, path_item in paths.items():
        if not isinstance(raw_path, str) or not raw_path.startswith("/"):
            continue
        if not isinstance(path_item, dict):
            continue

        ref = string_or_none(path_item.get("$ref"))
        resolved_item = path_item
        if ref:
            try:
                resolved_item = resolver.resolve(ref)
            except RuntimeError as exc:
                ref_errors.append({"api": api, "path": raw_path, "ref": ref, "error": str(exc)})
                continue
            if not isinstance(resolved_item, dict):
                ref_errors.append(
                    {
                        "api": api,
                        "path": raw_path,
                        "ref": ref,
                        "error": "resolved ref is not a path item object",
                    }
                )
                continue

        normalized = normalize_path(apply_server_prefix(api, raw_path, prefix))
        for method, operation in resolved_item.items():
            method_l = method.lower()
            if method_l not in VALID_METHODS or not isinstance(operation, dict):
                continue
            operation_obj = resolve_operation_ref(operation, resolver, api, raw_path, ref_errors)
            endpoint = ExpectedEndpoint(
                api=api,
                method=method_l.upper(),
                raw_path=raw_path,
                path_template=normalized,
                ref=ref,
                operation_id=string_or_none(operation_obj.get("operationId")),
                summary=string_or_none(operation_obj.get("summary")) or "",
                tags=tuple(t for t in operation_obj.get("tags", []) if isinstance(t, str)),
            )
            endpoints.append(endpoint)
    return sorted(endpoints, key=lambda e: (e.api, e.method, e.path_template, e.ref or ""))


def resolve_operation_ref(
    operation: dict[str, Any],
    resolver: RefResolver,
    api: str,
    raw_path: str,
    ref_errors: list[dict[str, str]],
) -> dict[str, Any]:
    ref = string_or_none(operation.get("$ref"))
    if not ref:
        return operation
    try:
        resolved = resolver.resolve(ref)
    except RuntimeError as exc:
        ref_errors.append({"api": api, "path": raw_path, "ref": ref, "error": str(exc)})
        return operation
    return resolved if isinstance(resolved, dict) else operation


def apply_server_prefix(api: str, path: str, prefix: str) -> str:
    if not prefix:
        return path
    prefix = "/" + prefix.strip("/")
    if path == prefix or path.startswith(prefix + "/"):
        return path
    if api == "read" and path in {"/ping", "/sping"}:
        return path
    return join_paths(prefix, path)


def load_yaml_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not a YAML object")
    return loaded


def select_db_path(cli_db: Path | None, results_dir: Path) -> Path | None:
    if cli_db is not None:
        return cli_db
    if not results_dir.exists():
        return None
    candidates = sorted(results_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def load_orbis(
    path: Path,
    *,
    scan_id: int | None,
    host: str | None,
    sources: list[str] | None,
    route_kind: str,
) -> list[ObservedEndpoint]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        columns = table_columns(con, "endpoint")
        select_columns = [
            "method",
            "host",
            "path_template",
            "source" if "source" in columns else "'unknown' as source",
            "route_kind" if "route_kind" in columns else "'unknown' as route_kind",
            "seen_count" if "seen_count" in columns else "1 as seen_count",
            "scan_id" if "scan_id" in columns else "NULL as scan_id",
        ]
        query = [f"select {', '.join(select_columns)} from endpoint"]
        where: list[str] = []
        params: list[Any] = []
        if scan_id is not None and "scan_id" in columns:
            where.append("scan_id = ?")
            params.append(scan_id)
        if host and "host" in columns:
            hosts = host_filter_values(host)
            placeholders = ", ".join("?" for _ in hosts)
            where.append(f"host in ({placeholders})")
            params.extend(hosts)
        if sources and "source" in columns:
            placeholders = ", ".join("?" for _ in sources)
            where.append(f"source in ({placeholders})")
            params.extend(sources)
        if route_kind != "all" and "route_kind" in columns:
            where.append("route_kind = ?")
            params.append(route_kind)
        if where:
            query.append("where " + " and ".join(where))
        query.append("order by method, path_template, host, source")
        rows = con.execute(" ".join(query), params).fetchall()
    finally:
        con.close()

    endpoints: dict[tuple[str, str, str, str, int | None], ObservedEndpoint] = {}
    for row in rows:
        endpoint = ObservedEndpoint(
            method=str(row["method"]).upper(),
            host=str(row["host"]),
            path_template=normalize_path(str(row["path_template"])),
            source=str(row["source"]),
            route_kind=str(row["route_kind"]),
            seen_count=int(row["seen_count"]),
            scan_id=row["scan_id"] if row["scan_id"] is not None else None,
        )
        key = (
            endpoint.method,
            endpoint.path_template,
            endpoint.host,
            endpoint.source,
            endpoint.scan_id,
        )
        endpoints[key] = endpoint
    return sorted(endpoints.values(), key=lambda e: (e.method, e.path_template, e.host, e.source))


def count_forbidden_sources(path: Path, *, scan_id: int | None, host: str | None) -> dict[str, int]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        columns = table_columns(con, "endpoint")
        if "source" not in columns:
            return {}
        where = [f"source in ({', '.join('?' for _ in FORBIDDEN_ORACLE_SOURCES)})"]
        params: list[Any] = sorted(FORBIDDEN_ORACLE_SOURCES)
        if scan_id is not None and "scan_id" in columns:
            where.append("scan_id = ?")
            params.append(scan_id)
        if host and "host" in columns:
            hosts = host_filter_values(host)
            placeholders = ", ".join("?" for _ in hosts)
            where.append(f"host in ({placeholders})")
            params.extend(hosts)
        rows = con.execute(
            "select source, count(*) as count from endpoint "
            f"where {' and '.join(where)} group by source order by source",
            params,
        ).fetchall()
    finally:
        con.close()
    return {str(row["source"]): int(row["count"]) for row in rows}


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    rows = con.execute(f"pragma table_info({table})").fetchall()
    if not rows:
        raise sqlite3.OperationalError(f"missing {table} table")
    return {str(row["name"]) for row in rows}


def host_filter_values(host: str) -> list[str]:
    values = [host]
    parsed = urlparse(host if "://" in host else f"http://{host}")
    if parsed.hostname and parsed.hostname not in values:
        values.append(parsed.hostname)
    return values


def build_report(
    expected: list[ExpectedEndpoint],
    observed: list[ObservedEndpoint],
    *,
    args: argparse.Namespace,
    db_path: Path | None,
    db_error: str | None,
    forbidden_sources: dict[str, int],
    ref_errors: list[dict[str, str]],
) -> dict[str, Any]:
    oracle_summary = summarize_expected(expected)
    db_exists = db_path is not None and db_path.exists()
    comparison = calculate_coverage(expected, observed) if db_exists and not db_error else None
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "read_spec": str(args.read_spec),
            "write_spec": str(args.write_spec),
            "raw_base": args.raw_base,
            "openapi_dir": str(args.openapi_dir) if args.openapi_dir else None,
            "read_prefix": args.read_prefix,
            "write_prefix": args.write_prefix,
            "db": str(db_path) if db_path else None,
            "host": args.host,
            "scan_id": args.scan_id,
            "source": args.source,
            "route_kind": args.route_kind,
        },
        "summary": {
            **oracle_summary,
            "db_exists": db_exists,
            "observed_count": len(observed),
            "coverage": comparison["summary"] if comparison else None,
            "ref_error_count": len(ref_errors),
            "forbidden_oracle_sources_present": forbidden_sources,
            "benchmark_rule_ok": not forbidden_sources,
        },
        "expected_endpoints": [asdict(endpoint) for endpoint in expected],
        "ref_errors": ref_errors,
        "comparison": comparison,
    }
    if db_error:
        report["db_error"] = db_error
    elif not db_exists:
        report["db_note"] = "No Orbis SQLite DB was provided or discovered; oracle-only report."
    return report


def summarize_expected(expected: list[ExpectedEndpoint]) -> dict[str, Any]:
    normalized_keys = {(endpoint.method, endpoint.path_template) for endpoint in expected}
    return {
        "oracle_operation_count": len(expected),
        "oracle_unique_normalized_endpoint_count": len(normalized_keys),
        "oracle_by_api": dict(sorted(Counter(endpoint.api for endpoint in expected).items())),
        "oracle_by_method": dict(sorted(Counter(endpoint.method for endpoint in expected).items())),
        "oracle_by_api_method": {
            api: dict(sorted(Counter(e.method for e in expected if e.api == api).items()))
            for api in sorted({endpoint.api for endpoint in expected})
        },
    }


def calculate_coverage(
    expected: list[ExpectedEndpoint],
    observed: list[ObservedEndpoint],
) -> dict[str, Any]:
    expected_keys = {(e.method, e.path_template) for e in expected}
    observed_by_key: dict[tuple[str, str], list[ObservedEndpoint]] = {}
    for endpoint in observed:
        observed_by_key.setdefault((endpoint.method, endpoint.path_template), []).append(endpoint)

    exact_matches: list[Match] = []
    shape_matches: list[Match] = []
    missing: list[ExpectedEndpoint] = []
    used_observed_keys: set[tuple[str, str]] = set()
    exact_observed_keys = expected_keys & set(observed_by_key)

    for endpoint in expected:
        key = (endpoint.method, endpoint.path_template)
        exact_candidates = observed_by_key.get(key, [])
        if exact_candidates:
            observed_endpoint = exact_candidates[0]
            exact_matches.append(match_for(endpoint, observed_endpoint, "exact"))
            used_observed_keys.add(key)
            continue

        shape_candidate = first_shape_candidate(endpoint, observed, exact_observed_keys)
        if shape_candidate is not None:
            shape_matches.append(match_for(endpoint, shape_candidate, "shape"))
            used_observed_keys.add((shape_candidate.method, shape_candidate.path_template))
        else:
            missing.append(endpoint)

    extra = [
        endpoint
        for endpoint in observed
        if (endpoint.method, endpoint.path_template) not in expected_keys
        and (endpoint.method, endpoint.path_template) not in used_observed_keys
    ]

    denominator = len(expected)
    exact_count = len(exact_matches)
    effective_count = exact_count + len(shape_matches)
    return {
        "summary": {
            "expected_operation_count": denominator,
            "expected_unique_normalized_endpoint_count": len(expected_keys),
            "observed_count": len(observed),
            "exact_matches": exact_count,
            "shape_matches": len(shape_matches),
            "missing_count": len(missing),
            "extra_count": len(extra),
            "exact_coverage": ratio(exact_count, denominator),
            "effective_coverage": ratio(effective_count, denominator),
            "observed_source_counts": dict(sorted(Counter(e.source for e in observed).items())),
            "observed_method_counts": dict(sorted(Counter(e.method for e in observed).items())),
        },
        "exact_matches": [asdict(match) for match in exact_matches],
        "shape_matches": [asdict(match) for match in shape_matches],
        "missing": [asdict(endpoint) for endpoint in missing],
        "extra": [asdict(endpoint) for endpoint in extra],
    }


def match_for(expected: ExpectedEndpoint, observed: ObservedEndpoint, mode: str) -> Match:
    return Match(
        method=expected.method,
        expected_path=expected.path_template,
        observed_path=observed.path_template,
        mode=mode,
        api=expected.api,
        host=observed.host,
        source=observed.source,
    )


def first_shape_candidate(
    expected: ExpectedEndpoint,
    observed: list[ObservedEndpoint],
    exact_observed_keys: set[tuple[str, str]],
) -> ObservedEndpoint | None:
    if not any(is_param_segment(segment) for segment in split_path(expected.path_template)):
        return None

    for endpoint in observed:
        key = (endpoint.method, endpoint.path_template)
        if endpoint.method != expected.method:
            continue
        if key in exact_observed_keys:
            continue
        if path_shape_matches(expected.path_template, endpoint.path_template):
            return endpoint
    return None


def path_shape_matches(expected_path: str, observed_path: str) -> bool:
    expected_parts = split_path(expected_path)
    observed_parts = split_path(observed_path)
    if len(expected_parts) != len(observed_parts):
        return False

    for expected, observed in zip(expected_parts, observed_parts):
        if expected == observed:
            continue
        if is_param_segment(expected):
            continue
        return False
    return True


def normalize_path(path: str) -> str:
    parsed = urlparse(path)
    if parsed.scheme and parsed.netloc:
        path = parsed.path
    path = path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    path = re.sub(r"/+", "/", path)
    path = path.rstrip("/") or "/"
    path = OPENAPI_PARAM_RE.sub("{id}", path)
    path = COLON_PARAM_RE.sub("{id}", path)
    path = templatize_path(path)
    return path


def templatize_path(path: str) -> str:
    if not path:
        return path
    return "/".join(replace_segment(segment) if segment else segment for segment in path.split("/"))


def replace_segment(segment: str) -> str:
    if is_param_segment(segment):
        return "{id}"
    if segment == "*":
        return "{id}"
    if UUID_RE.match(segment):
        return "{id}"
    if DATE_RE.match(segment):
        return "{id}"
    if SLUG_RE.match(segment):
        return "{id}"
    if LONG_HEX_RE.match(segment):
        return "{id}"
    if NUMERIC_RE.match(segment):
        return "{id}"
    return EMBEDDED_NUM_RE.sub("{n}", segment)


def join_paths(base_path: str, path: str) -> str:
    if not base_path:
        return path
    if path == "/":
        return base_path
    return "/" + "/".join(part.strip("/") for part in (base_path, path) if part.strip("/"))


def split_path(path: str) -> list[str]:
    return [part for part in path.strip("/").split("/") if part]


def is_param_segment(segment: str) -> bool:
    return bool(PARAM_SEGMENT_RE.match(segment))


def string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return round(numerator / denominator, 6)


def write_report(report: dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=out.parent, delete=False) as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")
        tmp_name = f.name
    Path(tmp_name).replace(out)
    out.chmod(0o644)


if __name__ == "__main__":
    raise SystemExit(main())
