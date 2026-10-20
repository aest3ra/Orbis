"""Scope check: is a URL inside the allowed crawl boundary?"""

from __future__ import annotations

import fnmatch
from urllib.parse import urlparse

from orbis.analysis.url import DEFAULT_PORTS, effective_port
from orbis.config import ScopeConfig


class Scope:
    def __init__(self, config: ScopeConfig, target: str | None = None) -> None:
        self._domains = []
        for domain in config.include_domains:
            if domain.count(":") > 1 and not domain.startswith("["):
                self._domains.append((domain.lower(), None))
                continue
            parsed = urlparse(f"//{domain}")
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError(f"include_domains entry {domain!r} has an invalid port") from exc
            self._domains.append(((parsed.hostname or "").lower(), port))
        self._target_port = effective_port(urlparse(target or ""))
        self._exclude = config.exclude_paths

    def allows(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in DEFAULT_PORTS:
            return False
        host = (parsed.hostname or "").lower()
        try:
            port = parsed.port
        except ValueError:
            return False  # a port that is not a number or is out of range: not a URL we crawl
        if port is None:
            port = DEFAULT_PORTS[parsed.scheme]
        allowed = False
        for domain, allowed_port in self._domains:
            expected_port = allowed_port if allowed_port is not None else self._target_port
            if fnmatch.fnmatch(host, domain) and (expected_port is None or port == expected_port):
                allowed = True
                break
        if not allowed:
            return False
        path = parsed.path or "/"
        return not any(path.startswith(p) for p in self._exclude)
