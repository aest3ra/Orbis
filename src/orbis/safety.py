"""Single source of truth for URL safety checks."""

from __future__ import annotations

from urllib.parse import urlparse

DANGER_KEYWORDS = {
    "logout", "signout", "sign-out", "log-out",
    "delete", "remove", "destroy", "unsubscribe",
    "deactivate", "close-account", "cancel-account",
    "로그아웃", "삭제", "탈퇴",
}

DOWNLOAD_EXTENSIONS = {
    ".zip", ".tar", ".gz", ".rar", ".7z",
    ".exe", ".msi", ".dmg", ".pkg",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
}


def is_dangerous_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(kw in path for kw in DANGER_KEYWORDS)


def is_download_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in DOWNLOAD_EXTENSIONS)


def is_safe_url(url: str) -> bool:
    return not is_dangerous_url(url) and not is_download_url(url)
