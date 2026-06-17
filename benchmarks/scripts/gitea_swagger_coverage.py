#!/usr/bin/env python3
"""Compare Gitea Swagger/OpenAPI JSON coverage against an Orbis SQLite DB.

The denominator is the normalized Swagger operation set. Observed Orbis rows
come from the endpoint table, optionally filtered by scan, host, source, and
route_kind. Matching is reported in two tiers:

* exact: same METHOD and normalized path_template
* shape: expected path parameters match one observed concrete or templated segment
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


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


@dataclass(frozen=True)
class ExpectedEndpoint:
    method: str
    path_template: str
    operation_id: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObservedEndpoint:
    method: str
    host: str
    path_template: str
    source: str
    route_kind: str
    seen_count: int


@dataclass(frozen=True)
class Match:
    method: str
    expected_path: str
    observed_path: str
    mode: str
    host: str
    source: str


def main() -> int:
    args = parse_args()
    expected = load_swagger(args.swagger_json, include_base_path=not args.ignore_base_path)
    observed = load_orbis(
        args.orbis_db,
        scan_id=args.scan_id,
        host=args.host,
        sources=args.source,
        route_kind=args.route_kind,
    )

    report = calculate_coverage(expected, observed)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_text_report(report, limit=args.limit)

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("swagger_json", type=Path, help="Gitea Swagger/OpenAPI JSON file")
    parser.add_argument("orbis_db", type=Path, help="Orbis SQLite DB path")
    parser.add_argument("--scan-id", type=int, help="Only compare endpoints from this scan_id")
    parser.add_argument("--host", help="Only compare Orbis endpoints for this hostname")
    parser.add_argument(
        "--source",
        action="append",
        help="Only compare Orbis endpoints from this source; repeat for multiple sources",
    )
    parser.add_argument(
        "--route-kind",
        default="application_api",
        help="Only compare this route_kind; use 'all' to disable",
    )
    parser.add_argument(
        "--ignore-base-path",
        action="store_true",
        help="Do not prepend Swagger basePath or OpenAPI servers path to expected paths",
    )
    parser.add_argument("--json", action="store_true", help="Emit full JSON report")
    parser.add_argument("--limit", type=int, default=25, help="Rows to print per text section")
    return parser.parse_args()


def load_swagger(path: Path, *, include_base_path: bool) -> list[ExpectedEndpoint]:
    with path.open("r", encoding="utf-8") as f:
        spec = json.load(f)
    if not isinstance(spec, dict):
        raise ValueError(f"{path} is not a Swagger/OpenAPI object")

    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise ValueError(f"{path} has no paths object")

    base_path = extract_base_path(spec) if include_base_path else ""
    endpoints: dict[tuple[str, str], ExpectedEndpoint] = {}
    for raw_path, path_item in paths.items():
        if not isinstance(raw_path, str) or not raw_path.startswith("/"):
            continue
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            method_l = method.lower()
            if method_l not in VALID_METHODS or not isinstance(operation, dict):
                continue
            full_path = join_paths(base_path, raw_path)
            endpoint = ExpectedEndpoint(
                method=method_l.upper(),
                path_template=normalize_path(full_path),
                operation_id=string_or_none(operation.get("operationId")),
                tags=tuple(t for t in operation.get("tags", []) if isinstance(t, str)),
            )
            endpoints[(endpoint.method, endpoint.path_template)] = endpoint
    return sorted(endpoints.values(), key=lambda e: (e.method, e.path_template))


def extract_base_path(spec: dict[str, Any]) -> str:
    base_path = spec.get("basePath")
    if isinstance(base_path, str) and base_path and base_path != "/":
        return base_path.rstrip("/")

    servers = spec.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        url = servers[0].get("url")
        if isinstance(url, str) and url:
            if url.startswith(("http://", "https://")):
                parsed_path = urlparse(url).path.rstrip("/")
                return "" if parsed_path == "/" else parsed_path
            if url.startswith("/"):
                return url.rstrip("/")
    return ""


def load_orbis(
    path: Path,
    *,
    scan_id: int | None,
    host: str | None,
    sources: list[str] | None,
    route_kind: str,
) -> list[ObservedEndpoint]:
    query = [
        "select method, host, path_template, source, route_kind, seen_count",
        "from endpoint",
    ]
    where: list[str] = []
    params: list[Any] = []
    if scan_id is not None:
        where.append("scan_id = ?")
        params.append(scan_id)
    if host:
        where.append("host = ?")
        params.append(host)
    if sources:
        placeholders = ", ".join("?" for _ in sources)
        where.append(f"source in ({placeholders})")
        params.extend(sources)
    if route_kind != "all":
        where.append("route_kind = ?")
        params.append(route_kind)
    if where:
        query.append("where " + " and ".join(where))
    query.append("order by method, path_template, host, source")

    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(" ".join(query), params).fetchall()
    finally:
        con.close()

    endpoints: dict[tuple[str, str, str, str], ObservedEndpoint] = {}
    for row in rows:
        endpoint = ObservedEndpoint(
            method=str(row["method"]).upper(),
            host=str(row["host"]),
            path_template=normalize_path(str(row["path_template"])),
            source=str(row["source"]),
            route_kind=str(row["route_kind"]),
            seen_count=int(row["seen_count"]),
        )
        endpoints[(endpoint.method, endpoint.path_template, endpoint.host, endpoint.source)] = endpoint
    return sorted(endpoints.values(), key=lambda e: (e.method, e.path_template, e.host, e.source))


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
            exact_matches.append(
                Match(
                    method=endpoint.method,
                    expected_path=endpoint.path_template,
                    observed_path=observed_endpoint.path_template,
                    mode="exact",
                    host=observed_endpoint.host,
                    source=observed_endpoint.source,
                )
            )
            used_observed_keys.add(key)
            continue

        shape_candidate = first_shape_candidate(endpoint, observed, exact_observed_keys)
        if shape_candidate is not None:
            shape_matches.append(
                Match(
                    method=endpoint.method,
                    expected_path=endpoint.path_template,
                    observed_path=shape_candidate.path_template,
                    mode="shape",
                    host=shape_candidate.host,
                    source=shape_candidate.source,
                )
            )
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
            "expected_count": denominator,
            "observed_count": len(observed),
            "exact_matches": exact_count,
            "shape_matches": len(shape_matches),
            "missing_count": len(missing),
            "extra_count": len(extra),
            "exact_coverage": ratio(exact_count, denominator),
            "effective_coverage": ratio(effective_count, denominator),
        },
        "exact_matches": [asdict(match) for match in exact_matches],
        "shape_matches": [asdict(match) for match in shape_matches],
        "missing": [asdict(endpoint) for endpoint in missing],
        "extra": [asdict(endpoint) for endpoint in extra],
    }


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


def print_text_report(report: dict[str, Any], *, limit: int) -> None:
    summary = report["summary"]
    print(f"Expected Swagger operations: {summary['expected_count']}")
    print(f"Observed Orbis endpoints:   {summary['observed_count']}")
    print(
        "Exact coverage:             "
        f"{summary['exact_matches']}/{summary['expected_count']} "
        f"({summary['exact_coverage']:.2%})"
    )
    print(
        "Effective coverage:         "
        f"{summary['exact_matches'] + summary['shape_matches']}/{summary['expected_count']} "
        f"({summary['effective_coverage']:.2%})"
    )
    print(f"Missing expected endpoints: {summary['missing_count']}")
    print(f"Extra observed endpoints:   {summary['extra_count']}")

    print_rows("Shape-only matches", report["shape_matches"], limit)
    print_rows("Missing", report["missing"], limit)
    print_rows("Extra", report["extra"], limit)


def print_rows(title: str, rows: list[dict[str, Any]], limit: int) -> None:
    if not rows:
        return
    print(f"\n{title} (first {min(limit, len(rows))} of {len(rows)}):")
    for row in rows[:limit]:
        method = row.get("method", "")
        if "expected_path" in row:
            print(
                f"  {method:7} {row['expected_path']} -> {row['observed_path']} "
                f"[{row['source']} {row['host']}]"
            )
        elif "path_template" in row:
            details = []
            if row.get("source"):
                details.append(str(row["source"]))
            if row.get("host"):
                details.append(str(row["host"]))
            suffix = f" [{' '.join(details)}]" if details else ""
            print(f"  {method:7} {row['path_template']}{suffix}")


if __name__ == "__main__":
    raise SystemExit(main())
