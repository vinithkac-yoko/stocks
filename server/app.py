#!/usr/bin/env python3
"""Daily 10 server: the static site, a small JSON API, and the daily NSE refresh.

One process, standard library only. Configuration comes from environment variables:
  APP_PASSCODE     required, at least 8 characters
  DATA_DIR         where marks.db, history.json and latest.json live; defaults to the Railway
                   volume mount path if one is attached, else ./data
  SESSION_SECRET   optional; signs login cookies (default: derived from the passcode)
  PORT             listening port (Railway sets it)
  DISABLE_REFRESH  set to anything to skip the NSE downloads (local testing)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import update_data  # noqa: E402
from db import ApiError, Store  # noqa: E402

COOKIE = "d10"
SESSION_SECONDS = 30 * 24 * 3600
MAX_BODY = 20 * 1024 * 1024  # a day's bhavcopy is a few MB
REFRESH_CHECK_SECONDS = 1800
REFRESH_AFTER = (18, 30)  # IST; NSE publishes the day's file in the evening

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/json", ".json")

CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def today_ist() -> str:
    return datetime.now(update_data.IST).date().isoformat()


@dataclass
class Config:
    passcode: str
    data_dir: Path
    docs_dir: Path = ROOT / "docs"
    secret: str = ""
    refresh: bool = True
    ephemeral: bool = False  # on Railway with no volume: everything is lost on redeploy

    @classmethod
    def from_env(cls) -> "Config":
        passcode = os.environ.get("APP_PASSCODE", "")
        if len(passcode) < 8:
            raise SystemExit("Set APP_PASSCODE to a passcode of at least 8 characters.")
        explicit = os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
        on_railway = any(k.startswith("RAILWAY_") for k in os.environ)
        return cls(
            passcode=passcode,
            data_dir=Path(explicit) if explicit else ROOT / "data",
            secret=os.environ.get("SESSION_SECRET", ""),
            refresh=not os.environ.get("DISABLE_REFRESH"),
            ephemeral=on_railway and not explicit,
        )


# ------------------------------------------------------------------ auth ----

class Throttle:
    """Single-user app, so a modest limit per client plus a global one is enough."""
    WINDOW, PER_IP, GLOBAL = 900, 5, 30

    def __init__(self):
        self.lock, self.by_ip, self.all = threading.Lock(), {}, []

    def _recent(self, stamps):
        cutoff = time.time() - self.WINDOW
        return [t for t in stamps if t > cutoff]

    def check(self, ip: str) -> None:
        with self.lock:
            self.all = self._recent(self.all)
            self.by_ip[ip] = self._recent(self.by_ip.get(ip, []))
            if len(self.by_ip[ip]) >= self.PER_IP or len(self.all) >= self.GLOBAL:
                raise ApiError(429, "Too many wrong passcodes. Try again in 15 minutes.")

    def fail(self, ip: str) -> None:
        with self.lock:
            now = time.time()
            self.by_ip.setdefault(ip, []).append(now)
            self.all.append(now)

    def ok(self, ip: str) -> None:
        with self.lock:
            self.by_ip.pop(ip, None)


class Refresher:
    """Downloads NSE data on a schedule. Failures are recorded and shown in the page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last_run: str | None = None
        self.last_ok: bool | None = None
        self.error: str | None = None

    def run_once(self, **kwargs) -> bool:
        with self.lock:
            self.last_run = datetime.now(update_data.IST).isoformat(timespec="seconds")
            try:
                ok = update_data.run(**kwargs) == 0
                err = None if ok else "No data produced; see the service logs."
            except SystemExit as e:
                ok, err = False, str(e.code)
            except Exception as e:  # network errors, bad files
                ok, err = False, f"{type(e).__name__}: {e}"
            self.last_ok, self.error = ok, err
            if err:
                log(f"refresh failed: {err}")
            return ok

    def status(self) -> dict:
        return {"lastRun": self.last_run, "lastOk": self.last_ok, "error": self.error}

    @staticmethod
    def due() -> bool:
        try:
            as_of = json.loads(update_data.output_path().read_text(encoding="utf-8"))["asOf"]
        except (OSError, ValueError, KeyError):
            return True
        now = datetime.now(update_data.IST)
        return now.weekday() < 5 and (now.hour, now.minute) >= REFRESH_AFTER and as_of < now.date().isoformat()

    def loop(self) -> None:
        self.run_once()  # catch up after a deploy or restart
        while True:
            time.sleep(REFRESH_CHECK_SECONDS)
            if self.due():
                self.run_once()


# --------------------------------------------------------------- handler ----

class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, cfg: Config):
        os.environ["DATA_DIR"] = str(cfg.data_dir)  # update_data reads this
        self.cfg, self.store = cfg, Store(cfg.data_dir / "marks.db")
        self.throttle, self.refresher = Throttle(), Refresher()
        self.secret = (cfg.secret or hashlib.sha256(("session:" + cfg.passcode).encode()).hexdigest()).encode()
        super().__init__(addr, Handler)

    def sign(self, exp: int) -> str:
        return f"{exp}.{hmac.new(self.secret, f'v1:{exp}'.encode(), hashlib.sha256).hexdigest()}"

    def session_valid(self, token: str) -> bool:
        exp, _, sig = token.partition(".")
        if not exp.isdigit() or int(exp) < time.time():
            return False
        return hmac.compare_digest(self.sign(int(exp)), f"{exp}.{sig}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Daily10"
    server: Server

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    do_GET = do_HEAD = lambda self: self._handle()
    do_POST = do_PUT = do_DELETE = lambda self: self._handle()

    # ---- plumbing

    def _ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For")
        return fwd.split(",")[-1].strip() if fwd else self.client_address[0]

    def _https(self) -> bool:
        return self.headers.get("X-Forwarded-Proto") == "https"

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None, head: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        if self._https():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(self, status: int, obj, headers: dict | None = None) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json", {"Cache-Control": "no-store", **(headers or {})})

    def _handle(self) -> None:
        method = self.command if self.command != "HEAD" else "GET"
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.close_connection = True
            return self._json(413, {"error": "file too large"}, {"Connection": "close"})
        body = self.rfile.read(length) if length else b""
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/healthz":
                return self._send(200, b"ok", "text/plain", head=self.command == "HEAD")
            if parsed.path.startswith("/api/"):
                return self._api(method, parsed.path, parse_qs(parsed.query), body)
            if method == "GET":
                return self._static(parsed.path, head=self.command == "HEAD")
            self._json(405, {"error": "method not allowed"})
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except Exception:
            log(traceback.format_exc())
            self._json(500, {"error": "server error"})

    # ---- static files

    def _static(self, path: str, head: bool) -> None:
        cfg = self.server.cfg
        if path == "/data/latest.json":  # real data if present, demo file otherwise
            real = update_data.output_path()
            target = real if real.exists() else cfg.docs_dir / "data" / "latest.json"
        else:
            rel = unquote(path).lstrip("/") or "index.html"
            if "\x00" in rel:
                return self._json(404, {"error": "not found"})
            target = (cfg.docs_dir / rel).resolve()
            if cfg.docs_dir.resolve() not in target.parents:
                return self._json(404, {"error": "not found"})
        if not target.is_file():
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/json":
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"}, head)

    # ---- auth

    def _check_passcode(self, candidate: str) -> bool:
        ip = self._ip()
        self.server.throttle.check(ip)
        if hmac.compare_digest(candidate.encode(), self.server.cfg.passcode.encode()):
            self.server.throttle.ok(ip)
            return True
        self.server.throttle.fail(ip)
        return False

    def _authed(self, method: str) -> bool:
        bearer = self.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            return self._check_passcode(bearer[7:])
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and self.server.session_valid(value):
                if method != "GET" and self.headers.get("X-App") != "1":
                    raise ApiError(403, "missing X-App header")
                return True
        return False

    def _json_body(self, body: bytes) -> dict:
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            raise ApiError(400, "body must be JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    # ---- API

    def _api(self, method: str, path: str, query: dict, body: bytes) -> None:
        s = self.server
        if method == "GET" and path == "/api/session":
            return self._json(200, {"authed": self._authed("GET")})
        if method == "POST" and path == "/api/login":
            passcode = self._json_body(body).get("passcode")
            if not isinstance(passcode, str) or not self._check_passcode(passcode):
                raise ApiError(401, "Wrong passcode")
            exp = int(time.time()) + SESSION_SECONDS
            cookie = f"{COOKIE}={s.sign(exp)}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}"
            return self._json(200, {"ok": True}, {"Set-Cookie": cookie + ("; Secure" if self._https() else "")})
        if method == "POST" and path == "/api/logout":
            return self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})

        if not self._authed(method):
            raise ApiError(401, "Not signed in")

        if method == "GET" and path == "/api/state":
            return self._json(200, {**s.store.state(today_ist()), "refresh": s.refresher.status(), "ephemeral": s.cfg.ephemeral})
        if method == "POST" and path == "/api/cycles":
            return self._json(200, s.store.start_cycle(today_ist()))
        if method == "POST" and path == "/api/picks":
            d = self._json_body(body)
            return self._json(200, {"id": s.store.add_pick(d.get("cycleId"), d.get("symbol"), d.get("pickedOn"))})
        if method == "POST" and path == "/api/admin/upload":
            return self._upload(query, body)

        m = re.fullmatch(r"/api/picks/(\d+)", path)
        if m and method == "DELETE":
            s.store.remove_pick(int(m[1]))
            return self._json(200, {"ok": True})
        m = re.fullmatch(r"/api/picks/(\d+)/invest", path)
        if m and method == "PUT":
            d = self._json_body(body)
            s.store.set_invested(int(m[1]), d.get("entryPrice"), d.get("qty"), d.get("investedOn"))
            return self._json(200, {"ok": True})
        if m and method == "DELETE":
            s.store.clear_invested(int(m[1]))
            return self._json(200, {"ok": True})
        m = re.fullmatch(r"/api/picks/(\d+)/sells", path)
        if m and method == "POST":
            d = self._json_body(body)
            return self._json(200, {"id": s.store.add_sell(int(m[1]), d.get("qty"), d.get("price"), d.get("soldOn"))})
        m = re.fullmatch(r"/api/sells/(\d+)", path)
        if m and method == "DELETE":
            s.store.remove_sell(int(m[1]))
            return self._json(200, {"ok": True})
        raise ApiError(404, "not found")

    def _upload(self, query: dict, body: bytes) -> None:
        """Fallback when NSE refuses the server's downloads: POST the file yourself."""
        kind = (query.get("kind") or [""])[0]
        text = body.decode("utf-8", "replace")
        if kind == "universe":
            if not update_data.read_csv(text) or "SYMBOL" not in update_data.read_csv(text)[0]:
                raise ApiError(400, "not a NIFTY 500 list (needs a Symbol column)")
            update_data.write_atomic(update_data.universe_cache(), text)
            return self._json(200, {"ok": True, "next": "now upload a bhavcopy with kind=bhav"})
        elif kind == "bhav":
            try:
                session = datetime.strptime((query.get("date") or [""])[0], "%Y-%m-%d").date().isoformat()
            except ValueError:
                raise ApiError(400, "date=YYYY-MM-DD is required") from None
            ok = self.server.refresher.run_once(bhav_text=text, bhav_date=session)
        else:
            raise ApiError(400, "kind must be 'universe' or 'bhav'")
        self._json(200 if ok else 422, {"ok": ok, "refresh": self.server.refresher.status()})


def make_server(cfg: Config, host: str = "0.0.0.0", port: int = 0) -> Server:
    return Server((host, port), cfg)


def main() -> None:
    cfg = Config.from_env()
    server = make_server(cfg, port=int(os.environ.get("PORT", "8000")))
    if cfg.ephemeral:
        log("WARNING: no volume attached. Marks and data will be lost on every redeploy.")
    if cfg.refresh:
        threading.Thread(target=server.refresher.loop, daemon=True, name="refresh").start()
    log(f"Listening on :{server.server_address[1]}, data in {cfg.data_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
