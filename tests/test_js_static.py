"""Tests for orbis.analysis.js_static — API URL literals in JS text."""

from orbis.analysis.api_markers import contains_api_marker
from orbis.analysis.js_static import MAX_REFS_PER_FILE, StaticEndpointRef, extract_js_endpoints


def _urls(js: str) -> list[str]:
    return [ref.raw_url for ref in extract_js_endpoints(js)]


class TestContainsApiMarker:
    def test_markers(self) -> None:
        for text in ("/api/users", "/rest/v1/items", "/graphql", "/gql", "/b2b/orders", 'baseApiUrl = "api/v1"'):
            assert contains_api_marker(text) is True

    def test_no_marker(self) -> None:
        assert contains_api_marker("var x = 1; var y = 2;") is False


class TestLiteralScan:
    def test_literal_in_any_call_shape(self) -> None:
        js = '''
        fetch("/api/users");
        axios.post('/api/orders', body);
        $.getJSON(config.relative_path + "/api/search?term=" + q);
        this.http.get(`/api/v1/items`);
        register("/rest/v1/orders");
        '''
        assert _urls(js) == ["/api/users", "/api/orders", "/api/search?term=", "/api/v1/items", "/rest/v1/orders"]

    def test_method_is_always_get(self) -> None:
        assert extract_js_endpoints('axios.delete("/api/users/1")') == [StaticEndpointRef("GET", "/api/users/1")]

    def test_absolute_url_kept_as_is(self) -> None:
        assert _urls('fetch("https://api.example.com/v1/api/users")') == ["https://api.example.com/v1/api/users"]

    def test_assignment_literal_is_not_special(self) -> None:
        assert _urls('var url = "/api/v1/products";') == ["/api/v1/products"]

    def test_trailing_punctuation_stripped(self) -> None:
        assert _urls('log("see /api/users.")') == ["/api/users"]

    def test_duplicates_collapse(self) -> None:
        assert _urls('fetch("/api/users"); fetch("/api/users")') == ["/api/users"]

    def test_no_marker_means_no_refs(self) -> None:
        assert extract_js_endpoints('fetch("/users/123"); axios.get("/products")') == []
        assert extract_js_endpoints("") == []

    def test_max_refs_per_file(self) -> None:
        js = "\n".join(f'fetch("/api/endpoint{i}")' for i in range(MAX_REFS_PER_FILE + 100))
        assert len(extract_js_endpoints(js)) == MAX_REFS_PER_FILE


class TestHygieneFilters:
    def test_template_literal_remnant_dropped(self) -> None:
        # `/api/users/${id}` is cut at `{`; what is left is a prefix, not a path.
        assert _urls("fetch(`/api/users/${id}`)") == []

    def test_router_pattern_dropped(self) -> None:
        js = '''
        app.get("/api/v3/queries/:id", handler);
        app.use("/api/v3/*", handler);
        app.get("/api/v3(/projects/:identifier)/queries/default", handler);
        '''
        assert _urls(js) == []

    def test_port_in_absolute_url_is_not_a_pattern(self) -> None:
        assert _urls('fetch("http://localhost:8080/api/users")') == ["http://localhost:8080/api/users"]

    def test_source_module_path_dropped(self) -> None:
        js = '''
        import("/app/features/bim/bcf/api/bcf-api.service.ts");
        "/app/shared/helpers/api-v3/filter-builder.js.map";
        fetch("/api/v3/queries/group_bys/status");
        '''
        assert _urls(js) == ["/api/v3/queries/group_bys/status"]

    def test_query_string_kept(self) -> None:
        assert _urls('fetch("/api/search?q=$x")') == ["/api/search?q=$x"]
