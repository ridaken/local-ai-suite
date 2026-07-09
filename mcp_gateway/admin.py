"""Server-rendered admin UI: dashboard, ZIM sources (installed + catalog +
downloads), and retrieval settings. Deliberately no build chain — plain HTML
forms, full-page POST/redirect/GET, a `<meta refresh>` for the one page
(downloads) that benefits from auto-updating. This is a human-only surface:
never exposed as MCP tools, and bound to loopback by default (config.ADMIN_HOST).

build_admin_app() takes its dependencies (settings store, download manager,
paths) as arguments rather than reaching for module globals, so tests can spin
up a Starlette TestClient against fakes without touching the network or a real
ZIM_DIR.
"""

from __future__ import annotations

import html
import shutil
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from . import catalog as catalog_client
from . import config, recommendations, zim_library
from .catalog import CatalogEntry
from .downloads import DownloadManager, delete_zim
from .settings_store import SettingsStore

_NAV = [
    ("/", "Dashboard"),
    ("/sources", "Sources"),
    ("/recommendations", "Recommended"),
    ("/catalog", "Catalog"),
    ("/downloads", "Downloads"),
    ("/settings", "Settings"),
]

_CSS = """
body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:#0e0e10;color:#eaeaea}
@media (prefers-color-scheme: light){body{background:#fafafa;color:#111}}
header{padding:1rem 1.5rem;border-bottom:1px solid #3a3a3a}
header h1{margin:0 0 .5rem;font-size:1.1rem}
nav a{margin-right:1.25rem;color:#7db9e8;text-decoration:none;font-size:.95rem}
nav a:hover{text-decoration:underline}
main{padding:1.5rem;max-width:960px;margin:0 auto}
h2{font-size:1.1rem;margin-top:2rem}
table{width:100%;border-collapse:collapse;margin:.75rem 0}
th,td{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #333;vertical-align:top}
.badge{padding:.15rem .5rem;border-radius:1rem;font-size:.78rem;white-space:nowrap}
.ok{background:#1f4d2b;color:#8fe0a3}
.down{background:#4d1f1f;color:#e08f8f}
.warn{background:#4d431f;color:#e0cf8f}
form.inline{display:inline}
button{cursor:pointer;padding:.25rem .6rem}
input[type=text]{padding:.3rem .5rem;width:280px}
.muted{opacity:.7;font-size:.85rem}
.error{color:#e08f8f}
.setup-banner{background:#4d431f;color:#f0e4b0;border-radius:.4rem;padding:.75rem 1rem;
  margin-bottom:1.25rem}
.setup-banner strong{display:block;margin-bottom:.35rem}
.setup-banner ul{margin:.25rem 0 0;padding-left:1.2rem}
.setup-banner code{background:rgba(255,255,255,.12);padding:0 .25rem;border-radius:.2rem}
"""


def _page(title: str, body: str, *, extra_head: str = "") -> HTMLResponse:
    nav_html = "".join(f'<a href="{href}">{label}</a>' for href, label in _NAV)
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)} — local-ai-suite</title>"
        f"<style>{_CSS}</style>{extra_head}</head><body>"
        f"<header><h1>local-ai-suite admin</h1><nav>{nav_html}</nav></header>"
        f"<main>{body}</main></body></html>"
    )


def _human_bytes(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


async def _reachable(url: str) -> str:
    """Best-effort liveness check: any HTTP response (even 404/405) counts as
    reachable — the goal is "is something listening", not endpoint semantics,
    since embed/rerank URLs point at POST-only paths."""
    if not url:
        return "unconfigured"
    try:
        async with httpx.AsyncClient(timeout=3.0, follow_redirects=True) as client:
            await client.get(url)
        return "reachable"
    except httpx.HTTPError:
        return "unreachable"


def _badge(status: str) -> str:
    cls = {"reachable": "ok", "unreachable": "down"}.get(status, "warn")
    return f'<span class="badge {cls}">{html.escape(status)}</span>'


def _fulltext_badge(has_fulltext_index: bool | None) -> str:
    if has_fulltext_index is False:
        return ' <span class="badge warn">no full-text index</span>'
    if has_fulltext_index is True:
        return ' <span class="badge ok">full-text index</span>'
    return ""


def _download_action(entry: CatalogEntry, zim_dir_path: Path | None) -> str:
    if not entry.download_url:
        return '<span class="muted">unavailable</span>'
    if zim_dir_path is None:
        return '<span class="muted">set ZIM_DIR first</span>'
    filename = entry.download_url.rsplit("/", 1)[-1]
    return (
        '<form method="post" action="/sources/download">'
        f'<input type="hidden" name="url" value="{html.escape(entry.download_url)}">'
        f'<input type="hidden" name="filename" value="{html.escape(filename)}">'
        '<button type="submit">Download</button></form>'
    )


def _setup_issues(zim_dir_path: Path | None) -> list[str]:
    """The one thing that actually blocks correct behavior when unset: without
    a data directory, downloads have nowhere durable to land and never show up
    as installed sources. Deliberately not a general "everything optional is
    unset" nag list (blank KAGI_API_KEY / no vector tier already self-document
    via their own tool responses and the dashboard's reachability badges)."""
    if zim_dir_path is not None:
        return []
    return [
        "No data directory is set (<code>ZIM_DIR</code>) — Catalog downloads have "
        "nowhere durable to go until you set it. Add it to <code>config/.env</code> "
        "(host mode) or the <code>gateway</code> service's environment in "
        "<code>docker-compose.yml</code> (Docker), then restart the gateway."
    ]


def _setup_banner(issues: list[str]) -> str:
    if not issues:
        return ""
    items = "".join(f"<li>{issue}</li>" for issue in issues)
    return f'<div class="setup-banner"><strong>Setup needed</strong><ul>{items}</ul></div>'


def build_admin_app(
    *,
    settings: SettingsStore,
    download_manager: DownloadManager,
    zim_dir: str,
    library_xml_path: str,
) -> Starlette:
    zim_dir_path = Path(zim_dir) if zim_dir else None

    def _render(title: str, body: str, *, extra_head: str = "") -> HTMLResponse:
        banner = _setup_banner(_setup_issues(zim_dir_path))
        return _page(title, banner + body, extra_head=extra_head)

    def _refresh_library() -> None:
        if zim_dir_path and library_xml_path:
            zim_library.refresh_library(zim_dir_path, Path(library_xml_path))

    async def dashboard(request: Request) -> HTMLResponse:
        kiwix_status, qdrant_status, embed_status, rerank_status = [
            await _reachable(url)
            for url in (config.KIWIX_URL, config.QDRANT_URL, config.EMBED_URL, config.RERANK_URL)
        ]
        books = zim_library.scan_zim_dir(zim_dir_path) if zim_dir_path else []
        corpus_bytes = sum(b.size_bytes for b in books)
        disk_line = ""
        if zim_dir_path and zim_dir_path.is_dir():
            usage = shutil.disk_usage(zim_dir_path)
            disk_line = (
                f"<p>Data drive: {_human_bytes(usage.used)} used / "
                f"{_human_bytes(usage.total)} total "
                f"({_human_bytes(usage.free)} free)</p>"
            )
        body = f"""
        <h2>Services</h2>
        <table>
          <tr><th>kiwix</th><td>{_badge(kiwix_status)}</td></tr>
          <tr><th>qdrant</th><td>{_badge(qdrant_status)}</td></tr>
          <tr><th>embedder</th><td>{_badge(embed_status)}</td></tr>
          <tr><th>reranker</th><td>{_badge(rerank_status)}</td></tr>
        </table>
        <h2>Knowledge base</h2>
        <p>{len(books)} ZIM(s) installed, {_human_bytes(corpus_bytes)} total.</p>
        {disk_line}
        <p class="muted">Retrieval mode: <b>{html.escape(settings.get_retrieval_mode())}</b>
           &middot; reranking: <b>{"on" if settings.get_rerank_enabled() else "off"}</b>
           &middot; see <a href="/settings">Settings</a> to change.</p>
        """
        return _render("Dashboard", body)

    async def sources(request: Request) -> HTMLResponse:
        books = zim_library.scan_zim_dir(zim_dir_path) if zim_dir_path else []
        rows = []
        for b in sorted(books, key=lambda b: b.title.lower()):
            enabled = settings.is_book_enabled(b.name)
            toggle_label = "Disable" if enabled else "Enable"
            fts = "yes" if b.has_fulltext_index else "no"
            error_note = (
                f'<br><span class="error">{html.escape(b.metadata_error)}</span>'
                if b.metadata_error
                else ""
            )
            rows.append(
                f"<tr><td>{html.escape(b.title)}<br>"
                f'<span class="muted">{html.escape(b.name)} &middot; '
                f"{b.article_count:,} articles &middot; {_human_bytes(b.size_bytes)} "
                f"&middot; FTS: {fts}</span>{error_note}"
                f"</td>"
                f"<td>{'enabled' if enabled else 'disabled'}</td>"
                f"<td>"
                f'<form class="inline" method="post" action="/sources/toggle">'
                f'<input type="hidden" name="name" value="{html.escape(b.name)}">'
                f'<input type="hidden" name="enabled" value="{"0" if enabled else "1"}">'
                f"<button type=\"submit\">{toggle_label}</button></form> "
                f'<form class="inline" method="post" action="/sources/delete" '
                f'onsubmit="return confirm(\'Delete {html.escape(b.filename)}?\')">'
                f'<input type="hidden" name="filename" value="{html.escape(b.filename)}">'
                f'<button type="submit">Delete</button></form>'
                f"</td></tr>"
            )
        empty_note = (
            '<p class="muted">No ZIMs installed yet. Browse the '
            '<a href="/catalog">catalog</a>.</p>'
        )
        table = (
            "<table><tr><th>Book</th><th>Status</th><th>Actions</th></tr>"
            + "".join(rows)
            + "</table>"
            if rows
            else empty_note
        )
        body = f"<h2>Installed sources</h2>{table}"
        return _render("Sources", body)

    async def sources_toggle(request: Request) -> RedirectResponse:
        form = await request.form()
        name = str(form.get("name", ""))
        enabled = str(form.get("enabled", "1")) == "1"
        if name:
            settings.set_book_enabled(name, enabled)
        return RedirectResponse("/sources", status_code=303)

    async def sources_delete(request: Request) -> RedirectResponse:
        form = await request.form()
        filename = str(form.get("filename", ""))
        if filename and zim_dir_path:
            try:
                delete_zim(zim_dir_path, filename, on_complete=_refresh_library)
            except ValueError:
                pass
        return RedirectResponse("/sources", status_code=303)

    async def catalog_page(request: Request) -> HTMLResponse:
        query = request.query_params.get("q", "")
        lang = request.query_params.get("lang", "")
        results_html = ""
        if query or lang:
            try:
                entries = await catalog_client.search_catalog(query=query, lang=lang, count=30)
            except httpx.HTTPError as exc:
                results_html = f'<p class="error">Catalog unreachable ({type(exc).__name__}).</p>'
                entries = []
            rows = []
            for e in entries:
                fts_note = _fulltext_badge(e.has_fulltext_index)
                rows.append(
                    f"<tr><td>{html.escape(e.title)}<br>"
                    f'<span class="muted">{html.escape(e.name)} &middot; '
                    f"{html.escape(e.language)} &middot; "
                    f"{e.article_count:,} articles &middot; {_human_bytes(e.size_bytes)}</span>"
                    f"{fts_note}</td><td>{_download_action(e, zim_dir_path)}</td></tr>"
                )
            results_html = (
                "<table><tr><th>Book</th><th>Action</th></tr>" + "".join(rows) + "</table>"
                if rows
                else results_html or "<p class='muted'>No results.</p>"
            )
        q_attr = html.escape(query)
        lang_attr = html.escape(lang)
        body = f"""
        <h2>Browse the Kiwix catalog</h2>
        <form method="get" action="/catalog">
          <input type="text" name="q" placeholder="search, e.g. python, stackoverflow"
                 value="{q_attr}">
          <input type="text" name="lang" placeholder="lang (e.g. eng)" value="{lang_attr}"
                 style="width:100px">
          <button type="submit">Search</button>
        </form>
        {results_html}
        """
        return _render("Catalog", body)

    async def recommendations_page(request: Request) -> HTMLResponse:
        installed = {
            b.name
            for b in (zim_library.scan_zim_dir(zim_dir_path) if zim_dir_path else [])
            if b.metadata_error is None
        }
        resolved = await recommendations.resolve_recommendations()
        rows = []
        for item in resolved:
            rec = item.recommendation
            entry = item.entry
            if entry is None:
                detail = html.escape(item.error or "No downloadable catalog match found.")
                rows.append(
                    f"<tr><td>{html.escape(rec.label)}<br>"
                    f'<span class="muted">{html.escape(rec.rationale)}</span></td>'
                    f'<td><span class="error">{detail}</span></td>'
                    '<td><span class="muted">unavailable</span></td></tr>'
                )
                continue

            action = (
                '<span class="badge ok">installed</span>'
                if entry.name in installed
                else _download_action(entry, zim_dir_path)
            )
            rows.append(
                f"<tr><td>{html.escape(rec.label)}<br>"
                f'<span class="muted">{html.escape(rec.rationale)}</span></td>'
                f"<td>{html.escape(entry.title)}<br>"
                f'<span class="muted">{html.escape(entry.name)} &middot; '
                f"{html.escape(entry.language)} &middot; {entry.article_count:,} articles "
                f"&middot; {_human_bytes(entry.size_bytes)}</span>"
                f"{_fulltext_badge(entry.has_fulltext_index)}</td>"
                f"<td>{action}</td></tr>"
            )

        body = (
            "<h2>Recommended ZIMs</h2>"
            '<p class="muted">Curated starter downloads are resolved through the Kiwix '
            "catalog, then installed through the same download queue as Catalog results.</p>"
            "<table><tr><th>Recommendation</th><th>Catalog match</th><th>Action</th></tr>"
            + "".join(rows)
            + "</table>"
        )
        return _render("Recommended", body)

    async def sources_download(request: Request) -> RedirectResponse:
        form = await request.form()
        url = str(form.get("url", ""))
        filename = str(form.get("filename", ""))
        if url and filename and zim_dir_path is not None:
            try:
                download_manager.start(url, filename)
            except ValueError:
                pass
        return RedirectResponse("/downloads", status_code=303)

    async def downloads_page(request: Request) -> HTMLResponse:
        jobs = download_manager.list_jobs()
        in_flight = any(j.status in ("queued", "downloading") for j in jobs)
        refresh = '<meta http-equiv="refresh" content="4">' if in_flight else ""
        rows = []
        for j in sorted(jobs, key=lambda j: j.job_id, reverse=True):
            pct = ""
            if j.total_bytes:
                pct = f" ({j.downloaded_bytes / j.total_bytes * 100:.0f}%)"
            total_part = f" / {_human_bytes(j.total_bytes)}" if j.total_bytes else ""
            progress = f"{_human_bytes(j.downloaded_bytes)}{total_part}{pct}"
            note = f'<br><span class="error">{html.escape(j.error)}</span>' if j.error else ""
            rows.append(
                f"<tr><td>{html.escape(j.filename)}</td>"
                f"<td>{_badge(j.status)}</td><td>{progress}{note}</td></tr>"
            )
        header = "<tr><th>File</th><th>Status</th><th>Progress</th></tr>"
        table = (
            f"<table>{header}{''.join(rows)}</table>"
            if rows
            else "<p class='muted'>No downloads yet.</p>"
        )
        body = f"<h2>Downloads</h2>{table}"
        return _render("Downloads", body, extra_head=refresh)

    async def settings_page(request: Request) -> HTMLResponse:
        mode = settings.get_retrieval_mode()
        rerank_on = settings.get_rerank_enabled()

        def radio(value: str, label: str) -> str:
            checked = "checked" if mode == value else ""
            return (
                f'<label><input type="radio" name="mode" value="{value}" {checked}> '
                f"{label}</label><br>"
            )

        body = f"""
        <h2>Retrieval settings</h2>
        <form method="post" action="/settings/update">
          <fieldset>
            <legend>Retrieval mode</legend>
            {radio("hybrid", "Hybrid (lexical + vector, reranked)")}
            {radio("lexical", "Lexical only (Kiwix full-text)")}
            {radio("vector", "Vector only (Qdrant, your curated corpora)")}
          </fieldset>
          <p><label><input type="checkbox" name="rerank" value="1" {"checked" if rerank_on else ""}>
             Enable reranking (when available)</label></p>
          <button type="submit">Save</button>
        </form>
        <p class="muted">Per-book enable/disable lives on the
           <a href="/sources">Sources</a> page.</p>
        """
        return _render("Settings", body)

    async def settings_update(request: Request) -> RedirectResponse:
        form = await request.form()
        mode = str(form.get("mode", "hybrid"))
        if mode in ("hybrid", "lexical", "vector"):
            settings.set_retrieval_mode(mode)
        settings.set_rerank_enabled(form.get("rerank") == "1")
        return RedirectResponse("/settings", status_code=303)

    return Starlette(
        routes=[
            Route("/", dashboard),
            Route("/sources", sources),
            Route("/sources/toggle", sources_toggle, methods=["POST"]),
            Route("/sources/delete", sources_delete, methods=["POST"]),
            Route("/sources/download", sources_download, methods=["POST"]),
            Route("/recommendations", recommendations_page),
            Route("/catalog", catalog_page),
            Route("/downloads", downloads_page),
            Route("/settings", settings_page),
            Route("/settings/update", settings_update, methods=["POST"]),
        ]
    )
