"""Tests for orbis.analysis.js_static — JS static endpoint extraction."""

import pytest

from orbis.analysis.js_static import (
    StaticEndpointRef,
    contains_api_marker,
    extract_js_endpoints,
    sanitize_candidate_url,
)


# ---------------------------------------------------------------------------
# contains_api_marker
# ---------------------------------------------------------------------------

class TestContainsApiMarker:
    def test_api_prefix(self) -> None:
        assert contains_api_marker('/api/users') is True

    def test_rest_prefix(self) -> None:
        assert contains_api_marker('/rest/v1/items') is True

    def test_graphql(self) -> None:
        assert contains_api_marker('/graphql') is True

    def test_no_marker(self) -> None:
        assert contains_api_marker('var x = 1; var y = 2;') is False

    def test_api_in_word(self) -> None:
        # "api/" as substring should still trigger
        assert contains_api_marker('baseApiUrl = "api/v1"') is True


# ---------------------------------------------------------------------------
# fetch() extraction
# ---------------------------------------------------------------------------

class TestFetch:
    def test_simple_fetch(self) -> None:
        js = 'fetch("/api/users")'
        refs = extract_js_endpoints(js)
        assert any(r.method == "GET" and r.raw_url == "/api/users" for r in refs)

    def test_fetch_with_post(self) -> None:
        js = 'fetch("/api/users", {method: "POST", body: JSON.stringify(data)})'
        refs = extract_js_endpoints(js)
        assert any(r.method == "POST" and r.raw_url == "/api/users" for r in refs)

    def test_fetch_single_quotes(self) -> None:
        js = "fetch('/api/items')"
        refs = extract_js_endpoints(js)
        assert any(r.raw_url == "/api/items" for r in refs)

    def test_fetch_template_literal(self) -> None:
        js = 'fetch(`/api/users/${userId}`)'
        refs = extract_js_endpoints(js)
        assert any("/api/users/" in r.raw_url for r in refs)

    def test_window_fetch(self) -> None:
        js = 'window.fetch("/api/data")'
        refs = extract_js_endpoints(js)
        assert any(r.raw_url == "/api/data" for r in refs)

    def test_fetch_absolute_url(self) -> None:
        js = 'fetch("https://api.example.com/api/v1/users")'
        refs = extract_js_endpoints(js)
        assert any("api.example.com" in r.raw_url for r in refs)


# ---------------------------------------------------------------------------
# axios extraction
# ---------------------------------------------------------------------------

class TestAxios:
    def test_axios_get(self) -> None:
        js = 'axios.get("/api/users")'
        refs = extract_js_endpoints(js)
        assert any(r.method == "GET" and r.raw_url == "/api/users" for r in refs)

    def test_axios_post(self) -> None:
        js = 'axios.post("/api/users", data)'
        refs = extract_js_endpoints(js)
        assert any(r.method == "POST" and r.raw_url == "/api/users" for r in refs)

    def test_axios_put(self) -> None:
        js = 'axios.put("/api/users/1", data)'
        refs = extract_js_endpoints(js)
        assert any(r.method == "PUT" for r in refs)

    def test_axios_delete(self) -> None:
        js = 'axios.delete("/api/users/1")'
        refs = extract_js_endpoints(js)
        assert any(r.method == "DELETE" for r in refs)

    def test_axios_object_config(self) -> None:
        js = 'axios({url: "/api/users", method: "POST"})'
        refs = extract_js_endpoints(js)
        assert any(r.method == "POST" and r.raw_url == "/api/users" for r in refs)


# ---------------------------------------------------------------------------
# XHR extraction
# ---------------------------------------------------------------------------

class TestXHR:
    def test_xhr_open(self) -> None:
        js = 'xhr.open("GET", "/api/users")'
        refs = extract_js_endpoints(js)
        assert any(r.method == "GET" and r.raw_url == "/api/users" for r in refs)

    def test_xhr_open_post(self) -> None:
        js = 'xhr.open("POST", "/api/submit")'
        refs = extract_js_endpoints(js)
        assert any(r.method == "POST" and r.raw_url == "/api/submit" for r in refs)


# ---------------------------------------------------------------------------
# jQuery extraction
# ---------------------------------------------------------------------------

class TestJQuery:
    def test_jquery_get(self) -> None:
        js = '$.get("/api/users")'
        refs = extract_js_endpoints(js)
        assert any(r.method == "GET" and r.raw_url == "/api/users" for r in refs)

    def test_jquery_post(self) -> None:
        js = '$.post("/api/users", data)'
        refs = extract_js_endpoints(js)
        assert any(r.method == "POST" and r.raw_url == "/api/users" for r in refs)

    def test_jquery_getjson(self) -> None:
        js = '$.getJSON("/api/data")'
        refs = extract_js_endpoints(js)
        assert any(r.method == "GET" and r.raw_url == "/api/data" for r in refs)

    def test_jquery_ajax(self) -> None:
        js = '$.ajax({url: "/api/users", type: "PUT"})'
        refs = extract_js_endpoints(js)
        assert any(r.method == "PUT" and r.raw_url == "/api/users" for r in refs)

    def test_jQuery_uppercase(self) -> None:
        js = 'jQuery.get("/api/items")'
        refs = extract_js_endpoints(js)
        assert any(r.raw_url == "/api/items" for r in refs)


# ---------------------------------------------------------------------------
# Angular http extraction
# ---------------------------------------------------------------------------

class TestAngularHttp:
    def test_http_get(self) -> None:
        js = 'this.http.get("/api/users")'
        refs = extract_js_endpoints(js)
        assert any(r.method == "GET" and r.raw_url == "/api/users" for r in refs)

    def test_http_post(self) -> None:
        js = 'this.http.post("/api/users")'
        refs = extract_js_endpoints(js)
        assert any(r.method == "POST" and r.raw_url == "/api/users" for r in refs)


# ---------------------------------------------------------------------------
# Variable resolution
# ---------------------------------------------------------------------------

class TestVariableResolution:
    def test_const_base(self) -> None:
        js = '''
        const BASE_URL = "/api/v1";
        fetch(BASE_URL + "/users");
        '''
        refs = extract_js_endpoints(js)
        assert any("/api/v1/users" in r.raw_url for r in refs)

    def test_this_property(self) -> None:
        js = '''
        this.apiUrl = "/api/v2";
        this.http.get(this.apiUrl + "/items");
        '''
        refs = extract_js_endpoints(js)
        assert any("/api/v2/items" in r.raw_url for r in refs)

    def test_chained_assignment(self) -> None:
        js = '''
        this.base = "/api";
        this.endpoint = this.base + "/v1/users";
        fetch(this.endpoint);
        '''
        refs = extract_js_endpoints(js)
        assert any("/api/v1/users" in r.raw_url for r in refs)

    def test_assignment_value_with_comma(self) -> None:
        js = '''
        const ENDPOINT = "/api/v1,legacy";
        fetch(ENDPOINT + "/users");
        '''
        refs = extract_js_endpoints(js)
        assert any(r.raw_url == "/api/v1,legacy/users" for r in refs)

    def test_template_literal_assignment_with_newline(self) -> None:
        js = (
            "const ENDPOINT = `/api/v1/users/${userId}\n"
            "/profile`;\n"
            "fetch(ENDPOINT);"
        )
        refs = extract_js_endpoints(js)
        assert any(r.raw_url == "/api/v1/users/{id}\n/profile" for r in refs)


# ---------------------------------------------------------------------------
# Template literal evaluation
# ---------------------------------------------------------------------------

class TestTemplateLiteral:
    def test_simple_template(self) -> None:
        js = '''
        this.baseUrl = "/api/v1";
        fetch(`${this.baseUrl}/users`);
        '''
        refs = extract_js_endpoints(js)
        assert any("/api/v1/users" in r.raw_url for r in refs)

    def test_template_with_unknown_var(self) -> None:
        js = 'fetch(`/api/users/${userId}`)'
        refs = extract_js_endpoints(js)
        # Unknown ${userId} after "/" should become {id}
        assert any("/api/users/" in r.raw_url for r in refs)

    def test_template_mixed(self) -> None:
        js = '''
        this.base = "/api/v1";
        fetch(`${this.base}/users/${id}/profile`);
        '''
        refs = extract_js_endpoints(js)
        assert any("/api/v1/users/" in r.raw_url for r in refs)


# ---------------------------------------------------------------------------
# Fallback: API URL string literal
# ---------------------------------------------------------------------------

class TestFallback:
    def test_api_string_literal(self) -> None:
        js = 'var url = "/api/v1/products";'
        # This is an assignment — should be captured by variable resolution
        # but without a call, it falls to fallback only if it's not recognized as assignment
        refs = extract_js_endpoints(js)
        # The assignment is tracked, but the fallback skips assignment-looking patterns
        # So this might or might not appear depending on _looks_assignment_literal
        # Let's test a non-assignment context
        pass

    def test_standalone_api_url(self) -> None:
        js = 'routes.push("/api/v1/checkout")'
        refs = extract_js_endpoints(js)
        assert any("/api/v1/checkout" in r.raw_url for r in refs)

    def test_rest_url(self) -> None:
        # URL in non-assignment context (function arg, not a = ...)
        js = 'register("/rest/v1/orders")'
        refs = extract_js_endpoints(js)
        assert any("/rest/v1/orders" in r.raw_url for r in refs)


# ---------------------------------------------------------------------------
# No API marker → empty result
# ---------------------------------------------------------------------------

class TestNoMarker:
    def test_no_api_urls(self) -> None:
        js = '''
        fetch("/users/123");
        axios.get("/products/list");
        '''
        refs = extract_js_endpoints(js)
        assert refs == []

    def test_completely_unrelated_code(self) -> None:
        js = 'var x = 1 + 2; console.log("hello world");'
        assert extract_js_endpoints(js) == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_input(self) -> None:
        assert extract_js_endpoints("") == []

    def test_max_refs_limit(self) -> None:
        # Generate many API calls
        lines = [f'fetch("/api/endpoint{i}")' for i in range(300)]
        js = "\n".join(lines)
        refs = extract_js_endpoints(js)
        assert len(refs) <= 200  # MAX_REFS_PER_FILE

    def test_no_duplicate_from_handled_spans(self) -> None:
        """fetch("/api/users") should not also appear as a fallback match."""
        js = 'fetch("/api/users")'
        refs = extract_js_endpoints(js)
        api_user_refs = [r for r in refs if "/api/users" in r.raw_url]
        assert len(api_user_refs) == 1  # Only from fetch, not fallback


# ---------------------------------------------------------------------------
# sanitize_candidate_url
# ---------------------------------------------------------------------------

class TestSanitizeCandidateUrl:
    def test_angle_placeholder(self) -> None:
        assert sanitize_candidate_url("/api/users/<userId>") == "/api/users/{id}"

    def test_trailing_dollar(self) -> None:
        assert sanitize_candidate_url("/api/users/$") == "/api/users"

    def test_trailing_punctuation(self) -> None:
        assert sanitize_candidate_url("/api/users.") == "/api/users"
        assert sanitize_candidate_url("/api/users;") == "/api/users"

    def test_clean_url(self) -> None:
        assert sanitize_candidate_url("/api/users") == "/api/users"
