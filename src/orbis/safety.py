"""Single source of truth for URL safety checks."""

from __future__ import annotations

import re
from urllib.parse import urlparse

DANGER_KEYWORDS = frozenset({
    "logout", "signout", "sign-out", "log-out",
    "delete", "remove", "destroy", "unsubscribe",
    "deactivate", "close-account", "cancel-account",
    "로그아웃", "삭제", "탈퇴",
})

DOWNLOAD_EXTENSIONS = frozenset({
    ".zip", ".tar", ".gz", ".rar", ".7z",
    ".exe", ".msi", ".dmg", ".pkg",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
})

_SEG_SPLIT = re.compile(r"[-_]")


def is_dangerous_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    for seg in path.split("/"):
        if not seg:
            continue
        if seg in DANGER_KEYWORDS:
            return True
        normalized = seg.replace("_", "-")
        if normalized in DANGER_KEYWORDS:
            return True
        parts = _SEG_SPLIT.split(seg)
        if len(parts) > 1 and any(p in DANGER_KEYWORDS for p in parts):
            return True
    return False


def is_download_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in DOWNLOAD_EXTENSIONS)


def is_safe_url(url: str) -> bool:
    return not is_dangerous_url(url) and not is_download_url(url)


def text_has_danger_keyword(text: str, extra: frozenset[str] = frozenset()) -> bool:
    """Danger-keyword check for free UI text (button labels, form text).

    Unlike is_dangerous_url's path-segment matching, free text — CJK in
    particular, which has no reliable word boundaries — is matched by
    substring. Centralized here so interaction callers share one keyword set
    instead of maintaining a parallel copy that can drift.
    """
    normalized = text.lower().replace("_", "-")
    return any(keyword in normalized for keyword in (DANGER_KEYWORDS | extra))
