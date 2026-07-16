"""Authentication and browser-security primitives for hosted services."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

SESSION_COOKIE = "las_admin_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
DOWNLOAD_ACTION_TTL_SECONDS = 15 * 60


@dataclass
class AdminSession:
    session_id: str
    csrf_token: str
    expires_at: int
    download_nonces: set[str] = field(default_factory=set)


class AdminSecurity:
    def __init__(
        self,
        token: str,
        *,
        allowed_hosts: list[str],
        allowed_origins: list[str],
        cookie_secure: bool = False,
        now_fn=time.time,
    ) -> None:
        self.token = token
        self.allowed_hosts = {value.lower() for value in allowed_hosts}
        self.allowed_origins = {value.rstrip("/") for value in allowed_origins}
        self.cookie_secure = cookie_secure
        self.now_fn = now_fn
        self.sessions: dict[str, AdminSession] = {}
        self._download_key = hmac.new(
            token.encode(), b"local-ai-suite/download-actions/v1", hashlib.sha256
        ).digest()

    def authenticate(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self.token.encode())

    def create_session(self) -> AdminSession:
        now = int(self.now_fn())
        session = AdminSession(
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + SESSION_TTL_SECONDS,
        )
        self.sessions[session.session_id] = session
        return session

    def session_for(self, request: Request) -> AdminSession | None:
        session_id = request.cookies.get(SESSION_COOKIE, "")
        session = self.sessions.get(session_id)
        if not session:
            return None
        if session.expires_at <= int(self.now_fn()):
            self.sessions.pop(session_id, None)
            return None
        return session

    def invalidate(self, request: Request) -> None:
        self.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)

    def host_allowed(self, request: Request) -> bool:
        raw = request.headers.get("host", "").lower()
        if raw.startswith("["):
            host = raw.split("]", 1)[0] + "]"
        else:
            host = raw.rsplit(":", 1)[0] if ":" in raw else raw
        return host in self.allowed_hosts

    def origin_allowed(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is None or origin.rstrip("/") in self.allowed_origins

    def issue_download_action(
        self, session: AdminSession, *, url: str, filename: str, expected_bytes: int
    ) -> str:
        nonce = secrets.token_urlsafe(18)
        session.download_nonces.add(nonce)
        payload = {
            "url": url,
            "filename": filename,
            "expected_bytes": expected_bytes,
            "expires": int(self.now_fn()) + DOWNLOAD_ACTION_TTL_SECONDS,
            "nonce": nonce,
            "session": session.session_id,
        }
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = _b64(hmac.new(self._download_key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def consume_download_action(self, session: AdminSession, token: str) -> dict[str, Any]:
        try:
            body, supplied = token.split(".", 1)
            expected = _b64(hmac.new(self._download_key, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("invalid download action")
            payload = json.loads(_unb64(body))
            if not isinstance(payload, dict):
                raise ValueError("invalid download action")
        except (binascii.Error, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid download action") from exc
        nonce = str(payload.get("nonce", ""))
        if payload.get("session") != session.session_id:
            raise ValueError("download action belongs to another session")
        try:
            expires = int(payload.get("expires", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid download action") from exc
        if expires < int(self.now_fn()):
            raise ValueError("download action expired")
        if nonce not in session.download_nonces:
            raise ValueError("download action was already used")
        session.download_nonces.remove(nonce)
        return payload


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded).decode()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response


class AdminAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, security: AdminSecurity) -> None:  # noqa: ANN001
        super().__init__(app)
        self.security = security

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self.security.host_allowed(request):
            return PlainTextResponse("invalid Host header", status_code=400)
        if request.url.path in {"/login", "/healthz"}:
            return await call_next(request)
        session = self.security.session_for(request)
        if session is None:
            if request.method == "GET":
                return RedirectResponse("/login", status_code=303)
            return PlainTextResponse("authentication required", status_code=401)
        request.state.admin_session = session
        return await call_next(request)


class MCPBearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, api_key: str) -> None:  # noqa: ANN001
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith("/mcp"):
            header = request.headers.get("authorization", "")
            scheme, _, value = header.partition(" ")
            valid = scheme.lower() == "bearer" and hmac.compare_digest(
                value.encode(), self.api_key.encode()
            )
            if not valid:
                return PlainTextResponse(
                    "authentication required",
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)
