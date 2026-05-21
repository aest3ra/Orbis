"""Tests for orbis.analysis.openapi — OpenAPI/Swagger JSON parsing."""

import json
import pytest

from orbis.analysis.openapi import OpenApiEndpoint, parse_openapi_spec


# ---------------------------------------------------------------------------
# OpenAPI 2.0 (Swagger)
# ---------------------------------------------------------------------------

class TestSwagger20:
    def test_basic_paths(self) -> None:
        spec = {
            "swagger": "2.0",
            "basePath": "/api/v1",
            "paths": {
                "/users": {
                    "get": {"summary": "List users"},
                    "post": {"summary": "Create user"},
                },
                "/users/{userId}": {
                    "get": {"summary": "Get user"},
                    "delete": {"summary": "Delete user"},
                },
            },
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        methods = {(e.method, e.path_template) for e in eps}
        assert ("GET", "/api/v1/users") in methods
        assert ("POST", "/api/v1/users") in methods
        assert ("GET", "/api/v1/users/{id}") in methods
        assert ("DELETE", "/api/v1/users/{id}") in methods

    def test_base_path_slash(self) -> None:
        spec = {
            "swagger": "2.0",
            "basePath": "/",
            "paths": {"/items": {"get": {}}},
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        assert eps[0].path_template == "/items"

    def test_no_base_path(self) -> None:
        spec = {
            "swagger": "2.0",
            "paths": {"/items": {"get": {}}},
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        assert eps[0].path_template == "/items"

    def test_parameters_v2(self) -> None:
        spec = {
            "swagger": "2.0",
            "paths": {
                "/users": {
                    "get": {
                        "parameters": [
                            {"name": "page", "in": "query", "type": "integer"},
                            {"name": "limit", "in": "query", "type": "integer"},
                            {"name": "Authorization", "in": "header", "type": "string"},
                        ],
                    },
                },
            },
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        assert len(eps) == 1
        params = eps[0].parameters
        param_set = {(loc, name) for loc, name, _ in params}
        assert ("query", "page") in param_set
        assert ("query", "limit") in param_set
        assert ("header", "Authorization") in param_set


# ---------------------------------------------------------------------------
# OpenAPI 3.0
# ---------------------------------------------------------------------------

class TestOpenAPI30:
    def test_basic_paths(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/api/users": {
                    "get": {"summary": "List"},
                    "post": {"summary": "Create"},
                },
            },
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        methods = {(e.method, e.path_template) for e in eps}
        assert ("GET", "/api/users") in methods
        assert ("POST", "/api/users") in methods

    def test_servers_base_path(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "servers": [{"url": "https://api.example.com/v2"}],
            "paths": {
                "/users": {"get": {}},
            },
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        assert eps[0].path_template == "/v2/users"

    def test_servers_relative_url(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "servers": [{"url": "/api/v3"}],
            "paths": {
                "/items": {"get": {}},
            },
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        assert eps[0].path_template == "/api/v3/items"

    def test_parameters_v3_schema(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/users/{id}": {
                    "get": {
                        "parameters": [
                            {"name": "id", "in": "path", "schema": {"type": "integer"}},
                            {"name": "fields", "in": "query", "schema": {"type": "string"}},
                        ],
                    },
                },
            },
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        params = eps[0].parameters
        param_types = {(loc, name): ptype for loc, name, ptype in params}
        assert param_types[("path", "id")] == "integer"
        assert param_types[("query", "fields")] == "string"

    def test_request_body_v3(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/users": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "age": {"type": "integer"},
                                            "email": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        params = eps[0].parameters
        param_set = {(loc, name) for loc, name, _ in params}
        assert ("body", "name") in param_set
        assert ("body", "age") in param_set
        assert ("body", "email") in param_set


# ---------------------------------------------------------------------------
# Path-level parameters
# ---------------------------------------------------------------------------

class TestPathLevelParams:
    def test_path_level_inherited(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/users/{id}": {
                    "parameters": [
                        {"name": "id", "in": "path", "schema": {"type": "integer"}},
                    ],
                    "get": {"summary": "Get user"},
                    "delete": {"summary": "Delete user"},
                },
            },
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        assert len(eps) == 2
        for ep in eps:
            assert any(name == "id" for _, name, _ in ep.parameters)

    def test_operation_overrides_path_level(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/items": {
                    "parameters": [
                        {"name": "page", "in": "query", "schema": {"type": "string"}},
                    ],
                    "get": {
                        "parameters": [
                            {"name": "page", "in": "query", "schema": {"type": "integer"}},
                        ],
                    },
                },
            },
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        params = eps[0].parameters
        page_type = next(ptype for loc, name, ptype in params if name == "page")
        assert page_type == "integer"  # Operation-level overrides path-level


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

class TestPathNormalization:
    def test_various_param_names(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/users/{userId}/posts/{postId}": {"get": {}},
            },
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        assert eps[0].path_template == "/users/{id}/posts/{id}"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_invalid_json(self) -> None:
        assert parse_openapi_spec("not json", "https://example.com") == []

    def test_empty_json(self) -> None:
        assert parse_openapi_spec("{}", "https://example.com") == []

    def test_no_paths(self) -> None:
        spec = {"openapi": "3.0.0", "info": {"title": "API"}}
        assert parse_openapi_spec(json.dumps(spec), "https://example.com") == []

    def test_invalid_path_item(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/valid": {"get": {}},
                "invalid-no-slash": {"get": {}},
                "/also-valid": "not-a-dict",
            },
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        assert len(eps) == 1
        assert eps[0].path_template == "/valid"

    def test_summary_truncation(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/items": {
                    "get": {"summary": "A" * 300},
                },
            },
        }
        eps = parse_openapi_spec(json.dumps(spec), "https://example.com")
        assert len(eps[0].summary) <= 200
