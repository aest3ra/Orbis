"""Real browser checks for transitions, writes, uploads and observed downloads."""

import asyncio
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from orbis.analysis.analyzer import analyze
from orbis.config import AuthConfig, InteractionConfig, LLMConfig, ScanConfig, ScopeConfig
from orbis.crawler.browser import capture_page
from orbis.crawler.scope import Scope

ATTACHMENT = b"orbis integration attachment\n"
APP_JS = """
window.unusedPost = () => fetch('/api/readonly', {method: 'POST'});
async function render() {
  const path = location.pathname;
  const menus = Array.from({length: 8}, (_, i) => `<a role="button" href="/sidebar/${i}">Menu ${i}</a>`).join(' ');
  document.body.innerHTML = '<nav>' + menus + '</nav><main id="app"></main>';
  const app = document.querySelector('#app');
  if (path === '/') {
    app.innerHTML = '<a href="/workspace">Workspace</a><a href="/inventory">Inventory</a>';
  } else if (path === '/workspace') {
    await fetch('/api/page/workspace');
    app.innerHTML = '<nav><a role="button" href="#composer" id="new">New post</a></nav><a href="/inventory">Inventory</a>';
    document.querySelector('#new').onclick = async event => {
      event.preventDefault(); event.stopPropagation(); (await import('/editor.js')).show();
    };
  } else {
    await fetch('/api/page/inventory');
    app.innerHTML = '<button role="tab" id="details">Details</button><a href="/workspace">Workspace</a>';
    document.querySelector('#details').onclick = async () => {
      await fetch('/api/inventory');
      app.innerHTML = '<p>Inventory loaded</p><a href="/workspace">Workspace</a>';
    };
  }
}
document.addEventListener('click', event => {
  const link = event.target.closest('a');
  if (!link || link.hasAttribute('download')) return;
  event.preventDefault();
  history.pushState({}, '', link.href);
  render();
});
window.onpopstate = render;
render();
"""
EDITOR_JS = """
export async function show() {
  await new Promise(resolve => setTimeout(resolve, 650));
  await fetch('/api/editor-options');
  document.querySelector('#app').innerHTML = `<form method="post" action="/api/posts">
    <label>Title<input name="title" required minlength="4"></label>
    <label>Content<textarea name="content" required minlength="10"></textarea></label>
    <label>Section<select name="section" required><option value="">Choose</option><option value="general">General</option></select></label>
    <input type="file" name="attachment" accept=".txt" hidden>
    <button type="submit">Publish</button></form>`;
  const form = document.querySelector('form');
  form.attachment.onchange = async () => {
    const data = new FormData();
    data.append('attachment', form.attachment.files[0]);
    await fetch('/api/uploads', {method: 'POST', body: data});
  };
  form.onsubmit = async event => {
    event.preventDefault();
    const response = await fetch('/api/posts', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: form.title.value, content: form.content.value, section: form.section.value})});
    if (response.ok) document.querySelector('#app').innerHTML = '<p>Saved</p><a href="/api/files/manual.txt" download>Download attachment</a><a href="/inventory">Inventory</a>';
  };
}
"""


@pytest.fixture
def workflow_site():
    state = {"requests": [], "posts": [], "uploaded": False, "llm_requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_body(self, body, mime="application/json", status=200, attachment=False):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            if attachment:
                self.send_header("Content-Disposition", 'attachment; filename="manual.txt"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["requests"].append(("GET", self.path))
            if self.path == "/logging":
                self.send_body(b'''<button type="button" onclick="fetch('/api/logging-failure?token=private-token',{method:'POST'})">Save first</button>
                  <button type="button" onclick="fetch('/api/logging-failure?token=private-token',{method:'POST'})">Save again</button>
                  <button type="button">Options tab</button>''', "text/html")
            elif self.path == "/post-download":
                self.send_body(b'<form method="post" action="/api/export"><button type="submit">Download report</button></form>', "text/html")
            elif self.path == "/chooser":
                self.send_body(b'''<button id="upload" type="button">Upload File</button><script>
                  document.querySelector('#upload').onclick = () => {
                    const input = document.createElement('input'); input.type = 'file'; input.accept = '.txt';
                    input.onchange = async () => {const data = new FormData(); data.append('file', input.files[0]);
                      await fetch('/api/uploads', {method: 'POST', body: data});}; input.click();
                  };
                </script>''', "text/html")
            elif self.path == "/state-steps":
                self.send_body(b'''<button id="advance" role="tab" aria-selected="false">Next</button><script>
                  let step = 0;
                  document.querySelector('#advance').onclick = async event => {
                    step += 1;
                    event.target.setAttribute('aria-selected', step === 1 ? 'true' : 'false');
                    if (step === 2) await fetch('/api/state-steps');
                  };
                </script>''', "text/html")
            elif self.path == "/unique-routes":
                self.send_body(b'''<button id="refresh" role="tab" aria-selected="0">Refresh</button><script>
                  let n = 0;
                  document.querySelector('#refresh').onclick = async event => {
                    n += 1;
                    event.target.setAttribute('aria-selected', String(n));
                    await fetch('/api/items/' + Date.now());
                  };
                </script>''', "text/html")
            elif self.path == "/app.js":
                self.send_body(APP_JS.encode(), "text/javascript")
            elif self.path == "/editor.js":
                self.send_body(EDITOR_JS.encode(), "text/javascript")
            elif self.path == "/api/files/manual.txt":
                self.send_body(ATTACHMENT, "text/plain", attachment=True)
            elif self.path.startswith("/api/"):
                self.send_body(b'{"ok": true}')
            else:
                self.send_body(b'<html><body><script src="/app.js"></script></body></html>', "text/html")

        def do_POST(self):
            state["requests"].append(("POST", self.path))
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path.startswith("/api/logging-failure"):
                self.send_body(b'{"error":"private-error-body"}', status=500)
                return
            if self.path == "/api/export":
                self.send_body(ATTACHMENT, "text/plain", attachment=True)
                return
            if self.path == "/v1/chat/completions":
                state["llm_requests"].append({"body": json.loads(body), "cookie": self.headers.get("Cookie")})
                self.send_body(json.dumps({"choices": [{"message": {"content": '{"candidate_id": 0}'}}]}).encode())
                return
            if self.path == "/api/uploads":
                state["uploaded"] = ATTACHMENT in body
            elif self.path == "/api/posts":
                data = json.loads(body)
                if not state["uploaded"] or len(data["title"]) < 4 or len(data["content"]) < 10 or not data["section"]:
                    self.send_body(b'{}', status=422)
                    return
                state["posts"].append(data)
            self.send_body(b'{"ok": true}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_spa_write_upload_download_are_observed(workflow_site, tmp_path):
    target, state = workflow_site
    upload = tmp_path / "fixture.txt"
    upload.write_bytes(ATTACHMENT)
    scope = Scope(ScopeConfig(include_domains=["127.0.0.1"]))

    async def exercise():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                capture = await capture_page(
                    context, target, scope=scope, max_scrolls=0, settle_ms=50, max_actions=12,
                    interaction=InteractionConfig(allow_write=True, upload_files=[upload]),
                    download_dir=tmp_path / "downloads",
                )
                assert not context.pages
                return capture
            finally:
                await browser.close()

    capture = asyncio.run(exercise())
    endpoints = {(ep.method, ep.path_template) for ep in analyze(capture, scope).endpoints if ep.source == "dynamic"}
    expected = {("GET", "/api/page/workspace"), ("GET", "/api/page/inventory"), ("GET", "/api/inventory"),
                ("GET", "/api/editor-options"), ("POST", "/api/uploads"), ("POST", "/api/posts"),
                ("GET", "/api/files/manual.txt")}
    assert expected <= endpoints, (expected - endpoints, capture.observations)
    assert len(state["posts"]) == 1
    assert state["uploaded"]
    assert any(body.url.endswith("/editor.js") for body in capture.selective_bodies)
    downloads = [row for row in capture.observations if row.get("kind") == "download"]
    assert len(downloads) == 1
    assert downloads[0]["sha256"] == hashlib.sha256(ATTACHMENT).hexdigest()


def test_readonly_exploration_does_not_submit_posts(workflow_site, tmp_path):
    target, state = workflow_site
    scope = Scope(ScopeConfig(include_domains=["127.0.0.1"]))

    async def exercise():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                return await capture_page(context, target, scope=scope, max_scrolls=0, settle_ms=50,
                                          download_dir=tmp_path, max_actions=12)
            finally:
                await browser.close()

    capture = asyncio.run(exercise())
    assert ("GET", "/api/inventory") in state["requests"]
    assert not [row for row in state["requests"] if row[0] == "POST"]
    assert not capture.error


def test_same_url_screen_states_allow_next_step_request(workflow_site, tmp_path):
    target, state = workflow_site
    scope = Scope(ScopeConfig(include_domains=["127.0.0.1"]))

    async def exercise():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                return await capture_page(context, target + "/state-steps", scope=scope, max_scrolls=0,
                                          settle_ms=50, download_dir=tmp_path, max_actions=4)
            finally:
                await browser.close()

    capture = asyncio.run(exercise())

    assert ("GET", "/api/state-steps") in state["requests"]
    assert ("GET", "/api/state-steps") in {
        (endpoint.method, endpoint.path_template) for endpoint in analyze(capture, scope).endpoints
    }
    assert [row["label"] for row in capture.observations if row.get("observation") == "action"] == ["Next", "Next"]
    assert not capture.error


def test_capture_failure_closes_real_page(workflow_site, monkeypatch):
    from orbis.crawler import browser as capture_module
    target, _state = workflow_site

    async def broken(_page):
        raise RuntimeError("injected collection failure")

    monkeypatch.setattr(capture_module, "_collect_inline_data", broken)

    async def exercise():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                with pytest.raises(RuntimeError, match="injected"):
                    await capture_page(context, target, max_scrolls=0, settle_ms=0)
                assert not context.pages
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_native_file_chooser_uses_only_configured_attachment(workflow_site, tmp_path):
    target, state = workflow_site
    upload = tmp_path / "fixture.txt"
    upload.write_bytes(ATTACHMENT)
    scope = Scope(ScopeConfig(include_domains=["127.0.0.1"]))

    async def exercise():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                capture = await capture_page(context, target + "/chooser", scope=scope,
                                             max_scrolls=0, settle_ms=0, max_actions=1,
                                             interaction=InteractionConfig(allow_write=True, upload_files=[upload]))
                assert not context.pages
                return capture
            finally:
                await browser.close()

    capture = asyncio.run(exercise())
    assert state["uploaded"]
    assert any(event.method == "POST" and event.url.endswith('/api/uploads') for event in capture.network_events)


def test_deadline_preserves_observations_and_closes_page(workflow_site):
    import time
    target, _state = workflow_site

    async def exercise():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                result = await capture_page(context, target, max_scrolls=0, settle_ms=1000,
                                            deadline=time.monotonic() + 0.4)
                assert result.error == "ScanDeadlineExceeded"
                assert result.network_events
                assert not context.pages
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_scan_uses_isolated_llm_transport_and_persists_evidence(workflow_site, tmp_path, monkeypatch):
    from sqlmodel import Session, select
    from orbis.crawler.runner import run_scan
    from orbis.storage.db import Endpoint, Observation, open_db

    target, state = workflow_site
    monkeypatch.setenv("NVIDIA_API_KEY", "fixture-only-key")
    upload = tmp_path / "fixture.txt"
    upload.write_bytes(ATTACHMENT)
    auth = tmp_path / "state.json"
    auth.write_text(json.dumps({"cookies": [{"name": "session", "value": "private-session", "domain": "127.0.0.1",
                                           "path": "/", "httpOnly": True, "secure": False, "sameSite": "Lax"}], "origins": []}))
    config = ScanConfig(target=target, auth=AuthConfig(storage_state_path=auth),
                        interaction=InteractionConfig(allow_write=True, upload_files=[upload]),
                        llm=LLMConfig(enabled=True, base_url=target + "/v1", max_calls=20))
    config.limits.max_pages = 1
    config.limits.max_scrolls_per_page = 0
    config.limits.probe_max_requests = 0
    database = tmp_path / "scan.db"
    scan_id = asyncio.run(run_scan(config, db_path=str(database), wayback=False))
    log_text = next(tmp_path.glob("scan.scan-*.jsonl")).read_text()
    log_rows = [json.loads(line) for line in log_text.splitlines()]
    assert not any(value in log_text for value in ("private-session", "fixture-only-key"))
    assert any(row["event"] == "llm_end" and row["outcome"] == "selected" for row in log_rows)
    assert {row["event"] for row in log_rows} >= {"action_end", "download_end", "llm_end"}
    assert any(row.get("status") == "saved" for row in log_rows if row["event"] == "download_end")
    assert all("action_id" in row and "duration_ms" in row for row in log_rows if row["event"] == "llm_end")
    assert state["llm_requests"]
    assert all(record["cookie"] is None for record in state["llm_requests"])
    assert len(state["posts"]) == 1
    engine = open_db(database)
    try:
        with Session(engine) as session:
            endpoints = session.exec(select(Endpoint).where(Endpoint.scan_id == scan_id)).all()
            assert any(ep.route_kind == "download" for ep in endpoints)
            assert {row.kind for row in session.exec(select(Observation).where(Observation.scan_id == scan_id)).all()} == {"scan"}
    finally:
        engine.dispose()


def test_http_failures_no_progress_and_new_endpoints_are_distinct(workflow_site, tmp_path):
    from sqlmodel import Session, select
    from orbis.crawler.runner import run_scan
    from orbis.storage.db import Observation, open_db

    target, _state = workflow_site
    config = ScanConfig(target=target + "/logging", interaction=InteractionConfig(allow_write=True))
    config.limits.max_pages = 1
    config.limits.max_scrolls_per_page = 0
    config.limits.max_actions_per_page = 3
    config.limits.probe_max_requests = 0
    database = tmp_path / "logging.db"
    asyncio.run(run_scan(config, db_path=str(database), wayback=False))
    text = next(tmp_path.glob("logging.scan-*.jsonl")).read_text()
    assert "private-token" not in text and "private-error-body" not in text
    rows = [json.loads(line) for line in text.splitlines()]
    actions = [row for row in rows if row["event"] == "action_end"]
    assert len(actions) == 3
    assert [row["status"] for row in actions] == ["executed"] * 3
    assert [row["http_error_count"] for row in actions] == [1, 1, 0]
    assert [row["new_request_routes"] for row in actions] == [1, 0, 0]
    assert [row["observed_progress"] for row in actions] == [True, False, False]
    assert all(row["application_outcome"] == "unverified" for row in actions)
    requests = [row for row in rows if row["event"] == "request_observed" and row["http_status"] == 500]
    assert [row["action_id"] for row in requests] == [1, 2]
    page = next(row for row in rows if row["event"] == "page_end")
    assert page["endpoints_new"] > 0 and page["endpoints_observed"] >= page["endpoints_new"]
    assert page["actions_with_http_errors"] == 2
    assert rows[-1]["stop_reason"] == "max_pages"
    assert rows[-1]["totals"]["actions_without_observed_progress"] == 2


def test_get_probe_does_not_verify_static_post_candidate(workflow_site, tmp_path):
    from sqlmodel import Session, select
    from orbis.crawler.runner import run_scan
    from orbis.storage.db import Endpoint, open_db

    target, state = workflow_site
    config = ScanConfig(target=target)
    config.limits.max_pages = 1
    config.limits.max_scrolls_per_page = 0
    config.limits.max_actions_per_page = 0
    config.limits.probe_max_requests = 3
    database = tmp_path / 'probe.db'
    scan_id = asyncio.run(run_scan(config, db_path=str(database), wayback=False))
    engine = open_db(database)
    try:
        with Session(engine) as session:
            endpoint = session.exec(select(Endpoint).where(Endpoint.scan_id == scan_id,
                                                           Endpoint.path_template == '/api/readonly')).one()
            assert endpoint.method == 'POST'
            assert endpoint.probe_status == 'unverified'
            assert endpoint.probe_code is None
    finally:
        engine.dispose()
    assert ('GET', '/api/readonly') not in state['requests']


def test_post_download_preserves_method_and_separate_artifacts(workflow_site, tmp_path):
    from orbis.crawler.interactor import ExplorationState
    target, _state = workflow_site
    scope = Scope(ScopeConfig(include_domains=['127.0.0.1']))
    exploration = ExplorationState()

    async def exercise():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                return await capture_page(context, target + '/post-download', scope=scope,
                                          max_scrolls=0, settle_ms=0, max_actions=1,
                                          interaction=InteractionConfig(allow_write=True), download_dir=tmp_path,
                                          exploration=exploration)
            finally:
                await browser.close()

    capture = asyncio.run(exercise())
    downloads = [ep for ep in analyze(capture, scope).endpoints if ep.route_kind == 'download']
    assert [(ep.method, ep.path_template) for ep in downloads] == [('POST', '/api/export')]
    evidence = next(row for row in capture.observations if row.get('kind') == 'download')
    assert evidence['method_evidence'] == 'network_request'
    assert evidence['action_id'] == 1 and evidence['content_seen_count'] == 1
    first_action = next(row for row in capture.observations if row.get('observation') == 'action')
    assert first_action['network_failure_count'] == 0
    assert all(event.loading_error is None or event.resource_type == 'Download'
               for event in capture.network_events if event.url.endswith('/api/export'))
    exploration.acted.clear()  # Simulate revisiting an operation after a caller resets candidate history.
    second = asyncio.run(exercise())
    second_record = next(row for row in second.observations if row.get('kind') == 'download')
    assert second_record['action_id'] == 2 and second_record['content_seen_count'] == 2
    assert next(row for row in second.observations if row.get('observation') == 'action')['attempt_number'] == 2
    second_file = second_record['path']
    assert second_file != evidence['path']
    assert Path(second_file).read_bytes() == Path(evidence['path']).read_bytes() == ATTACHMENT


def test_route_novelty_ignores_high_cardinality_paths(workflow_site, tmp_path):
    """Each click fetches /api/items/<timestamp>. Templatized route novelty makes
    the second click unproductive, so the per-URL cap stops the control after two
    executions instead of chasing a new path forever."""
    target, state = workflow_site
    scope = Scope(ScopeConfig(include_domains=["127.0.0.1"]))

    async def exercise():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context()
                return await capture_page(context, target + "/unique-routes", scope=scope, max_scrolls=0,
                                          settle_ms=50, download_dir=tmp_path, max_actions=6)
            finally:
                await browser.close()

    capture = asyncio.run(exercise())

    actions = [row for row in capture.observations if row.get("observation") == "action" and row["label"] == "Refresh"]
    assert [row["status"] for row in actions] == ["executed", "executed"]
    assert actions[0]["new_request_routes"] >= 1
    assert actions[1]["new_request_routes"] == 0
    assert len([path for method, path in state["requests"] if path.startswith("/api/items/")]) == 2
    assert not capture.error
