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

    # --- Slugs (number-text patterns) ---

    @pytest.mark.parametrize("path, expected", [
        ("/posts/1944-%ED%99%94%EC%9D%B4%ED%8A%B8%ED%96%87%EC%8A%A4%EC%BF%A8-%EC%A7%80%EC%9B%90",
         "/posts/{slug}"),
        ("/posts/1954-bob-vs-%ED%99%94%EC%9D%B4%ED%96%87%EC%8A%A4%EC%BF%A8",
         "/posts/{slug}"),
        ("/blog/12345-this-is-a-long-slug-title",
         "/blog/{slug}"),
        ("/articles/99-abcdefgh",
         "/articles/{slug}"),
    ])
    def test_slug_patterns(self, path: str, expected: str) -> None:
        assert templatize_path(path) == expected

    @pytest.mark.parametrize("path, expected", [
        ("/posts/123-abc", "/posts/123-abc"),           # suffix too short (<8)
        ("/posts/1234-short", "/posts/{n}-short"),       # suffix short, embedded num
        ("/items/123-v2", "/items/123-v2"),              # not a slug, possible variant
    ])
    def test_short_suffix_not_slug(self, path: str, expected: str) -> None:
        assert templatize_path(path) == expected

    def test_slug_order_does_not_break_date(self) -> None:
        """DATE must be matched before SLUG to avoid false positives."""
        assert templatize_path("/report/2024-01-15") == "/report/{date}"

    def test_slug_order_does_not_break_uuid(self) -> None:
        """UUID must be matched before SLUG."""
        assert templatize_path(
            "/item/550e8400-e29b-41d4-a716-446655440000"
        ) == "/item/{uuid}"

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
