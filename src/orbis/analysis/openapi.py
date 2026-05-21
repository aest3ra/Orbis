"""OpenAPI/Swagger JSON specification parser.

Extracts endpoints and their parameters from OpenAPI 2.0 (Swagger) and
OpenAPI 3.0 JSON specifications.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Normalize path parameter names: {userId} → {id}, {item_name} → {id}
_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")


@dataclass
class OpenApiEndpoint:
    """An endpoint extracted from an OpenAPI specification."""
    method: str
    path_template: str
    parameters: list[tuple[str, str, str]] = field(default_factory=list)
    # (location, name, type) — e.g. ("query", "page", "integer")
    summary: str | None = None


_VALID_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def parse_openapi_spec(body: str, base_url: str) -> list[OpenApiEndpoint]:
    """Parse OpenAPI JSON spec and extract endpoints.

    Supports OpenAPI 2.0 (Swagger) and 3.0 formats.
    The base_url is used to resolve basePath (v2) relative to server URL.

    Returns empty list if the body is not a valid OpenAPI spec.
    """
    try:
        spec = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(spec, dict):
        return []

    # Detect version and extract base path
    base_path = _extract_base_path(spec)

    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return []

    endpoints: list[OpenApiEndpoint] = []

    for path, path_item in paths.items():
        if not isinstance(path_item, dict) or not path.startswith("/"):
            continue

        # Normalize path: {userId} → {id}
        normalized_path = _normalize_path(base_path + path)

        # Path-level parameters
        path_params = _extract_parameters(path_item.get("parameters", []))

        for method_str, operation in path_item.items():
            if method_str not in _VALID_METHODS:
                continue
            if not isinstance(operation, dict):
                continue

            # Operation-level parameters override path-level
            op_params = _extract_parameters(operation.get("parameters", []))
            # Also extract request body params for OpenAPI 3.0
            body_params = _extract_request_body_params(operation.get("requestBody"))

            all_params = _merge_params(path_params, op_params + body_params)

            summary = operation.get("summary") or operation.get("description")
            if isinstance(summary, str) and len(summary) > 200:
                summary = summary[:200]

            endpoints.append(OpenApiEndpoint(
                method=method_str.upper(),
                path_template=normalized_path,
                parameters=all_params,
                summary=summary if isinstance(summary, str) else None,
            ))

    return endpoints


def _extract_base_path(spec: dict) -> str:
    """Extract base path from spec (v2 basePath or v3 servers)."""
    # OpenAPI 2.0
    base_path = spec.get("basePath", "")
    if isinstance(base_path, str) and base_path and base_path != "/":
        return base_path.rstrip("/")

    # OpenAPI 3.0 servers
    servers = spec.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict):
            url = first.get("url", "")
            if isinstance(url, str) and url:
                # Extract path portion from server URL
                # e.g., "https://api.example.com/v2" → "/v2"
                if url.startswith(("http://", "https://")):
                    from urllib.parse import urlparse
                    path = urlparse(url).path.rstrip("/")
                    if path and path != "/":
                        return path
                elif url.startswith("/"):
                    return url.rstrip("/")

    return ""


def _normalize_path(path: str) -> str:
    """Normalize OpenAPI path: collapse all {paramName} → {id}."""
    return _PATH_PARAM_RE.sub("{id}", path)


def _extract_parameters(params: list | None) -> list[tuple[str, str, str]]:
    """Extract (location, name, type) tuples from OpenAPI parameters array."""
    if not isinstance(params, list):
        return []

    result: list[tuple[str, str, str]] = []
    for param in params:
        if not isinstance(param, dict):
            continue

        name = param.get("name")
        location = param.get("in")  # query, header, path, cookie
        if not isinstance(name, str) or not isinstance(location, str):
            continue

        # Type from schema (v3) or direct type (v2)
        param_type = "string"
        schema = param.get("schema")
        if isinstance(schema, dict):
            param_type = schema.get("type", "string")
        elif "type" in param:
            param_type = param.get("type", "string")

        if not isinstance(param_type, str):
            param_type = "string"

        result.append((location, name, param_type))

    return result


def _extract_request_body_params(request_body: dict | None) -> list[tuple[str, str, str]]:
    """Extract parameters from OpenAPI 3.0 requestBody schema."""
    if not isinstance(request_body, dict):
        return []

    content = request_body.get("content")
    if not isinstance(content, dict):
        return []

    # Try JSON content type first
    for mime_key in ("application/json", "application/x-www-form-urlencoded"):
        media_type = content.get(mime_key)
        if not isinstance(media_type, dict):
            continue
        schema = media_type.get("schema")
        if not isinstance(schema, dict):
            continue
        return _extract_schema_properties(schema, location="body")

    return []


def _extract_schema_properties(
    schema: dict, location: str,
) -> list[tuple[str, str, str]]:
    """Extract top-level properties from a JSON schema object."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []

    result: list[tuple[str, str, str]] = []
    for name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue
        param_type = prop_schema.get("type", "string")
        if not isinstance(param_type, str):
            param_type = "string"
        result.append((location, name, param_type))

    return result


def _merge_params(
    path_params: list[tuple[str, str, str]],
    op_params: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """Merge path-level and operation-level parameters.

    Operation-level params override path-level ones with the same (location, name).
    """
    merged: dict[tuple[str, str], tuple[str, str, str]] = {}
    for loc, name, ptype in path_params:
        merged[(loc, name)] = (loc, name, ptype)
    for loc, name, ptype in op_params:
        merged[(loc, name)] = (loc, name, ptype)
    return list(merged.values())
