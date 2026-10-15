"""
web/server.py
-------------
HTTP server of the web panel (aiohttp, already installed with ccxt).

    /                    panel (static/index.html)
    /login, /logout      password login (only when a password is set)
    /api/v1/...          JSON API — the same endpoints a future app or
                         multi-user product would use
    /api/v1/health       liveness, never needs login

Read-only by design: nothing here changes the bots, their files or the
services. Data comes from web/data.py.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import math
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from web import data

STATIC = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "trader_session"
SESSION_TTL_SEC = 7 * 24 * 3600
_MAX_FAILURES = 10            # per client address, then a lockout
_LOCKOUT_SEC = 15 * 60


def _clean(obj: Any) -> Any:
    """NaN/inf → None, recursively (JSON has no NaN)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _json(payload: Any, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(_clean(payload), ensure_ascii=False, allow_nan=False),
        status=status, content_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


async def _services() -> dict[str, str]:
    """systemctl is-active for each bot service ('unknown' off systemd)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", *data.SERVICES,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        states = out.decode().split()
    except (OSError, asyncio.TimeoutError):
        states = []
    if len(states) != len(data.SERVICES):
        states = ["unknown"] * len(data.SERVICES)
    return dict(zip(data.SERVICES, states))


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

class Auth:
    """Password login with in-memory sessions (a restart logs everyone out)."""

    def __init__(self, password: str | None) -> None:
        self.password = password
        self.sessions: dict[str, float] = {}           # token -> expiry
        self.failures: dict[str, tuple[int, float]] = {}  # addr -> (count, last)

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def valid(self, request: web.Request) -> bool:
        if not self.enabled:
            return True
        token = request.cookies.get(SESSION_COOKIE, "")
        exp = self.sessions.get(token)
        if exp is None or exp < time.time():
            self.sessions.pop(token, None)
            return False
        return True

    def locked(self, addr: str) -> bool:
        count, last = self.failures.get(addr, (0, 0.0))
        if count >= _MAX_FAILURES and time.time() - last < _LOCKOUT_SEC:
            return True
        if time.time() - last >= _LOCKOUT_SEC:
            self.failures.pop(addr, None)
        return False

    def check(self, addr: str, password: str) -> bool:
        ok = hmac.compare_digest(password.encode(), (self.password or "").encode())
        if ok:
            self.failures.pop(addr, None)
        else:
            count, _ = self.failures.get(addr, (0, 0.0))
            self.failures[addr] = (count + 1, time.time())
        return ok

    def new_session(self) -> str:
        now = time.time()
        self.sessions = {t: e for t, e in self.sessions.items() if e > now}
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + SESSION_TTL_SEC
        return token


_PUBLIC = {"/login", "/api/v1/health", "/static/style.css"}


@web.middleware
async def security_headers(request: web.Request, handler):
    resp = await handler(request)
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


@web.middleware
async def require_login(request: web.Request, handler):
    auth: Auth = request.app["auth"]
    if request.path in _PUBLIC or auth.valid(request):
        return await handler(request)
    if request.path.startswith("/api/"):
        return _json({"error": "login necessário"}, status=401)
    raise web.HTTPFound("/login")


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


_LOGIN_ERRORS = {
    "1": "Senha incorreta.",
    "bloqueado": "Muitas tentativas erradas. Tente de novo em 15 minutos.",
}


async def login_page(request: web.Request) -> web.StreamResponse:
    if not request.app["auth"].enabled:
        raise web.HTTPFound("/")
    html = (STATIC / "login.html").read_text(encoding="utf-8")
    # Fixed messages only: nothing from the URL is echoed into the page
    msg = _LOGIN_ERRORS.get(request.query.get("erro", ""), "")
    return web.Response(text=html.replace("<!--ERROR-->", msg), content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def login_post(request: web.Request) -> web.Response:
    auth: Auth = request.app["auth"]
    if not auth.enabled:
        raise web.HTTPFound("/")
    addr = request.remote or "?"
    if auth.locked(addr):
        raise web.HTTPFound("/login?erro=bloqueado")
    form = await request.post()
    if not auth.check(addr, str(form.get("password", ""))):
        await asyncio.sleep(1.0)                     # slows down guessing
        raise web.HTTPFound("/login?erro=1")
    resp = web.HTTPFound("/")
    resp.set_cookie(SESSION_COOKIE, auth.new_session(), max_age=SESSION_TTL_SEC,
                    httponly=True, samesite="Strict", secure=request.secure)
    raise resp


async def logout(request: web.Request) -> web.Response:
    auth: Auth = request.app["auth"]
    auth.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    resp = web.HTTPFound("/login" if auth.enabled else "/")
    resp.del_cookie(SESSION_COOKIE)
    raise resp


async def api_health(request: web.Request) -> web.Response:
    return _json({"ok": True, "time": time.time()})


async def api_status(request: web.Request) -> web.Response:
    services, beats, system = await asyncio.gather(
        _services(), asyncio.to_thread(data.heartbeats), asyncio.to_thread(data.system_info))
    return _json({"time": time.time(), "services": services, "heartbeats": beats,
                  "system": system, "login": request.app["auth"].enabled})


async def api_bot(request: web.Request) -> web.Response:
    return _json(await asyncio.to_thread(data.bot_overview))


async def api_candles(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "500"))
    except ValueError:
        return _json({"error": "limit inválido"}, status=400)
    return _json(await asyncio.to_thread(data.candles, limit))


async def api_scanner(request: web.Request) -> web.Response:
    return _json(await asyncio.to_thread(data.scanner_report))


async def api_maker(request: web.Request) -> web.Response:
    return _json(await asyncio.to_thread(data.maker_report))


async def api_logs(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    try:
        lines = int(request.query.get("lines", "100"))
        out = await asyncio.to_thread(data.log_tail, name, lines)
    except ValueError:
        return _json({"error": "lines inválido"}, status=400)
    except KeyError:
        return _json({"error": "log desconhecido"}, status=404)
    return _json({"name": name, "lines": out})


def create_app(password: str | None = None) -> web.Application:
    app = web.Application(middlewares=[security_headers, require_login],
                          client_max_size=16 * 1024)
    app["auth"] = Auth(password)
    app.router.add_get("/", index)
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_post)
    app.router.add_get("/logout", logout)
    app.router.add_get("/api/v1/health", api_health)
    app.router.add_get("/api/v1/status", api_status)
    app.router.add_get("/api/v1/bot", api_bot)
    app.router.add_get("/api/v1/candles", api_candles)
    app.router.add_get("/api/v1/scanner", api_scanner)
    app.router.add_get("/api/v1/maker", api_maker)
    app.router.add_get("/api/v1/logs/{name}", api_logs)
    app.router.add_static("/static/", STATIC, show_index=False, follow_symlinks=False)
    return app
