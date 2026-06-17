from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
PATH_PARAM_RE = re.compile(r"\{[^}]+\}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    oracle = load_oracle(Path(args.spec))
    found = load_orbis(Path(args.db), args.host)
    found_keys = {endpoint_key(ep) for ep in found}

    rows = []
    for ep in oracle:
        key = endpoint_key(ep)
        present = key in found_keys
        rows.append({
            **ep,
            "found_by_orbis": present,
            "miss_reason": None if present else miss_reason(ep),
        })

    eligible = [row for row in rows if row["v1_eligible"]]
    report = {
        "summary": summarize(rows, eligible, found),
        "miss_reasons": Counter(
            row["miss_reason"] for row in eligible
            if not row["found_by_orbis"]
        ),
        "oracle_method_counts": Counter(row["method"] for row in rows),
        "orbis_source_counts": Counter(ep["source"] for ep in found),
        "missed_eligible": [
            row for row in eligible
            if not row["found_by_orbis"]
        ],
        "excluded": [
            row for row in rows
            if not row["v1_eligible"]
        ],
        "orbis_only": sorted(
            found_keys - {endpoint_key(ep) for ep in oracle}
        ),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))


def load_oracle(path: Path) -> list[dict[str, Any]]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    base_path = str(spec.get("basePath") or "").rstrip("/")
    rows = []
    for raw_path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict) or not str(raw_path).startswith("/"):
            continue
        path_template = normalize_path(f"{base_path}{raw_path}")
        for method, operation in item.items():
            if method.lower() not in METHODS or not isinstance(operation, dict):
                continue
            rows.append({
                "method": method.upper(),
                "path_template": path_template,
                "summary": operation.get("summary") or "",
                "v1_eligible": True,
                "excluded_reason": None,
            })
    return rows


def load_orbis(path: Path, host: str) -> list[dict[str, str]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        select method, host, path_template, source
        from endpoint
        where host = ?
        order by method, path_template
        """,
        (host,),
    ).fetchall()
    return [dict(row) for row in rows]


def endpoint_key(ep: dict[str, Any]) -> str:
    return f"{ep['method']} {normalize_path(ep['path_template'])}"


def normalize_path(path: str) -> str:
    path = PATH_PARAM_RE.sub("{id}", path)
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/"


def summarize(
    rows: list[dict[str, Any]],
    eligible: list[dict[str, Any]],
    found: list[dict[str, str]],
) -> dict[str, Any]:
    found_total = sum(1 for row in rows if row["found_by_orbis"])
    found_eligible = sum(1 for row in eligible if row["found_by_orbis"])
    return {
        "oracle_total": len(rows),
        "oracle_v1_eligible": len(eligible),
        "orbis_found_for_host": len(found),
        "found_total_oracle": found_total,
        "found_v1_eligible": found_eligible,
        "total_recall": ratio(found_total, len(rows)),
        "v1_eligible_recall": ratio(found_eligible, len(eligible)),
    }


def ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)


def miss_reason(ep: dict[str, Any]) -> str:
    if ep.get("excluded_reason"):
        return str(ep["excluded_reason"])
    return "openapi_not_collected_or_not_parsed"


if __name__ == "__main__":
    main()
