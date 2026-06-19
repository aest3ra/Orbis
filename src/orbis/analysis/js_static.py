"""Static API endpoint extraction from external JS bundle text.

Ported from Browser-Agent beta's common.py. This is a shallow string-expression
evaluator, not a JS parser. It covers common bundled SPA service patterns:

- fetch(), axios.get/post(), XHR .open(), $.ajax(), $.get/post()
- Angular this.http.get/post()
- Variable/constant resolution (3-pass iteration for chained assignments)
- Template literal evaluation: `${this.baseUrl}/users/${id}`
- String concatenation: this.host + "/api" + "/users"
- API marker fallback: /api/*, /rest/*, /graphql/* string literals

No external dependencies — pure text in, StaticEndpointRef list out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- API marker detection ---

API_PREFIX_RE = re.compile(
    r"/(?:api(?:[-_][a-z0-9]+)?|rest|b2b|graphql)(?=[/?#]|$)",
    re.IGNORECASE,
)
API_URL_RE = re.compile(
    r"""(?P<url>"""
    r"""https?://[^"'`\s<>{}\\]+/(?:api(?:[-_][a-z0-9]+)?|rest|b2b|graphql)(?=[/?#]|$)[^"'`\s<>{}\\]*"""
    r"""|https?://[^"'`\s<>{}\\]+/[^"'`\s<>{}\\]*?/(?:api(?:[-_][a-z0-9]+)?|rest|b2b|graphql)(?=[/?#]|$)[^"'`\s<>{}\\]*"""
    r"""|/(?!/)[^"'`\s<>{}\\]*?/(?:api(?:[-_][a-z0-9]+)?|rest|b2b|graphql)(?=[/?#]|$)[^"'`\s<>{}\\]*"""
    r"""|/(?:api(?:[-_][a-z0-9]+)?|rest|b2b|graphql)(?=[/?#]|$)[^"'`\s<>{}\\]*)""",
    re.IGNORECASE,
)
ANGLE_PLACEHOLDER_RE = re.compile(r"<[A-Za-z_$][\w$-]*>")

# Max endpoints per JS file to prevent explosion
MAX_REFS_PER_FILE = 200

# --- HTTP call pattern regexes ---

_ASSIGNMENT_RE = re.compile(
    # The string alternatives exclude backslash from the "other char" branch
    # ([^"\\] not [^"]) so a backslash is consumed ONLY by \\. — without this,
    # \\.|[^"] is ambiguous and a run of backslashes in an unclosed string
    # causes catastrophic backtracking (a 45-char input hangs for seconds; a
    # real minified bundle hangs for many minutes, GIL-held, uninterruptible).
    r"""(?<![\w${?&])(?P<name>(?:this\.)?[A-Za-z_$][\w$]*)\s*=\s*(?![>/])"""
    r"""(?P<expr>(?:`(?:\\.|[^`\\])*`|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|[^;,\n]){1,500})""",
    re.DOTALL,
)
_OBJECT_ASSIGNMENT_RE = re.compile(
    r"""(?<![\w${?&])(?P<name>(?:this\.)?[A-Za-z_$][\w$]*)\s*=\s*\{""",
    re.DOTALL,
)
_HTTP_CALL_RE = re.compile(
    r"""(?P<receiver>(?:this\.)?http|axios)\.(?P<method>get|post|put|patch|delete)\s*\(""",
    re.IGNORECASE,
)
_FETCH_CALL_RE = re.compile(
    r"""(?<![\w$.])(?:window\.|self\.|globalThis\.)?fetch\s*\(""",
    re.IGNORECASE,
)
_REQUEST_CALL_RE = re.compile(
    r"""(?<![\w$.])new\s+Request\s*\(""",
    re.IGNORECASE,
)
_XHR_OPEN_RE = re.compile(r"""\.open\s*\(""", re.IGNORECASE)
_JQUERY_SHORTCUT_RE = re.compile(
    r"""(?P<receiver>\$|jQuery)\.(?P<method>get|post|getJSON)\s*\(""",
    re.IGNORECASE,
)
_JQUERY_AJAX_RE = re.compile(
    r"""(?P<receiver>\$|jQuery)\.ajax\s*\(""",
    re.IGNORECASE,
)
_AXIOS_OBJECT_RE = re.compile(
    r"""(?<![\w$.])axios(?:\.request)?\s*\(""",
    re.IGNORECASE,
)
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
_URL_OBJECT_KEYS = {"url", "path", "endpoint", "api", "href"}
_METHOD_OBJECT_KEYS = {"method", "type"}


@dataclass(frozen=True)
class StaticEndpointRef:
    """A single API endpoint reference extracted from JS text."""
    method: str     # GET, POST, PUT, PATCH, DELETE
    raw_url: str    # extracted URL (relative or absolute)


def contains_api_marker(text: str) -> bool:
    """Quick check: does the text contain any API path marker?"""
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "/api", "/rest", "/b2b", "/graphql",
            "api/", "rest/", "b2b/", "graphql",
        )
    )


def extract_js_endpoints(body: str) -> list[StaticEndpointRef]:
    """Extract API endpoint references from JS text.

    Returns a list of (method, raw_url) pairs. URLs are not resolved —
    the caller is responsible for resolving relative URLs against the page URL.
    """
    if not contains_api_marker(body):
        return []

    refs: list[StaticEndpointRef] = []
    handled_spans: list[tuple[int, int]] = []
    global_env = _collect_api_assignments(body, allow_locals=False)

    # 1. axios.get/post/etc, this.http.get/post/etc
    for match in _HTTP_CALL_RE.finditer(body):
        args = _extract_arguments(body, match.end(), max_args=1)
        if not args:
            continue
        expr, span = args[0]
        env = _env_for_call(body, match.start(), global_env)
        raw_url = _evaluate_url_expression(expr, env)
        if raw_url is None:
            continue
        handled_spans.append(span)
        refs.append(StaticEndpointRef(
            method=match.group("method").upper(),
            raw_url=raw_url,
        ))

    # 2. fetch(), new Request()
    for match in list(_FETCH_CALL_RE.finditer(body)) + list(_REQUEST_CALL_RE.finditer(body)):
        args = _extract_arguments(body, match.end(), max_args=2)
        if not args:
            continue
        env = _env_for_call(body, match.start(), global_env)
        raw_url = _evaluate_url_expression(args[0][0], env)
        if raw_url is None:
            continue
        method = "GET"
        if len(args) > 1:
            method = _method_from_object_literal(args[1][0]) or method
        handled_spans.append(args[0][1])
        refs.append(StaticEndpointRef(method=method, raw_url=raw_url))

    # 3. XHR .open("METHOD", "url")
    for match in _XHR_OPEN_RE.finditer(body):
        args = _extract_arguments(body, match.end(), max_args=2)
        if len(args) < 2:
            continue
        method = _method_from_expression(args[0][0])
        if method is None:
            continue
        env = _env_for_call(body, match.start(), global_env)
        raw_url = _evaluate_url_expression(args[1][0], env)
        if raw_url is None:
            continue
        handled_spans.append(args[1][1])
        refs.append(StaticEndpointRef(method=method, raw_url=raw_url))

    # 4. $.get/post/getJSON
    for match in _JQUERY_SHORTCUT_RE.finditer(body):
        args = _extract_arguments(body, match.end(), max_args=2)
        if not args:
            continue
        env = _env_for_call(body, match.start(), global_env)
        raw_url = _evaluate_url_expression(args[0][0], env)
        url_span = args[0][1]
        if raw_url is None:
            extracted = _url_from_object_literal(args[0][0], env)
            if extracted is None:
                continue
            raw_url, relative_span = extracted
            url_span = _absolute_span(args[0][1], relative_span)
        method_name = match.group("method").lower()
        method = "GET" if method_name == "getjson" else method_name.upper()
        handled_spans.append(url_span)
        refs.append(StaticEndpointRef(method=method, raw_url=raw_url))

    # 5. $.ajax({url: ..., method: ...})
    for match in _JQUERY_AJAX_RE.finditer(body):
        _append_settings_call_ref(
            body, match.end(), match.start(), refs, handled_spans, global_env,
        )

    # 6. axios({url: ..., method: ...})
    for match in _AXIOS_OBJECT_RE.finditer(body):
        _append_settings_call_ref(
            body, match.end(), match.start(), refs, handled_spans, global_env,
        )

    # 7. Fallback: /api/*, /rest/*, /graphql/* string literals
    for match in API_URL_RE.finditer(body):
        if _inside_any(match.span("url"), handled_spans):
            continue
        if _looks_assignment_literal(body, match.start("url")):
            continue
        refs.append(StaticEndpointRef(method="GET", raw_url=match.group("url")))

    return refs[:MAX_REFS_PER_FILE]


def sanitize_candidate_url(url: str) -> str:
    """Clean up URL artifacts from JS extraction."""
    url = ANGLE_PLACEHOLDER_RE.sub("{id}", url)
    if url.endswith("/$"):
        return url[:-2]
    return url.rstrip(".$,;:)")


# ---------------------------------------------------------------------------
# Settings-object call handling ($.ajax, axios({...}))
# ---------------------------------------------------------------------------

def _append_settings_call_ref(
    body: str,
    args_start: int,
    call_start: int,
    refs: list[StaticEndpointRef],
    handled_spans: list[tuple[int, int]],
    global_env: dict[str, str],
) -> None:
    args = _extract_arguments(body, args_start, max_args=2)
    if not args:
        return
    env = _env_for_call(body, call_start, global_env)
    method = "GET"
    raw_url: str | None = None
    url_span = args[0][1]

    direct_url = _evaluate_url_expression(args[0][0], env)
    if direct_url is not None:
        raw_url = direct_url
        if len(args) > 1:
            method = _method_from_object_literal(args[1][0]) or method
    else:
        extracted = _url_from_object_literal(args[0][0], env)
        if extracted is None:
            return
        raw_url, relative_span = extracted
        url_span = _absolute_span(args[0][1], relative_span)
        method = _method_from_object_literal(args[0][0]) or method

    handled_spans.append(url_span)
    refs.append(StaticEndpointRef(method=method, raw_url=raw_url))


# ---------------------------------------------------------------------------
# Variable/assignment resolution
# ---------------------------------------------------------------------------

def _collect_api_assignments(
    body: str,
    *,
    allow_locals: bool = False,
    min_local_name_len: int = 1,
    seed_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build name→value environment from JS assignments.

    Iterates up to 3 times to resolve chained assignments like:
        const BASE = "/api/v1"
        const USERS = BASE + "/users"
    """
    env: dict[str, str] = dict(seed_env or {})
    for _ in range(3):
        changed = False
        for match in _ASSIGNMENT_RE.finditer(body):
            name = match.group("name")
            if not _should_keep_assignment_name(
                name,
                allow_locals=allow_locals,
                min_local_name_len=min_local_name_len,
            ):
                continue
            value = _evaluate_string_expression(match.group("expr"), env)
            if value is None:
                continue
            if not _should_keep_assignment_value(value, allow_locals=allow_locals):
                continue
            for alias in _assignment_aliases(name):
                if env.get(alias) != value:
                    env[alias] = value
                    changed = True
        for match in _OBJECT_ASSIGNMENT_RE.finditer(body):
            name = match.group("name")
            if not _should_keep_assignment_name(
                name,
                allow_locals=allow_locals,
                min_local_name_len=min_local_name_len,
            ):
                continue
            block = _extract_balanced_block(body, match.end() - 1)
            if block is None:
                continue
            object_expr, _span = block
            for prop, value_expr, _prop_span in _iter_top_level_object_properties(object_expr):
                value = _evaluate_string_expression(value_expr, env)
                if value is None:
                    continue
                if not _should_keep_assignment_value(value, allow_locals=allow_locals):
                    continue
                key = f"{name}.{prop}"
                if env.get(key) != value:
                    env[key] = value
                    changed = True
        if not changed:
            break
    return env


def _env_for_call(
    body: str,
    call_start: int,
    global_env: dict[str, str],
) -> dict[str, str]:
    """Build environment for a specific call site: global + nearby locals."""
    env = dict(global_env)
    env.update(_collect_api_assignments(
        _context_window(body, call_start, respect_boundaries=False),
        allow_locals=True,
        min_local_name_len=2,
    ))
    env.update(_collect_api_assignments(
        _context_window(body, call_start, max_chars=2_000),
        allow_locals=True,
        seed_env=env,
    ))
    return env


def _context_window(
    body: str,
    end: int,
    max_chars: int = 80_000,
    *,
    respect_boundaries: bool = True,
) -> str:
    start = max(0, end - max_chars)
    if respect_boundaries:
        for marker in ("class ", "})()});var ", "})();var ", "});var "):
            idx = body.rfind(marker, start, end)
            if idx >= 0:
                start = max(start, idx)
    return body[start:end]


def _looks_assignment_literal(body: str, api_start: int) -> bool:
    prefix = body[max(0, api_start - 200):api_start]
    last_delim = max(prefix.rfind(";"), prefix.rfind("\n"))
    if last_delim >= 0:
        prefix = prefix[last_delim + 1:]
    if any(marker in prefix for marker in ("http.", "fetch(", "axios.")):
        return False
    match = re.search(
        r"""(?P<name>(?:this\.)?[A-Za-z_$][\w$]*)\s*=\s*(?!>)""",
        prefix,
    )
    return bool(match and _should_keep_assignment_name(match.group("name")))


def _should_keep_assignment_name(
    name: str,
    *,
    allow_locals: bool = False,
    min_local_name_len: int = 1,
) -> bool:
    if name.startswith("this."):
        return True
    if name == "host":
        return True
    if allow_locals and "." not in name:
        return len(name) >= min_local_name_len
    return len(name) > 2


def _should_keep_assignment_value(value: str, *, allow_locals: bool = False) -> bool:
    if API_PREFIX_RE.search(value):
        return True
    if not allow_locals:
        return False
    if len(value) > 200 or any(ch.isspace() for ch in value):
        return False
    return "/" in value or value.endswith("/")


def _assignment_aliases(name: str) -> list[str]:
    if name.startswith("this."):
        return [name, name.removeprefix("this.")]
    if name == "host":
        return ["host", "this.host"]
    return [name]


# ---------------------------------------------------------------------------
# Argument extraction from function calls
# ---------------------------------------------------------------------------

def _extract_arguments(
    body: str,
    start: int,
    *,
    max_args: int,
) -> list[tuple[str, tuple[int, int]]]:
    """Extract function arguments as (text, span) pairs."""
    args: list[tuple[str, tuple[int, int]]] = []
    depth = 0
    quote: str | None = None
    escaped = False
    arg_start = start
    i = start
    while i < len(body):
        ch = body[i]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                if len(args) < max_args:
                    args.append((body[arg_start:i].strip(), (arg_start, i)))
                return args
            depth -= 1
        elif ch == "," and depth == 0:
            args.append((body[arg_start:i].strip(), (arg_start, i)))
            if len(args) >= max_args:
                return args
            arg_start = i + 1
        i += 1
    return args


def _extract_balanced_block(
    body: str,
    start: int,
) -> tuple[str, tuple[int, int]] | None:
    """Extract balanced {...} block."""
    if start >= len(body) or body[start] != "{":
        return None
    depth = 0
    quote: str | None = None
    escaped = False
    i = start
    while i < len(body):
        ch = body[i]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return body[start:i + 1], (start, i + 1)
        i += 1
    return None


# ---------------------------------------------------------------------------
# Object literal parsing
# ---------------------------------------------------------------------------

def _url_from_object_literal(
    expr: str,
    env: dict[str, str],
) -> tuple[str, tuple[int, int]] | None:
    for key, value_expr, span in _iter_top_level_object_properties(expr):
        if key not in _URL_OBJECT_KEYS:
            continue
        raw_url = _evaluate_url_expression(value_expr, env)
        if raw_url is not None:
            return raw_url, span
    return None


def _method_from_object_literal(expr: str) -> str | None:
    for key, value_expr, _span in _iter_top_level_object_properties(expr):
        if key not in _METHOD_OBJECT_KEYS:
            continue
        method = _method_from_expression(value_expr)
        if method is not None:
            return method
    return None


def _method_from_expression(expr: str) -> str | None:
    expr = _strip_wrappers(expr.strip())
    if not _is_string_literal(expr):
        return None
    method = _decode_js_string(expr).upper()
    return method if method in _HTTP_METHODS else None


def _iter_top_level_object_properties(
    expr: str,
) -> list[tuple[str, str, tuple[int, int]]]:
    expr = _strip_wrappers(expr.strip())
    if not (expr.startswith("{") and expr.endswith("}")):
        return []

    props: list[tuple[str, str, tuple[int, int]]] = []
    i = 1
    end = len(expr) - 1
    while i < end:
        while i < end and expr[i] in " \t\r\n,":
            i += 1
        parsed_key = _parse_object_key(expr, i)
        if parsed_key is None:
            i += 1
            continue
        key, i = parsed_key
        while i < end and expr[i].isspace():
            i += 1
        if i >= end or expr[i] != ":":
            continue
        value_start = i + 1
        value_end = _find_object_value_end(expr, value_start, end)
        props.append((
            key,
            expr[value_start:value_end].strip(),
            (value_start, value_end),
        ))
        i = value_end + 1

    return props


def _parse_object_key(expr: str, start: int) -> tuple[str, int] | None:
    if start >= len(expr):
        return None
    if expr[start] in ("'", '"'):
        quote = expr[start]
        escaped = False
        i = start + 1
        chars: list[str] = []
        while i < len(expr):
            ch = expr[i]
            if escaped:
                chars.append(ch)
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                return "".join(chars), i + 1
            else:
                chars.append(ch)
            i += 1
        return None
    match = re.match(r"""[A-Za-z_$][\w$-]*""", expr[start:])
    if match is None:
        return None
    return match.group(0), start + match.end()


def _find_object_value_end(expr: str, start: int, end: int) -> int:
    depth = 0
    quote: str | None = None
    escaped = False
    i = start
    while i < end:
        ch = expr[i]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            return i
        i += 1
    return end


def _absolute_span(
    outer_span: tuple[int, int],
    inner_span: tuple[int, int],
) -> tuple[int, int]:
    outer_start, _outer_end = outer_span
    inner_start, inner_end = inner_span
    return outer_start + inner_start, outer_start + inner_end


# ---------------------------------------------------------------------------
# String expression evaluation
# ---------------------------------------------------------------------------

def _evaluate_url_expression(expr: str, env: dict[str, str]) -> str | None:
    value = _evaluate_string_expression(expr, env)
    if value is None:
        return None
    return _api_url_value(value)


def _evaluate_string_expression(expr: str, env: dict[str, str]) -> str | None:
    expr = _strip_wrappers(expr.strip())
    expr = _strip_simple_string_method(expr)
    if not expr:
        return None
    if expr.startswith("{") and expr.endswith("}"):
        return None
    if expr in env:
        return env[expr]
    if _is_string_literal(expr):
        return _decode_js_string(expr)
    if expr.startswith("`") and expr.endswith("`"):
        return _evaluate_template_literal(expr, env)

    parts = _split_top_level_plus(expr)
    if len(parts) > 1:
        rendered = ""
        for part in parts:
            rendered += _evaluate_expression_part(part, env, rendered)
        return rendered

    if API_PREFIX_RE.search(expr):
        return expr
    return None


def _evaluate_expression_part(
    part: str,
    env: dict[str, str],
    rendered_prefix: str = "",
) -> str:
    part = _strip_wrappers(part.strip())
    part = _strip_simple_string_method(part)
    if not part:
        return ""
    if part in env:
        return env[part]
    if _is_string_literal(part):
        return _decode_js_string(part)
    if part.startswith("`") and part.endswith("`"):
        return _evaluate_template_literal(part, env)
    if "hostServer" in part:
        return ""
    return "{id}" if rendered_prefix.endswith("/") else ""


def _strip_simple_string_method(expr: str) -> str:
    for method in (".replace(", ".split(", ".trim("):
        idx = expr.find(method)
        if idx > 0:
            return expr[:idx].strip()
    return expr


def _evaluate_template_literal(expr: str, env: dict[str, str]) -> str:
    inner = expr[1:-1]
    out: list[str] = []
    i = 0
    while i < len(inner):
        if inner.startswith("${", i):
            end = _find_template_expr_end(inner, i + 2)
            if end is None:
                out.append("{id}" if "".join(out).endswith("/") else "")
                break
            rendered = _evaluate_url_expression(inner[i + 2:end], env)
            if rendered is not None:
                out.append(rendered)
            else:
                out.append("{id}" if "".join(out).endswith("/") else "")
            i = end + 1
            continue
        out.append(inner[i])
        i += 1
    return "".join(out)


def _find_template_expr_end(text: str, start: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    i = start
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            if depth == 0:
                return i
            depth -= 1
        i += 1
    return None


def _split_top_level_plus(expr: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for i, ch in enumerate(expr):
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "+" and depth == 0:
            parts.append(expr[start:i])
            start = i + 1
    parts.append(expr[start:])
    return parts


def _strip_wrappers(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        expr = expr[1:-1].strip()
    return expr


def _is_string_literal(expr: str) -> bool:
    return (
        len(expr) >= 2
        and expr[0] == expr[-1]
        and expr[0] in ("'", '"')
    )


def _decode_js_string(expr: str) -> str:
    inner = expr[1:-1]
    try:
        return bytes(inner, "utf-8").decode("unicode_escape").replace("\\/", "/")
    except Exception:
        return inner.replace("\\/", "/")


def _api_url_value(value: str) -> str | None:
    match = API_PREFIX_RE.search(value)
    if match is None:
        return None
    sanitized = sanitize_candidate_url(value)
    if sanitized.startswith(("http://", "https://", "/", "./", "../")):
        return sanitized
    return "/" + sanitized.lstrip("/")


def _inside_any(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(
        other_start <= start and end <= other_end
        for other_start, other_end in spans
    )
