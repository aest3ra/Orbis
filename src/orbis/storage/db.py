"""SQLite schema."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel, create_engine


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Scan(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    target: str
    auth_state_path: str | None = None
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    pages_crawled: int = 0
    endpoints_found: int = 0


class Endpoint(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "scan_id", "method", "host", "path_template",
            name="uq_endpoint",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    scan_id: int = Field(foreign_key="scan.id", index=True)
    method: str
    host: str
    path_template: str
    sample_url: str
    route_kind: str
    seen_count: int = 1


class Parameter(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "endpoint_id", "location", "name",
            name="uq_param",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    endpoint_id: int = Field(foreign_key="endpoint.id", index=True)
    location: str
    name: str
    type_inferred: str
    sample_values_json: str = "[]"
    seen_count: int = 1


def open_db(path: str | Path):
    engine = create_engine(f"sqlite:///{Path(path).resolve()}", echo=False)
    SQLModel.metadata.create_all(engine)
    return engine
