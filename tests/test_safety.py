"""Tests for orbis.safety — segment-level URL safety checks."""

import pytest

from orbis.safety import is_dangerous_url, is_download_url, is_safe_url


class TestIsDangerousUrl:
    """Segment-based matching: exact segment or hyphen/underscore-separated part."""

    # --- Should be BLOCKED (dangerous) ---

    @pytest.mark.parametrize("url", [
        "https://example.com/logout",
        "https://example.com/api/logout",
        "https://example.com/auth/sign-out",
        "https://example.com/user/delete",
        "https://example.com/account/remove",
        "https://example.com/account/destroy",
        "https://example.com/settings/deactivate",
        "https://example.com/settings/close-account",
        "https://example.com/unsubscribe",
    ])
    def test_blocks_exact_danger_segments(self, url: str) -> None:
        assert is_dangerous_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://example.com/delete-user",
        "https://example.com/api/delete-account",
        "https://example.com/remove-item",
        "https://example.com/destroy-session",
        "https://example.com/cancel_account",
        "https://example.com/log-out/confirm",
    ])
    def test_blocks_compound_danger_segments(self, url: str) -> None:
        assert is_dangerous_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://example.com/로그아웃",
        "https://example.com/api/삭제",
        "https://example.com/회원/탈퇴",
    ])
    def test_blocks_korean_danger_keywords(self, url: str) -> None:
        assert is_dangerous_url(url) is True

    # --- Should be ALLOWED (not dangerous) ---

    @pytest.mark.parametrize("url", [
        "https://example.com/undelete",
        "https://example.com/api/undelete",
        "https://example.com/deleted-items",
        "https://example.com/remover",
        "https://example.com/removeall",
        "https://example.com/logoutside",
    ])
    def test_allows_non_matching_substrings(self, url: str) -> None:
        assert is_dangerous_url(url) is False

    @pytest.mark.parametrize("url", [
        "https://example.com/",
        "https://example.com/api/users",
        "https://example.com/dashboard",
        "https://example.com/profile/settings",
        "https://example.com/api/v1/items/123",
    ])
    def test_allows_normal_urls(self, url: str) -> None:
        assert is_dangerous_url(url) is False


class TestIsDownloadUrl:
    @pytest.mark.parametrize("url", [
        "https://example.com/file.zip",
        "https://example.com/report.pdf",
        "https://example.com/installer.exe",
        "https://example.com/data.xlsx",
        "https://example.com/archive.tar",
    ])
    def test_blocks_download_extensions(self, url: str) -> None:
        assert is_download_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://example.com/api/zip-codes",
        "https://example.com/page.html",
        "https://example.com/script.js",
    ])
    def test_allows_non_download_urls(self, url: str) -> None:
        assert is_download_url(url) is False


class TestIsSafeUrl:
    def test_dangerous_url_is_unsafe(self) -> None:
        assert is_safe_url("https://example.com/delete") is False

    def test_download_url_is_unsafe(self) -> None:
        assert is_safe_url("https://example.com/file.zip") is False

    def test_normal_url_is_safe(self) -> None:
        assert is_safe_url("https://example.com/api/users") is True
