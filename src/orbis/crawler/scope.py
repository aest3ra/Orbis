"""Scope check: is a URL inside the allowed crawl boundary?"""

from __future__ import annotations

import fnmatch
from urllib.parse import urlparse

from orbis.config import ScopeConfig


class Scope:
    def __init__(self, config: ScopeConfig) -> None:
        self._domains = [d.lower() for d in config.include_domains]
        self._exclude = config.exclude_paths

    def allows(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https", "ws", "wss"):
            return False
        host = (parsed.hostname or "").lower()
        if not any(fnmatch.fnmatch(host, pat) for pat in self._domains):
            return False
        path = parsed.path or "/"
        return not any(path.startswith(p) for p in self._exclude)
