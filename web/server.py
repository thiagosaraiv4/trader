"""
web/server.py
-------------
HTTP server of the web panel (aiohttp, already installed with ccxt).

    /                    panel (static/index.html)
    /login, /logout      password login (only when a password is set)
    /api/v1/...          JSON API — the same endpoints a future app or
                         multi-user product would use; ?bot=eth picks a
                         coin (default btc), /api/v1/bots lists them
    /api/v1/health       liveness, never needs login

Read-only by design: nothing here changes the bots, their files or the
services. Data comes from web/data.py.

Requests must name an allowed Host (localhost by default): a page on
another site that rebinds its DNS name to 127.0.0.1 cannot read the panel
through an SSH tunnel.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import math
import os
import secrets
import signal
import time
from collections import deque
from pathlib import Path
from typing import Any

from aiohttp import web

from web import data

STATIC = Path(__file__).resolve().parent / "static"
SESSION_COOKIE = "trader_session"
SESSION_TTL_SEC = 7 * 24 * 3600
_MAX_FAILURES = 10            # per client address, then a lockout
_LOCKOUT_SEC = 15 * 60
_GLOBAL_MAX_FAILURES = 100    # all addresses together, per lockout window
_MAX_TRACKED = 10_000         # client addresses remembered at most
_BODY_TIMEOUT_SEC = 10
_SERVICES_TTL_SEC = 10
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


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


async def _services(app: web.Application) -> dict[str, str]:
    """systemctl is-active for each bot service ('unknown' off systemd),
    cached a few seconds so open tabs don't each spawn a process."""
    cache = app["services_cache"]
    names = data.services()          # changes when a coin bot is installed
    async with app["services_lock"]:
        if (cache["states"] and tuple(cache["states"]) == names
                and time.monotonic() - cache["at"] < _SERVICES_TTL_SEC):
            return cache["states"]
        states: list[str] = []
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", *names,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,                  # own group: killable as a whole
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            states = out.decode().split()
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            if proc is not None and proc.returncode is None:   # hung systemctl
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
        if len(states) != len(names):
            states = ["unknown"] * len(names)
        result = dict(zip(names, states))
        cache.update(at=time.monotonic(), states=result)
        return result


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def client_key(addr: str | None) -> str:
    """Lockout key of a client: its IPv4 address, or its IPv6 /64 (one
    machine usually controls a whole /64)."""
    try:
        ip = ipaddress.ip_address(addr or "")
    except ValueError:
        return addr or "?"
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class Auth:
    """
    Password login with in-memory sessions (a restart logs everyone out).

    Wrong passwords lock the client out after _MAX_FAILURES, and everyone
    after _GLOBAL_MAX_FAILURES, for _LOCKOUT_SEC. locked() and check() are
    plain (non-async) calls: run back to back, no other request can slip in
    between them.
    """

    def __init__(self, password: str | None) -> None:
        self.password = password
        self.sessions: dict[str, float] = {}           # token -> expiry
        self.failures: dict[str, tuple[int, float]] = {}  # client -> (count, last)
        self.recent: deque[float] = deque()            # times of all failures

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

    def locked(self, client: str) -> bool:
        now = time.time()
        while self.recent and now - self.recent[0] >= _LOCKOUT_SEC:
            self.recent.popleft()
        if len(self.recent) >= _GLOBAL_MAX_FAILURES:
            return True
        count, last = self.failures.get(client, (0, 0.0))
        if now - last >= _LOCKOUT_SEC:
            self.failures.pop(client, None)
            return False
        return count >= _MAX_FAILURES

    def check(self, client: str, password: str) -> bool:
        """Compare the password and count a failure; never compares while locked."""
        if self.locked(client):
            return False
        ok = hmac.compare_digest(password.encode(), (self.password or "").encode())
        if ok:
            self.failures.pop(client, None)
            return True
        now = time.time()
        count, _ = self.failures.get(client, (0, 0.0))
        self.failures[client] = (count + 1, now)
        self.recent.append(now)
        if len(self.failures) > _MAX_TRACKED:
            self._prune(now)
        return False

    def _prune(self, now: float) -> None:
        self.failures = {k: v for k, v in self.failures.items() if now - v[1] < _LOCKOUT_SEC}
        while len(self.failures) > _MAX_TRACKED:              # drop the oldest
            del self.failures[min(self.failures, key=lambda k: self.failures[k][1])]

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


def host_name(host_header: str) -> str:
    """'Example.com:8080' → 'example.com', '[::1]:8080' → '::1'."""
    h = host_header.strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else ""
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


@web.middleware
async def check_host(request: web.Request, handler):
    """Only allowed Host names (blocks DNS rebinding); no cross-site API or
    form calls."""
    if host_name(request.headers.get("Host", "")) not in request.app["allowed_hosts"]:
        return web.Response(status=421, text="Host não permitido neste painel.")
    if request.headers.get("Sec-Fetch-Site") == "cross-site" and (
            request.method != "GET" or request.path.startswith("/api/")):
        return web.Response(status=403, text="Requisição de outro site recusada.")
    return await handler(request)


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
    try:
        form = await asyncio.wait_for(request.post(), timeout=_BODY_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        raise web.HTTPRequestTimeout() from None
    # Lockout checked AFTER the body arrives and right before comparing (no
    # await in between): requests held open in parallel cannot all get
    # past a check made while the failure count was still low
    client = client_key(request.remote)
    if auth.locked(client):
        raise web.HTTPFound("/login?erro=bloqueado")
    if not auth.check(client, str(form.get("password", ""))):
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
    services, beats, system, bots = await asyncio.gather(
        _services(request.app), asyncio.to_thread(data.heartbeats),
        asyncio.to_thread(data.system_info), asyncio.to_thread(data.bots))
    return _json({"time": time.time(), "services": services, "heartbeats": beats,
                  "system": system, "login": request.app["auth"].enabled,
                  "bots": [{k: b[k] for k in ("id", "log", "service")} for b in bots]})


def _bot(request: web.Request) -> dict:
    """The bot named by ?bot= (default btc); 404 for an unknown one."""
    try:
        return data.get_bot(request.query.get("bot", "btc"))
    except KeyError:
        raise web.HTTPNotFound(text=json.dumps({"error": "robô desconhecido"}),
                               content_type="application/json") from None


async def api_bots(request: web.Request) -> web.Response:
    return _json(await asyncio.to_thread(data.bots_summary))


async def api_bot(request: web.Request) -> web.Response:
    return _json(await asyncio.to_thread(data.bot_overview, _bot(request)))


async def api_candles(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "500"))
    except ValueError:
        return _json({"error": "limit inválido"}, status=400)
    return _json(await asyncio.to_thread(data.candles, limit, _bot(request)))


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


def create_app(password: str | None = None,
               allowed_hosts: tuple[str, ...] | list[str] = ()) -> web.Application:
    """allowed_hosts: names accepted in the Host header besides localhost
    (needed when the panel is opened by IP or domain instead of a tunnel)."""
    app = web.Application(middlewares=[security_headers, check_host, require_login],
                          client_max_size=16 * 1024)
    app["auth"] = Auth(password)
    app["allowed_hosts"] = LOOPBACK_HOSTS | {host_name(h) for h in allowed_hosts}
    app["services_cache"] = {"at": 0.0, "states": None}
    app["services_lock"] = asyncio.Lock()
    app.router.add_get("/", index)
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_post)
    app.router.add_get("/logout", logout)
    app.router.add_get("/api/v1/health", api_health)
    app.router.add_get("/api/v1/status", api_status)
    app.router.add_get("/api/v1/bots", api_bots)
    app.router.add_get("/api/v1/bot", api_bot)
    app.router.add_get("/api/v1/candles", api_candles)
    app.router.add_get("/api/v1/scanner", api_scanner)
    app.router.add_get("/api/v1/maker", api_maker)
    app.router.add_get("/api/v1/logs/{name}", api_logs)
    app.router.add_static("/static/", STATIC, show_index=False, follow_symlinks=False)
    return app
