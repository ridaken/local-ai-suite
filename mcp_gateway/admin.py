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

import hmac
import html
import shutil
from pathlib import Path
from urllib.parse import parse_qsl, quote

import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from . import catalog as catalog_client
from . import config, recommendations, zim_library
from .catalog import CatalogEntry
from .downloads import DownloadManager, delete_zim
from .security import (
    SESSION_COOKIE,
    AdminAuthMiddleware,
    AdminSecurity,
    AdminSession,
    SecurityHeadersMiddleware,
)
from .settings_store import SettingsStore

# Two-tier navigation. The primary tabs collapse the seven pages into three
# groups; each entry lists the routes that belong to it (so the right primary
# tab highlights no matter which sub-page you're on). Groups with a sub-nav list
# their pages in _SUB_NAV.
_PRIMARY_NAV = [
    ("/", "Dashboard", ("/",)),
    ("/sources", "Knowledge Base", ("/sources", "/recommendations", "/catalog", "/downloads")),
    ("/settings", "Settings", ("/settings", "/configuration")),
]

_SUB_NAV = {
    "/sources": [
        ("/sources", "Installed"),
        ("/recommendations", "Recommended"),
        ("/catalog", "Catalog"),
        ("/downloads", "Downloads"),
    ],
    "/settings": [
        ("/settings", "Retrieval"),
        ("/configuration", "Configuration"),
    ],
}


def _nav_html(current_path: str) -> str:
    active_group = next(
        (group for group, _label, members in _PRIMARY_NAV if current_path in members),
        "/",
    )
    primary = "".join(
        f'<a class="{"active" if group == active_group else ""}" href="{group}">'
        f"{html.escape(label)}</a>"
        for group, label, _members in _PRIMARY_NAV
    )
    sub = ""
    if active_group in _SUB_NAV:
        sub_links = "".join(
            f'<a class="{"active" if path == current_path else ""}" href="{path}">'
            f"{html.escape(label)}</a>"
            for path, label in _SUB_NAV[active_group]
        )
        sub = f'<nav class="subnav">{sub_links}</nav>'
    return f'<nav class="primary">{primary}</nav>{sub}'

_CSS = """
body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:#0e0e10;color:#eaeaea}
@media (prefers-color-scheme: light){body{background:#fafafa;color:#111}}
header{padding:1rem 1.5rem;border-bottom:1px solid #3a3a3a}
header h1{margin:0 0 .6rem;font-size:1.1rem}
nav.primary a{display:inline-block;margin-right:1.4rem;padding:.15rem 0;color:#7db9e8;
  text-decoration:none;font-size:1rem;border-bottom:2px solid transparent}
nav.primary a:hover{color:#a9d4f5}
nav.primary a.active{color:#eaeaea;font-weight:600;border-bottom-color:#7db9e8}
@media (prefers-color-scheme: light){nav.primary a.active{color:#111}}
:root[data-theme="light"] nav.primary a.active{color:#111}
:root[data-theme="dark"] nav.primary a.active{color:#eaeaea}
nav.subnav{margin-top:.55rem}
nav.subnav a{display:inline-block;margin-right:.5rem;padding:.2rem .7rem;border-radius:1rem;
  color:#7db9e8;text-decoration:none;font-size:.85rem}
nav.subnav a:hover{background:rgba(125,185,232,.15)}
nav.subnav a.active{background:#7db9e8;color:#0e0e10;font-weight:600}
nav.section-tabs{display:flex;flex-wrap:wrap;gap:.15rem;margin:.75rem 0 1rem;
  border-bottom:1px solid #333}
nav.section-tabs a{padding:.35rem .8rem;color:#7db9e8;text-decoration:none;font-size:.88rem;
  border-bottom:2px solid transparent;margin-bottom:-1px}
nav.section-tabs a:hover{color:#a9d4f5}
nav.section-tabs a.active{color:inherit;font-weight:600;border-bottom-color:#7db9e8}
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
input[type=password],input[type=number]{padding:.3rem .5rem;width:280px}
.config-input{width:100%;max-width:520px}
.field-help{display:block;margin-top:.2rem}
.muted{opacity:.7;font-size:.85rem}
.error{color:#e08f8f}
.setup-banner{background:#4d431f;color:#f0e4b0;border-radius:.4rem;padding:.75rem 1rem;
  margin-bottom:1.25rem}
.setup-banner strong{display:block;margin-bottom:.35rem}
.setup-banner ul{margin:.25rem 0 0;padding-left:1.2rem}
.setup-banner code{background:rgba(255,255,255,.12);padding:0 .25rem;border-radius:.2rem}
"""


def _page(title: str, body: str, *, current_path: str = "/", extra_head: str = "") -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)} — local-ai-suite</title>"
        f"<style>{_CSS}</style>{extra_head}</head><body>"
        f"<header><h1>local-ai-suite admin</h1>{_nav_html(current_path)}</header>"
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


def _download_action(
    entry: CatalogEntry,
    zim_dir_path: Path | None,
    *,
    security: AdminSecurity,
    session: AdminSession,
    csrf_token: str,
) -> str:
    if not entry.download_url:
        return '<span class="muted">unavailable</span>'
    if zim_dir_path is None:
        return '<span class="muted">set ZIM_DIR first</span>'
    if entry.size_bytes is None:
        return '<span class="muted">unavailable: catalog size unknown</span>'
    filename = entry.download_url.rsplit("/", 1)[-1]
    action = security.issue_download_action(
        session,
        url=entry.download_url,
        filename=filename,
        expected_bytes=entry.size_bytes,
    )
    return (
        '<form method="post" action="/sources/download">'
        f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">'
        f'<input type="hidden" name="action" value="{html.escape(action)}">'
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
    admin_token: str | None = None,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    cookie_secure: bool | None = None,
) -> Starlette:
    initial_zim_dir = zim_dir
    initial_library_xml_path = library_xml_path
    admin_token = admin_token if admin_token is not None else config.ADMIN_TOKEN
    if not admin_token:
        raise ValueError("ADMIN_TOKEN is required for the admin service")
    security = AdminSecurity(
        admin_token,
        allowed_hosts=allowed_hosts or config.ADMIN_ALLOWED_HOSTS,
        allowed_origins=allowed_origins or config.ADMIN_ALLOWED_ORIGINS,
        cookie_secure=config.ADMIN_COOKIE_SECURE if cookie_secure is None else cookie_secure,
    )

    async def _urlencoded_form(request: Request) -> dict[str, str]:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {"", "application/x-www-form-urlencoded"}:
            return {}
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 64 * 1024:
                raise HTTPException(413, "form is too large")
            chunks.append(chunk)
        body = b"".join(chunks)
        if not body and not content_type:
            return {}
        if content_type != "application/x-www-form-urlencoded":
            return {}
        return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))

    def _session(request: Request) -> AdminSession:
        return request.state.admin_session

    def _csrf_input(request: Request) -> str:
        return (
            '<input type="hidden" name="csrf_token" value="'
            + html.escape(_session(request).csrf_token)
            + '">'
        )

    async def _mutation_form(request: Request):  # noqa: ANN202
        if not security.origin_allowed(request):
            return None, PlainTextResponse("invalid Origin header", status_code=403)
        form = await _urlencoded_form(request)
        supplied = str(form.get("csrf_token", ""))
        if not hmac.compare_digest(supplied, _session(request).csrf_token):
            return None, PlainTextResponse("invalid CSRF token", status_code=403)
        return form, None

    def _zim_dir_path() -> Path | None:
        current = str(config.ZIM_DIR or initial_zim_dir).strip()
        return Path(current) if current else None

    def _library_xml_path() -> Path | None:
        current = str(config.LIBRARY_XML_PATH or initial_library_xml_path).strip()
        return Path(current) if current else None

    def _render(
        request: Request, title: str, body: str, *, extra_head: str = ""
    ) -> HTMLResponse:
        banner = _setup_banner(_setup_issues(_zim_dir_path()))
        return _page(
            title, banner + body, current_path=request.url.path, extra_head=extra_head
        )

    def _refresh_library() -> None:
        zim_path = _zim_dir_path()
        library_path = _library_xml_path()
        if zim_path and library_path:
            zim_library.refresh_library(zim_path, library_path)

    async def login(request: Request) -> Response:
        if request.method == "GET":
            body = """
            <h2>Administrator login</h2>
            <form method="post" action="/login">
              <label>Admin token <input type="password" name="token"
                autocomplete="current-password" required></label>
              <button type="submit">Log in</button>
            </form>
            """
            return _page("Login", body)
        if not security.origin_allowed(request):
            return PlainTextResponse("invalid Origin header", status_code=403)
        form = await _urlencoded_form(request)
        if not security.authenticate(str(form.get("token", ""))):
            return _page("Login", '<p class="error">Invalid admin token.</p>', current_path="/")
        security.invalidate(request)
        session = security.create_session()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session.session_id,
            max_age=8 * 60 * 60,
            httponly=True,
            secure=security.cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    async def logout(request: Request) -> Response:
        _form, error = await _mutation_form(request)
        if error:
            return error
        security.invalidate(request)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    async def dashboard(request: Request) -> HTMLResponse:
        kiwix_status, qdrant_status, embed_status, rerank_status = [
            await _reachable(url)
            for url in (config.KIWIX_URL, config.QDRANT_URL, config.EMBED_URL, config.RERANK_URL)
        ]
        zim_path = _zim_dir_path()
        books = zim_library.scan_zim_dir(zim_path) if zim_path else []
        corpus_bytes = sum(b.size_bytes for b in books)
        disk_line = ""
        if zim_path and zim_path.is_dir():
            usage = shutil.disk_usage(zim_path)
            disk_line = (
                f"<p>Data drive: {_human_bytes(usage.used)} used / "
                f"{_human_bytes(usage.total)} total "
                f"({_human_bytes(usage.free)} free)</p>"
            )
        body = f"""
        <form method="post" action="/logout" style="float:right">
          {_csrf_input(request)}<button type="submit">Log out</button>
        </form>
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
        return _render(request, "Dashboard", body)

    async def sources(request: Request) -> HTMLResponse:
        zim_path = _zim_dir_path()
        books = zim_library.scan_zim_dir(zim_path) if zim_path else []
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
                f'{_csrf_input(request)}'
                f'<input type="hidden" name="name" value="{html.escape(b.name)}">'
                f'<input type="hidden" name="enabled" value="{"0" if enabled else "1"}">'
                f"<button type=\"submit\">{toggle_label}</button></form> "
                f'<a href="/sources/delete/confirm?filename={quote(b.filename)}">Delete</a>'
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
        return _render(request, "Installed sources", body)

    async def sources_toggle(request: Request) -> Response:
        form, error = await _mutation_form(request)
        if error:
            return error
        assert form is not None
        name = str(form.get("name", ""))
        enabled = str(form.get("enabled", "1")) == "1"
        if name:
            settings.set_book_enabled(name, enabled)
        return RedirectResponse("/sources", status_code=303)

    async def sources_delete_confirm(request: Request) -> HTMLResponse:
        filename = str(request.query_params.get("filename", ""))
        body = (
            "<h2>Confirm deletion</h2>"
            f"<p>Delete <code>{html.escape(filename)}</code>?</p>"
            '<form method="post" action="/sources/delete">'
            f"{_csrf_input(request)}"
            f'<input type="hidden" name="filename" value="{html.escape(filename)}">'
            '<button type="submit">Delete permanently</button> '
            '<a href="/sources">Cancel</a></form>'
        )
        return _render(request, "Confirm deletion", body)

    async def sources_delete(request: Request) -> Response:
        form, error = await _mutation_form(request)
        if error:
            return error
        assert form is not None
        filename = str(form.get("filename", ""))
        zim_path = _zim_dir_path()
        if filename and zim_path:
            try:
                delete_zim(zim_path, filename, on_complete=_refresh_library)
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
                action = _download_action(
                    e,
                    _zim_dir_path(),
                    security=security,
                    session=_session(request),
                    csrf_token=_session(request).csrf_token,
                )
                rows.append(
                    f"<tr><td>{html.escape(e.title)}<br>"
                    f'<span class="muted">{html.escape(e.name)} &middot; '
                    f"{html.escape(e.language)} &middot; "
                    f"{e.article_count:,} articles &middot; {_human_bytes(e.size_bytes)}</span>"
                    f"{fts_note}</td><td>{action}</td></tr>"
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
        return _render(request, "Catalog", body)

    async def recommendations_page(request: Request) -> HTMLResponse:
        zim_path = _zim_dir_path()
        installed = {
            b.name
            for b in (zim_library.scan_zim_dir(zim_path) if zim_path else [])
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
                else _download_action(
                    entry,
                    _zim_dir_path(),
                    security=security,
                    session=_session(request),
                    csrf_token=_session(request).csrf_token,
                )
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
        return _render(request, "Recommended", body)

    async def sources_download(request: Request) -> Response:
        form, error = await _mutation_form(request)
        if error:
            return error
        assert form is not None
        zim_path = _zim_dir_path()
        if zim_path is not None:
            try:
                payload = security.consume_download_action(
                    _session(request), str(form.get("action", ""))
                )
                download_manager.zim_dir = zim_path
                download_manager.start(
                    str(payload["url"]),
                    str(payload["filename"]),
                    expected_bytes=int(payload["expected_bytes"]),
                )
            except ValueError:
                return RedirectResponse(
                    "/downloads?error=Download%20request%20was%20rejected",
                    status_code=303,
                )
        return RedirectResponse("/downloads", status_code=303)

    async def downloads_page(request: Request) -> HTMLResponse:
        jobs = download_manager.list_jobs()
        error_notice = (
            '<p class="error">Download request was rejected.</p>'
            if request.query_params.get("error")
            else ""
        )
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
        body = f"<h2>Downloads</h2>{error_notice}{table}"
        return _render(request, "Downloads", body, extra_head=refresh)

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
          {_csrf_input(request)}
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
        return _render(request, "Retrieval settings", body)

    async def settings_update(request: Request) -> Response:
        form, error = await _mutation_form(request)
        if error:
            return error
        assert form is not None
        mode = str(form.get("mode", "hybrid"))
        if mode in ("hybrid", "lexical", "vector"):
            settings.set_retrieval_mode(mode)
        settings.set_rerank_enabled(form.get("rerank") == "1")
        return RedirectResponse("/settings", status_code=303)

    def _config_section(request: Request) -> str:
        section = request.query_params.get("section", "")
        if section not in config.CONFIG_GROUP_KEYS:
            return config.CONFIG_GROUP_KEYS[0]
        return section

    async def configuration_page(request: Request) -> HTMLResponse:
        section = _config_section(request)

        def row(field: dict[str, object]) -> str:
            name = str(field["name"])
            current = "configured" if field.get("secret") and getattr(config, name, "") else (
                "not configured" if field.get("secret") else str(getattr(config, name, ""))
            )
            help_text = str(field.get("help", ""))
            help_html = (
                f'<span class="muted field-help">{html.escape(help_text)}</span>'
                if help_text
                else ""
            )
            return (
                f"<tr><th>{html.escape(str(field.get('label', name)))}"
                f"<br><span class='muted'>{html.escape(name)}</span></th>"
                f"<td><code>{html.escape(str(current))}</code>"
                f"{help_html}</td></tr>"
            )

        section_tabs = "".join(
            f'<a class="{"active" if key == section else ""}" '
            f'href="/configuration?section={key}">{html.escape(label)}</a>'
            for key, label in config.CONFIG_GROUPS
        )
        rows = "".join(
            row(field) for field in config.CONFIG_FIELDS if field.get("group") == section
        )
        body = f"""
        <h2>Effective configuration</h2>
        <p class="muted">Infrastructure, endpoints, paths, and secrets are read-only here.
        Change environment variables or mounted secret files, then restart the affected service.
        Secret values are never displayed or persisted by the admin UI.</p>
        <nav class="section-tabs">{section_tabs}</nav>
        <table><tr><th>Setting</th><th>Effective value</th></tr>{rows}</table>
        """
        return _render(request, "Configuration", body)

    async def healthz(_request: Request) -> Response:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/login", login, methods=["GET", "POST"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/healthz", healthz),
            Route("/", dashboard),
            Route("/sources", sources),
            Route("/sources/toggle", sources_toggle, methods=["POST"]),
            Route("/sources/delete/confirm", sources_delete_confirm),
            Route("/sources/delete", sources_delete, methods=["POST"]),
            Route("/sources/download", sources_download, methods=["POST"]),
            Route("/recommendations", recommendations_page),
            Route("/catalog", catalog_page),
            Route("/downloads", downloads_page),
            Route("/settings", settings_page),
            Route("/settings/update", settings_update, methods=["POST"]),
            Route("/configuration", configuration_page),
        ]
    )
    app.add_middleware(AdminAuthMiddleware, security=security)
    app.add_middleware(SecurityHeadersMiddleware)
    app.state.admin_security = security
    return app
