"""Tests for orbis.crawler.slug — cardinality-based slug detection."""

from orbis.crawler.slug import SlugDetector, split_segments


def _feed(det: SlugDetector, host: str, paths: list[str]) -> None:
    for p in paths:
        det.observe(host, p)


class TestSplitSegments:
    def test_basic(self) -> None:
        assert split_segments("/board/hello/view") == ["board", "hello", "view"]

    def test_root(self) -> None:
        assert split_segments("/") == []

    def test_trailing_and_double_slash(self) -> None:
        assert split_segments("/a//b/") == ["a", "b"]


class TestDetection:
    def test_collapses_last_segment_after_threshold(self) -> None:
        det = SlugDetector(threshold=4)
        _feed(det, "ex.com", [f"/posts/{w}" for w in ("a", "b", "c", "d")])
        assert det.template("ex.com", "/posts/zzz") == "/posts/{slug}"
        # existing members normalize too
        assert det.template("ex.com", "/posts/a") == "/posts/{slug}"

    def test_below_threshold_unchanged(self) -> None:
        det = SlugDetector(threshold=4)
        _feed(det, "ex.com", ["/posts/a", "/posts/b", "/posts/c"])
        assert det.template("ex.com", "/posts/a") == "/posts/a"

    def test_numeric_handled_by_cardinality_not_shape(self) -> None:
        det = SlugDetector(threshold=3)
        _feed(det, "ex.com", ["/user/1", "/user/2", "/user/3"])
        assert det.template("ex.com", "/user/9") == "/user/{slug}"

    def test_middle_segment_slug(self) -> None:
        det = SlugDetector(threshold=3)
        _feed(det, "ex.com", [f"/board/{w}/view" for w in ("x", "y", "z")])
        assert det.template("ex.com", "/board/anything/view") == "/board/{slug}/view"
        # the fixed suffix is preserved, not collapsed
        assert det.template("ex.com", "/board/x") == "/board/x"


class TestRootProtection:
    def test_top_level_pages_never_slugged(self) -> None:
        det = SlugDetector(threshold=3)
        _feed(det, "ex.com", [
            "/about", "/contact", "/pricing", "/careers", "/team", "/blog",
        ])
        assert det.template("ex.com", "/about") == "/about"
        assert det.template("ex.com", "/blog") == "/blog"


class TestScoping:
    def test_different_parents_counted_separately(self) -> None:
        det = SlugDetector(threshold=3)
        # /a/* gets 3 distinct -> slug; /b/* gets only 1 -> stays
        _feed(det, "ex.com", ["/a/1", "/a/2", "/a/3", "/b/only"])
        assert det.template("ex.com", "/a/x") == "/a/{slug}"
        assert det.template("ex.com", "/b/only") == "/b/only"

    def test_hosts_isolated(self) -> None:
        det = SlugDetector(threshold=3)
        _feed(det, "a.com", ["/p/1", "/p/2", "/p/3"])
        assert det.template("a.com", "/p/x") == "/p/{slug}"
        # other host has seen nothing -> unchanged
        assert det.template("b.com", "/p/x") == "/p/x"

    def test_same_last_segment_different_prefix_not_merged(self) -> None:
        det = SlugDetector(threshold=3)
        # distinct second segments under DIFFERENT prefixes don't share a slot
        _feed(det, "ex.com", ["/v1/posts", "/v2/posts", "/v3/posts"])
        # last segment is always "posts" (cardinality 1) -> not slugged;
        # the version segment is position 0 with 3 distinct -> /{slug}/posts
        assert det.template("ex.com", "/v4/posts") == "/{slug}/posts"


class TestCascade:
    def test_nested_slug_after_parent_known(self) -> None:
        det = SlugDetector(threshold=3)
        # First teach it that shop ids vary (position 1)
        _feed(det, "ex.com", ["/shop/1", "/shop/2", "/shop/3"])
        assert det.template("ex.com", "/shop/9") == "/shop/{slug}"
        # Now reviews under (already-slugged) shops accumulate together
        _feed(det, "ex.com", [
            "/shop/1/reviews/aa", "/shop/2/reviews/bb", "/shop/3/reviews/cc",
        ])
        assert (
            det.template("ex.com", "/shop/7/reviews/dd")
            == "/shop/{slug}/reviews/{slug}"
        )


class TestMemoryBound:
    def test_value_set_capped_at_threshold(self) -> None:
        det = SlugDetector(threshold=3)
        _feed(det, "ex.com", [f"/posts/{i}" for i in range(100)])
        # only one context for the open last position; capped at threshold
        sizes = [len(v) for v in det._values.values()]
        assert max(sizes) <= 3
