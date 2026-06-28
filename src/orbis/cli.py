"""orbis CLI: scan, login, list, inspect."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import typer
from rich import print
from rich.table import Table
from sqlmodel import Session

from orbis.config import CRAWL_PRESETS, AuthConfig, ScanConfig, load_config
from orbis.crawler.runner import run_scan
from orbis.storage.db import open_db
from orbis.storage.repo import get_endpoint_with_params, list_endpoints

app = typer.Typer(no_args_is_help=True, add_completion=False)
CrawlMode = Literal["quick", "deep", "exhaustive"]


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="  %(message)s",
        handlers=[logging.StreamHandler()],
    )
    if not verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command()
def scan(
    target_or_config: str = typer.Argument(..., help="Target URL or YAML config."),
    db: Path | None = typer.Option(None, "--db"),
    auth: Path | None = typer.Option(None, "--auth", exists=True),
    headless: bool = typer.Option(True, "--headless/--no-headless"),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1),
    max_depth: int | None = typer.Option(None, "--max-depth", min=0),
    max_duration: int | None = typer.Option(None, "--max-duration", min=1),
    per_template: int | None = typer.Option(None, "--per-template", min=1),
    max_scrolls: int | None = typer.Option(None, "--max-scrolls", min=0),
    crawl_mode: CrawlMode | None = typer.Option(None, "--crawl-mode"),
    js_analysis: bool = typer.Option(True, "--js-analysis/--no-js-analysis",
                                     help="Enable external JS static analysis."),
    passive: bool = typer.Option(True, "--passive/--no-passive",
                                 help="Pull archived URLs (Wayback) as a passive layer."),
    probe: bool = typer.Option(True, "--probe/--no-probe",
                               help="Actively verify unobserved endpoints with safe GETs."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Crawl target and collect API endpoints."""
    _setup_logging(verbose)
    config = _load_config(target_or_config)

    if auth:
        config.auth = AuthConfig(type="storage_state", storage_state_path=auth)
    if crawl_mode:
        for k, v in CRAWL_PRESETS[crawl_mode].items():
            setattr(config.limits, k, v)
    if max_pages is not None:
        config.limits.max_pages = max_pages
    if max_depth is not None:
        config.limits.max_depth = max_depth
    if max_duration is not None:
        config.limits.max_duration_sec = max_duration
    if per_template is not None:
        config.limits.max_visits_per_template = per_template
    if max_scrolls is not None:
        config.limits.max_scrolls_per_page = max_scrolls

    db_path = db or _default_db(config.target)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[bold]orbis scan[/bold] {config.target}")
    print(f"  db:     {db_path}")
    print(f"  scope:  {config.scope.include_domains}")
    depth_str = str(config.limits.max_depth) if config.limits.max_depth is not None else "unlimited"
    js_str = "on" if js_analysis else "off"
    passive_str = "on" if passive else "off"
    probe_str = "on" if probe else "off"
    print(f"  limits: {config.limits.max_pages} pages, {config.limits.max_duration_sec}s, depth={depth_str}")
    print(f"  static: js_analysis={js_str}  passive={passive_str}  probe={probe_str}\n")

    scan_id = asyncio.run(run_scan(
        config, db_path=str(db_path), headless=headless,
        js_analysis=js_analysis, passive=passive, probe=probe,
    ))

    engine = open_db(db_path)
    with Session(engine) as session:
        endpoints = list_endpoints(session, scan_id)

    print()
    if not endpoints:
        print("[dim]no API endpoints found[/dim]")
        return
    _print_table(endpoints, title=f"Endpoints (scan #{scan_id})")
    print(f"\n[bold green]done[/bold green] {len(endpoints)} endpoints -> {db_path}")


@app.command()
def login(
    target_url: str = typer.Argument(..., help="URL to open for login."),
    out: Path = typer.Option(Path("auth.json"), "-o", "--out"),
) -> None:
    """Open browser for manual login, save session state."""
    asyncio.run(_login_flow(target_url, out))


@app.command(name="list")
def list_cmd(
    db_path: Path = typer.Argument(..., exists=True, help="orbis DB path."),
    kind: str | None = typer.Option(None, "--kind", help="Filter by route_kind."),
    source: str | None = typer.Option(None, "--source",
                                      help="Filter by source (dynamic|static_js|static_openapi|static_docs)."),
    probe_status: str | None = typer.Option(None, "--probe-status",
                                            help="Filter by probe_status (unverified|verified|failed)."),
) -> None:
    """List all endpoints in the DB."""
    engine = open_db(db_path)
    with Session(engine) as session:
        endpoints = list_endpoints(session)
    if kind:
        endpoints = [e for e in endpoints if e.route_kind == kind]
    if source:
        endpoints = [e for e in endpoints if e.source == source]
    if probe_status:
        endpoints = [e for e in endpoints if e.probe_status == probe_status]
    if not endpoints:
        print("[dim]no endpoints found[/dim]")
        return
    _print_table(endpoints)


@app.command()
def inspect(
    db_path: Path = typer.Argument(..., exists=True, help="orbis DB path."),
    endpoint_id: int = typer.Argument(..., help="Endpoint ID."),
) -> None:
    """Show detail for a single endpoint."""
    engine = open_db(db_path)
    with Session(engine) as session:
        ep, params = get_endpoint_with_params(session, endpoint_id)

    if ep is None:
        typer.secho(f"endpoint #{endpoint_id} not found", fg="red", err=True)
        raise typer.Exit(1)

    print(f"[bold cyan]#{ep.id}[/bold cyan] [bold]{ep.method}[/bold] {ep.host}{ep.path_template}")
    print(f"  kind:   {ep.route_kind}")
    print(f"  source: {ep.source}")
    probe_code = f" ({ep.probe_code})" if ep.probe_code is not None else ""
    print(f"  probe:  {ep.probe_status or 'n/a'}{probe_code}")
    print(f"  via:    {ep.discovered_via or 'passive load'}")
    print(f"  sample: {ep.sample_url}")
    print(f"  seen:   {ep.seen_count}")

    if not params:
        print("  [dim]no parameters[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("location")
    table.add_column("name")
    table.add_column("type")
    table.add_column("seen", justify="right")
    table.add_column("samples")
    for p in params:
        samples = json.loads(p.sample_values_json)
        table.add_row(
            p.location, p.name, p.type_inferred,
            str(p.seen_count), ", ".join(repr(s) for s in samples[:3]),
        )
    print(table)


def _print_table(endpoints, title: str = "Endpoints") -> None:
    table = Table(title=title, show_header=True, header_style="bold")
    table.add_column("id", justify="right")
    table.add_column("method", style="bold")
    table.add_column("host")
    table.add_column("path")
    table.add_column("kind")
    table.add_column("source")
    table.add_column("probe")
    table.add_column("code", justify="right")
    table.add_column("seen", justify="right")
    for ep in endpoints:
        probe_code = str(ep.probe_code) if ep.probe_code is not None else "-"
        table.add_row(
            str(ep.id), ep.method, ep.host,
            ep.path_template, ep.route_kind,
            ep.source, ep.probe_status or "-",
            probe_code,
            str(ep.seen_count),
        )
    print(table)


def _load_config(value: str) -> ScanConfig:
    path = Path(value).expanduser()
    if path.exists() and path.suffix in (".yaml", ".yml"):
        return load_config(path)
    return ScanConfig(target=value)


def _default_db(target: str) -> Path:
    host = urlparse(target).hostname or "target"
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", host).strip("-").lower()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    h = hashlib.sha256(target.encode()).hexdigest()[:6]
    return Path("runs") / f"orbis-{ts}_{slug}_{h}.db"


async def _login_flow(url: str, out: Path) -> None:
    from playwright.async_api import async_playwright

    out.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        print(f"\n[bold]Log in in the browser, then press Enter here:[/bold]")
        await asyncio.to_thread(input)
        state = await context.storage_state(path=str(out))
        await browser.close()
    cookies = len(state.get("cookies", []))
    print(f"[green]done[/green] saved {out} ({cookies} cookies)")


def main() -> None:
    app()
