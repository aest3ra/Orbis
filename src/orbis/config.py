"""Configuration models and YAML loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel


class ScopeConfig(BaseModel):
    include_domains: list[str] = []
    exclude_paths: list[str] = []


class AuthConfig(BaseModel):
    type: str = "none"
    storage_state_path: Path | None = None


class LimitsConfig(BaseModel):
    max_pages: int = 100
    max_depth: int | None = None
    max_duration_sec: int = 600
    max_visits_per_template: int = 5
    max_scrolls_per_page: int = 3
    rate_limit_rps: float = 2.0
    # Cardinality-based slug detection (crawl-time templating).
    # A path position with this many distinct observed values becomes {slug}.
    slug_threshold: int = 8
    # Diminishing-returns stop: after this many consecutive visits to one
    # template yield no new endpoints, stop visiting that template.
    template_saturation: int = 3
    # Max archived URLs to pull per host from Wayback.
    wayback_max_urls: int = 5000
    # Max active probe requests actually sent after safety skips.
    probe_max_requests: int = 500
    probe_timeout_sec: int = 10


class ScanConfig(BaseModel):
    target: str
    scope: ScopeConfig = ScopeConfig()
    auth: AuthConfig = AuthConfig()
    limits: LimitsConfig = LimitsConfig()

    def model_post_init(self, __context: Any) -> None:
        if not self.scope.include_domains:
            host = urlparse(self.target).hostname
            if host:
                self.scope.include_domains = [host]

def load_config(path: Path) -> ScanConfig:
    with open(path) as f:
        data = yaml.safe_load(f)
    return ScanConfig.model_validate(data)
