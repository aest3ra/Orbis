"""DB save/load helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import inspect
from sqlmodel import Session, select

from orbis.analysis.analyzer import MAX_SAMPLES, NormalizedEndpoint, _SOURCE_PRIORITY
from orbis.analysis.params import merge_types
from orbis.storage.db import Endpoint, Parameter, Scan

def create_scan(session: Session, target: str, auth_path: str | None = None) -> int:
    scan = Scan(target=target, auth_state_path=auth_path)
    session.add(scan)
    session.commit()
    return inspect(scan).identity[0]


def finish_scan(session: Session, scan_id: int, pages: int, endpoints: int) -> None:
    scan = session.get(Scan, scan_id)
    if scan:
        scan.finished_at = datetime.now(timezone.utc)
        scan.pages_crawled = pages
        scan.endpoints_found = endpoints
        session.commit()


def set_probe_result(
    session: Session,
    endpoint_id: int,
    status: str,
    code: int | None,
) -> None:
    ep = session.get(Endpoint, endpoint_id)
    if ep:
        ep.probe_status = status
        ep.probe_code = code


def list_unverified_endpoints(session: Session, scan_id: int) -> list[Endpoint]:
    return list(session.exec(
        select(Endpoint)
        .where(
            Endpoint.scan_id == scan_id,
            Endpoint.probe_status == "unverified",
        )
        .order_by(Endpoint.id)
    ).all())


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
                row.probe_code = None
                row.sample_url = ep.sample_url
                row.route_kind = ep.route_kind
                row.discovered_via = ep.discovered_via
            # Page-load reachability wins: a plain-load sighting on any page
            # clears the interaction tag.
            if ep.source == "dynamic" and ep.discovered_via is None:
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
            existing.type_inferred = merge_types(existing.type_inferred, param.type_inferred)
            existing.sample_values_json = _merge_samples(existing.sample_values_json, samples)
        else:
            session.add(Parameter(
                endpoint_id=endpoint_id,
                location=loc,
                name=name,
                type_inferred=param.type_inferred,
                sample_values_json=samples,
                seen_count=param.seen_count,
            ))


def _merge_samples(first: str, second: str) -> str:
    values = dict.fromkeys(json.loads(first) + json.loads(second))
    return json.dumps(list(values)[:MAX_SAMPLES])


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
