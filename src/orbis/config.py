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


CRAWL_PRESETS: dict[str, dict[str, int]] = {
    "quick": {
        "max_pages": 50,
        "max_duration_sec": 300,
        "max_visits_per_template": 3,
        "max_scrolls_per_page": 2,
    },
    "deep": {
        "max_pages": 200,
        "max_duration_sec": 900,
        "max_visits_per_template": 10,
        "max_scrolls_per_page": 5,
    },
    "exhaustive": {
        "max_pages": 1000,
        "max_duration_sec": 3600,
        "max_visits_per_template": 30,
        "max_scrolls_per_page": 5,
    },
}


def load_config(path: Path) -> ScanConfig:
    with open(path) as f:
        data = yaml.safe_load(f)
    return ScanConfig.model_validate(data)
