"""DB save/load helpers."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlmodel import Session, select

from orbis.analysis.analyzer import NormalizedEndpoint, _SOURCE_PRIORITY
from orbis.storage.db import Endpoint, Parameter, Scan

log = logging.getLogger("orbis.storage")


def create_scan(session: Session, target: str, auth_path: str | None = None) -> int:
    scan = Scan(target=target, auth_state_path=auth_path)
    session.add(scan)
    session.commit()
    session.refresh(scan)
    return scan.id  # type: ignore[return-value]


def finish_scan(session: Session, scan_id: int, pages: int, endpoints: int) -> None:
    scan = session.get(Scan, scan_id)
    if scan:
        scan.finished_at = datetime.now(timezone.utc)
        scan.pages_crawled = pages
        scan.endpoints_found = endpoints
        session.commit()


def save_endpoints(
    session: Session,
    scan_id: int,
    endpoints: list[NormalizedEndpoint],
) -> tuple[int, int]:
    """Batch-save all endpoints in a single transaction. Returns (added, updated)."""
    added = updated = 0
    for ep in endpoints:
        row = session.exec(
            select(Endpoint).where(
                Endpoint.scan_id == scan_id,
                Endpoint.method == ep.method,
                Endpoint.host == ep.host,
                Endpoint.path_template == ep.path_template,
            )
        ).first()

        # Derive probe_status from source:
        # dynamic → NULL (already observed), static_* → "unverified"
        probe_status = None if ep.source == "dynamic" else "unverified"

        if row:
            row.seen_count += ep.seen_count
            if _SOURCE_PRIORITY.get(ep.source, 99) < _SOURCE_PRIORITY.get(row.source, 99):
                row.source = ep.source
                row.probe_status = probe_status
                row.sample_url = ep.sample_url
            # Passive reachability wins: a plain-load sighting on any page
            # clears the interaction tag.
            if ep.discovered_via is None:
                row.discovered_via = None
            session.flush()
            ep_id = row.id
            updated += 1
        else:
            row = Endpoint(
                scan_id=scan_id,
                method=ep.method,
                host=ep.host,
                path_template=ep.path_template,
                sample_url=ep.sample_url,
                route_kind=ep.route_kind,
                seen_count=ep.seen_count,
                source=ep.source,
                probe_status=probe_status,
                discovered_via=ep.discovered_via,
            )
            session.add(row)
            session.flush()
            ep_id = row.id
            added += 1

        _save_params(session, ep_id, ep)  # type: ignore[arg-type]

    session.commit()
    return added, updated


def _save_params(session: Session, endpoint_id: int, ep: NormalizedEndpoint) -> None:
    for (loc, name), param in ep.params.items():
        existing = session.exec(
            select(Parameter).where(
                Parameter.endpoint_id == endpoint_id,
                Parameter.location == loc,
                Parameter.name == name,
            )
        ).first()
        samples = json.dumps(param.sample_values)
        if existing:
            existing.seen_count += param.seen_count
            existing.type_inferred = param.type_inferred
            existing.sample_values_json = samples
        else:
            session.add(Parameter(
                endpoint_id=endpoint_id,
                location=loc,
                name=name,
                type_inferred=param.type_inferred,
                sample_values_json=samples,
                seen_count=param.seen_count,
            ))


def collapse_scan_endpoints(
    session: Session,
    scan_id: int,
    threshold: int = 3,
) -> int:
    """Post-scan: collapse high-cardinality sibling endpoints.

    When multiple endpoints share the same (method, host, parent_path)
    and differ only in the last path segment, they are merged into one
    endpoint with a ``{slug}`` placeholder.  Root-level paths (prefix is
    empty) are never collapsed to avoid merging unrelated top-level pages
    like ``/about`` and ``/contact``.

    Returns the number of endpoints removed by merging.
    """
    endpoints = list(
        session.exec(select(Endpoint).where(Endpoint.scan_id == scan_id)).all()
    )

    # Group by (method, host, parent_path) — skip already-templatized last segments
    # and root-level paths (empty prefix).
    groups: dict[tuple[str, str, str], list[Endpoint]] = defaultdict(list)
    for ep in endpoints:
        parts = ep.path_template.rsplit("/", 1)
        if len(parts) == 2 and parts[0] and not parts[1].startswith("{"):
            key = (ep.method, ep.host, parts[0])
            groups[key].append(ep)

    merged = 0
    for key, eps in groups.items():
        if len(eps) < threshold:
            continue
        last_segs = {e.path_template.rsplit("/", 1)[-1] for e in eps}
        if len(last_segs) < threshold:
            continue

        method, host, prefix = key
        survivor = eps[0]
        survivor.path_template = f"{prefix}/{{slug}}"
        survivor.seen_count = sum(e.seen_count for e in eps)

        for ep in eps[1:]:
            # Migrate unique params to survivor, then delete the duplicate.
            for param in session.exec(
                select(Parameter).where(Parameter.endpoint_id == ep.id)
            ).all():
                existing = session.exec(
                    select(Parameter).where(
                        Parameter.endpoint_id == survivor.id,
                        Parameter.location == param.location,
                        Parameter.name == param.name,
                    )
                ).first()
                if existing:
                    existing.seen_count += param.seen_count
                session.delete(param)
            session.delete(ep)
            merged += 1

    if merged:
        session.commit()
        log.info("collapsed %d sibling endpoints into {slug} templates", merged)

    return merged


def list_endpoints(session: Session, scan_id: int | None = None) -> list[Endpoint]:
    q = select(Endpoint)
    if scan_id is not None:
        q = q.where(Endpoint.scan_id == scan_id)
    return list(session.exec(q.order_by(Endpoint.id)).all())


def get_endpoint_with_params(
    session: Session, endpoint_id: int,
) -> tuple[Endpoint | None, list[Parameter]]:
    ep = session.get(Endpoint, endpoint_id)
    if ep is None:
        return None, []
    params = list(session.exec(
        select(Parameter)
        .where(Parameter.endpoint_id == endpoint_id)
        .order_by(Parameter.location, Parameter.name)
    ).all())
    return ep, params
