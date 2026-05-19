"""Tests for orbis.analysis.url — path templatization."""

import pytest

from orbis.analysis.url import templatize_path


class TestTemplatizePath:
    # --- Numeric IDs ---

    @pytest.mark.parametrize("path, expected", [
        ("/user/123", "/user/{id}"),
        ("/user/123/posts/456", "/user/{id}/posts/{id}"),
        ("/api/v1/items/99", "/api/v1/items/{id}"),
        ("/0", "/{id}"),
    ])
    def test_numeric_ids(self, path: str, expected: str) -> None:
        assert templatize_path(path) == expected

    # --- UUIDs ---

    @pytest.mark.parametrize("path, expected", [
        (
            "/user/550e8400-e29b-41d4-a716-446655440000",
            "/user/{uuid}",
        ),
        (
            "/api/550e8400-e29b-41d4-a716-446655440000/detail",
            "/api/{uuid}/detail",
        ),
    ])
    def test_uuids(self, path: str, expected: str) -> None:
        assert templatize_path(path) == expected

    # --- Long hex hashes ---

    @pytest.mark.parametrize("path, expected", [
        ("/assets/abc123def456abc0", "/assets/{hash}"),
        ("/commit/abc123def456abc0def1", "/commit/{hash}"),
    ])
    def test_long_hex(self, path: str, expected: str) -> None:
        assert templatize_path(path) == expected

    # --- Dates ---

    @pytest.mark.parametrize("path, expected", [
        ("/blog/2024-01-15", "/blog/{date}"),
        ("/archive/2024-01-15/posts", "/archive/{date}/posts"),
    ])
    def test_dates(self, path: str, expected: str) -> None:
        assert templatize_path(path) == expected

    # --- Embedded numbers ---

    @pytest.mark.parametrize("path, expected", [
        ("/page12345", "/page{n}"),
        ("/item_99999_detail", "/item_{n}_detail"),
    ])
    def test_embedded_numbers(self, path: str, expected: str) -> None:
        assert templatize_path(path) == expected

    # --- Should NOT templatize ---

    @pytest.mark.parametrize("path", [
        "/",
        "/api",
        "/api/users",
        "/api/v1/items",
        "/dashboard/settings",
    ])
    def test_leaves_non_dynamic_paths_unchanged(self, path: str) -> None:
        assert templatize_path(path) == path

    # --- Edge cases ---

    def test_empty_string(self) -> None:
        assert templatize_path("") == ""

    def test_root_path(self) -> None:
        assert templatize_path("/") == "/"

    def test_version_prefix_not_templatized(self) -> None:
        # "v1" has only 1 digit, below _NUMERIC threshold, and
        # _EMBEDDED_NUM requires 4+ digits
        assert templatize_path("/api/v1/users") == "/api/v1/users"
