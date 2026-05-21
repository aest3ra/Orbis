"""Tests for orbis.analysis.docs — API documentation endpoint extraction."""

import pytest

from orbis.analysis.docs import extract_doc_endpoints


# ---------------------------------------------------------------------------
# Detection: does a page look like API docs?
# ---------------------------------------------------------------------------

class TestDetection:
    def test_url_with_api_doc_marker(self) -> None:
        body = "<html><body>Simple page</body></html>"
        refs = extract_doc_endpoints(body, "https://example.com/api-docs")
        # Empty body with no endpoints — but detection should pass
        assert refs == []  # detected as docs, but no endpoints found

    def test_body_with_api_reference_text(self) -> None:
        body = """
        <html><body>
        <h1>API Reference</h1>
        <p>GET /api/users - List all users</p>
        <p>POST /api/users - Create a user</p>
        </body></html>
        """
        refs = extract_doc_endpoints(body, "https://example.com/random-page")
        assert len(refs) >= 2

    def test_body_without_markers_not_detected(self) -> None:
        body = "<html><body><p>Welcome to our site!</p></body></html>"
        refs = extract_doc_endpoints(body, "https://example.com/about")
        assert refs == []


# ---------------------------------------------------------------------------
# METHOD /path text pattern extraction
# ---------------------------------------------------------------------------

class TestMethodPathExtraction:
    def test_get_path(self) -> None:
        body = """
        <html><body>
        <h1>API Documentation</h1>
        <p>GET /api/users</p>
        <p>POST /api/users</p>
        </body></html>
        """
        refs = extract_doc_endpoints(body, "https://example.com/docs")
        methods = {(r.method, r.raw_url) for r in refs}
        assert ("GET", "/api/users") in methods
        assert ("POST", "/api/users") in methods

    def test_various_methods(self) -> None:
        body = """
        <html><body>
        <h1>API Docs</h1>
        <p>GET /api/items</p>
        <p>PUT /api/items/1</p>
        <p>DELETE /api/items/1</p>
        <p>PATCH /api/items/1</p>
        </body></html>
        """
        refs = extract_doc_endpoints(body, "https://example.com/api-docs")
        methods = {r.method for r in refs}
        assert {"GET", "PUT", "DELETE", "PATCH"} <= methods

    def test_colon_placeholder_normalized(self) -> None:
        body = """
        <html><body>
        <h1>API Docs</h1>
        <p>GET /api/users/:userId</p>
        <p>DELETE /api/users/:id</p>
        </body></html>
        """
        refs = extract_doc_endpoints(body, "https://example.com/api-docs")
        raw_urls = {r.raw_url for r in refs}
        assert "/api/users/{id}" in raw_urls


# ---------------------------------------------------------------------------
# <code> tag extraction
# ---------------------------------------------------------------------------

class TestCodeTagExtraction:
    def test_code_path(self) -> None:
        body = """
        <html><body>
        <h1>API Reference</h1>
        <p>Endpoint: <code>/api/v2/products</code></p>
        <p>Endpoint: <code>/api/v2/orders</code></p>
        </body></html>
        """
        refs = extract_doc_endpoints(body, "https://example.com/docs")
        raw_urls = {r.raw_url for r in refs}
        assert "/api/v2/products" in raw_urls
        assert "/api/v2/orders" in raw_urls

    def test_code_defaults_to_get(self) -> None:
        body = """
        <html><body>
        <h1>API Reference</h1>
        <code>/api/users</code>
        <code>/api/orders</code>
        </body></html>
        """
        refs = extract_doc_endpoints(body, "https://example.com/api-docs")
        assert all(r.method == "GET" for r in refs)


# ---------------------------------------------------------------------------
# HTML form/link extraction
# ---------------------------------------------------------------------------

class TestHTMLExtraction:
    def test_form_action(self) -> None:
        body = """
        <html><body>
        <h1>API Documentation</h1>
        <form method="POST" action="/api/login"></form>
        <form method="GET" action="/api/search"></form>
        </body></html>
        """
        refs = extract_doc_endpoints(body, "https://example.com/docs")
        methods = {(r.method, r.raw_url) for r in refs}
        assert ("POST", "/api/login") in methods
        assert ("GET", "/api/search") in methods

    def test_link_href(self) -> None:
        body = """
        <html><body>
        <h1>API Reference</h1>
        <a href="/api/v1/users">Users API</a>
        <a href="/api/v1/orders">Orders API</a>
        </body></html>
        """
        refs = extract_doc_endpoints(body, "https://example.com/apidoc")
        raw_urls = {r.raw_url for r in refs}
        assert "/api/v1/users" in raw_urls
        assert "/api/v1/orders" in raw_urls


# ---------------------------------------------------------------------------
# apiDoc define() format
# ---------------------------------------------------------------------------

class TestApiDocFormat:
    def test_apidoc_define(self) -> None:
        body = '''define({"api": [
            {"type": "GET", "url": "/api/users"},
            {"type": "POST", "url": "/api/users"},
            {"type": "DELETE", "url": "/api/users/:id"}
        ]});'''
        refs = extract_doc_endpoints(body, "https://example.com/apidoc/api_data.js")
        methods = {(r.method, r.raw_url) for r in refs}
        assert ("GET", "/api/users") in methods
        assert ("POST", "/api/users") in methods
        assert ("DELETE", "/api/users/{id}") in methods


# ---------------------------------------------------------------------------
# Noise filtering
# ---------------------------------------------------------------------------

class TestNoiseFiltering:
    def test_static_asset_paths_excluded(self) -> None:
        body = """
        <html><body>
        <h1>API Documentation</h1>
        <a href="/js/app.js">Script</a>
        <a href="/css/style.css">Style</a>
        <a href="/images/logo.png">Logo</a>
        <a href="/api/users">Users</a>
        <a href="/api/orders">Orders</a>
        </body></html>
        """
        refs = extract_doc_endpoints(body, "https://example.com/docs")
        raw_urls = {r.raw_url for r in refs}
        assert "/api/users" in raw_urls
        assert "/js/app.js" not in raw_urls
        assert "/css/style.css" not in raw_urls

    def test_doc_prefix_paths_excluded(self) -> None:
        body = """
        <html><body>
        <h1>API Documentation</h1>
        <a href="/apidoc/api_data.js">Data</a>
        <a href="/api/users">Users</a>
        <a href="/api/items">Items</a>
        </body></html>
        """
        refs = extract_doc_endpoints(body, "https://example.com/docs")
        raw_urls = {r.raw_url for r in refs}
        assert "/api/users" in raw_urls
        assert "/apidoc/api_data.js" not in raw_urls

    def test_root_path_excluded(self) -> None:
        body = """
        <html><body>
        <h1>API Reference</h1>
        <a href="/">Home</a>
        <a href="/api/users">Users</a>
        <a href="/api/orders">Orders</a>
        </body></html>
        """
        refs = extract_doc_endpoints(body, "https://example.com/docs")
        raw_urls = {r.raw_url for r in refs}
        assert "/" not in raw_urls


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDedup:
    def test_no_duplicate_refs(self) -> None:
        # Same endpoint from multiple sources (text, code tag, link)
        body = """
        <html><body>
        <h1>API Docs</h1>
        <p>GET /api/users</p>
        <a href="/api/users">Users</a>
        <code>/api/users</code>
        </body></html>
        """
        refs = extract_doc_endpoints(body, "https://example.com/api-docs")
        get_users = [(r.method, r.raw_url) for r in refs if r.raw_url == "/api/users" and r.method == "GET"]
        assert len(get_users) == 1
