#!/usr/bin/env python3
"""OpenStick admin page server (Python standard library only).

Usage:
  Device:  python3 server.py --listen 192.168.100.1:80 --config /etc/openstick-admin/config.json
  Dev:     python3 server.py --mock --listen 127.0.0.1:8080 --config ./dev-config.json
"""
import argparse
import ipaddress
import hashlib
import hmac
import json
import mimetypes
import re
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

WWW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
SESSION_TTL = 30 * 60          # 30 minutes after the last request
PBKDF2_ITER = 200_000
MAX_BODY = 64 * 1024
MAX_UPLOAD = 2 * 1024 * 1024     # larger only for MMS attachments (base64)


class Config:
    """Stores the admin password hash (JSON file, 0600)."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path) as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {}

    @property
    def has_password(self):
        return "pw_hash" in self.data

    def set_password(self, password):
        salt = secrets.token_bytes(16)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITER)
        with self.lock:
            self.data.update(pw_salt=salt.hex(), pw_hash=h.hex(), pw_iter=PBKDF2_ITER)
            self._save()

    def check_password(self, password):
        if not self.has_password:
            return False
        salt = bytes.fromhex(self.data["pw_salt"])
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, self.data["pw_iter"])
        return hmac.compare_digest(h.hex(), self.data["pw_hash"])

    def _save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)


class Sessions:
    def __init__(self):
        self.items = {}   # token -> {"csrf", "seen"}
        self.lock = threading.Lock()

    def create(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.lock:
            self.items[token] = {"csrf": csrf, "seen": time.time()}
        return token, csrf

    def get(self, token):
        with self.lock:
            s = self.items.get(token)
            if not s:
                return None
            if time.time() - s["seen"] > SESSION_TTL:
                del self.items[token]
                return None
            s["seen"] = time.time()
            return s

    def drop(self, token):
        with self.lock:
            self.items.pop(token, None)

    def drop_all(self):
        with self.lock:
            self.items.clear()


class LoginThrottle:
    """Doubles the wait time per IP on each failed login (max 5 minutes)."""

    def __init__(self):
        self.fails = {}
        self.lock = threading.Lock()

    def wait_left(self, ip):
        with self.lock:
            n, until = self.fails.get(ip, (0, 0))
        return max(0, until - time.time())

    def fail(self, ip):
        with self.lock:
            n, _ = self.fails.get(ip, (0, 0))
            n += 1
            delay = 0 if n < 3 else min(300, 2 ** (n - 2))
            self.fails[ip] = (n, time.time() + delay)

    def ok(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenStickAdmin/0.1"
    # the server attribute (app) is injected in main()

    # ---------- Common ----------
    def log_message(self, fmt, *args):
        if self.server.app["verbose"]:
            super().log_message(fmt, *args)

    def _send_json(self, status, obj, extra_headers=()):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra_headers:
            self.send_header(k, v)
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:")

    def _cookie_token(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "osa_session":
                return v
        return None

    def _session(self):
        if self.server.app.get("dev_autologin"):      # --mock only (screenshots/development)
            return "dev", {"csrf": "dev", "seen": time.time()}
        tok = self._cookie_token()
        return (tok, self.server.app["sessions"].get(tok)) if tok else (None, None)

    def _drain(self, n, cap=8 * 1024 * 1024):
        """Read and discard the body even when rejecting it. If we close before reading it all, the browser
        sees a network error instead of 413 and the page cannot show why it was rejected."""
        left = min(n, cap)
        while left > 0:
            chunk = self.rfile.read(min(left, 65536))
            if not chunk:
                break
            left -= len(chunk)
        if n > cap:
            self.close_connection = True

    def _read_json(self, limit=MAX_BODY):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self._drain(n)
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                           f"Request too large (max {limit >> 10}KB)")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid JSON")
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "A JSON object is required")
        return data

    # ---------- Static files ----------
    def _static(self, path):
        if path == "/":
            path = "/index.html"
        full = os.path.realpath(os.path.join(WWW, path.lstrip("/")))
        if not full.startswith(os.path.realpath(WWW) + os.sep) or not os.path.isfile(full):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    # ---------- Routing ----------
    def _from_allowed_network(self):
        """The page may listen on every interface, but it only answers private LAN addresses.
        That keeps it off VPN overlays such as Tailscale (100.64.0.0/10) and off the internet."""
        nets = self.server.app.get("allow_nets")
        if not nets:
            return True
        try:
            ip = ipaddress.ip_address(self.client_address[0])
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
        except ValueError:
            return False
        return any(ip in n for n in nets)

    def _refuse(self):
        self.send_response(HTTPStatus.FORBIDDEN)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "10")
        self.end_headers()
        self.wfile.write(b"Forbidden\n")

    def do_GET(self):
        if not self._from_allowed_network():
            return self._refuse()
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self._api("GET", path)
        else:
            self._static(path)

    def do_POST(self):
        if not self._from_allowed_network():
            return self._refuse()
        self._api("POST", urlparse(self.path).path)

    def _api(self, method, path):
        app = self.server.app
        try:
            ip = self.client_address[0]
            # --- Allowed without authentication ---
            if (method, path) == ("GET", "/api/session"):
                tok, s = self._session()
                return self._send_json(200, {
                    "logged_in": bool(s), "csrf": s["csrf"] if s else None,
                    "needs_setup": not app["config"].has_password, "mock": app["mock"]})
            if (method, path) == ("POST", "/api/setup"):
                if app["config"].has_password:
                    raise ApiError(HTTPStatus.FORBIDDEN, "Already set up")
                pw = self._read_json().get("password", "")
                _check_new_password(pw)
                app["config"].set_password(pw)
                return self._login_response()
            if (method, path) == ("POST", "/api/login"):
                left = app["throttle"].wait_left(ip)
                if left:
                    raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, f"Try again in {int(left) + 1} seconds")
                pw = self._read_json().get("password", "")
                if not app["config"].check_password(pw):
                    app["throttle"].fail(ip)
                    raise ApiError(HTTPStatus.UNAUTHORIZED, "Incorrect password")
                app["throttle"].ok(ip)
                return self._login_response()

            # --- Login required below ---
            tok, s = self._session()
            if not s:
                raise ApiError(HTTPStatus.UNAUTHORIZED, "Login required")
            if method == "POST" and not hmac.compare_digest(
                    self.headers.get("X-CSRF-Token", ""), s["csrf"]):
                raise ApiError(HTTPStatus.FORBIDDEN, "Invalid CSRF token")

            if (method, path) == ("POST", "/api/logout"):
                app["sessions"].drop(tok)
                return self._send_json(200, {"ok": True}, [("Set-Cookie", _cookie("", 0))])
            if (method, path) == ("POST", "/api/password"):
                d = self._read_json()
                if not app["config"].check_password(d.get("old", "")):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Current password is incorrect")
                _check_new_password(d.get("new", ""))
                app["config"].set_password(d["new"])
                app["sessions"].drop_all()
                return self._login_response()

            be = app["backend"]
            q = {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}
            if (method, path) == ("GET", "/api/messages/media"):
                ctype, data, name = be.messages_media(q.get("id", "0"), q.get("part", "0"))
                return self._send_media(ctype, data, name)
            routes = {
                ("GET", "/api/status"): be.status,
                ("GET", "/api/clients"): be.clients,
                ("GET", "/api/assistant"): be.assistant_get,
                ("GET", "/api/wifi"): be.wifi_get,
                ("POST", "/api/wifi"): lambda: be.wifi_set(self._read_json()),
                ("GET", "/api/wifi/client"): be.wifi_client_get,
                ("POST", "/api/wifi/client/scan"): be.wifi_client_scan,
                ("POST", "/api/wifi/client/join"): lambda: be.wifi_client_join(self._read_json()),
                ("POST", "/api/wifi/client/leave"): be.wifi_client_leave,
                ("GET", "/api/wan"): be.wan_get,
                ("POST", "/api/wan/data"): lambda: be.wan_set_data(self._read_json()),
                ("POST", "/api/wan/apn"): lambda: be.wan_set_apn(self._read_json()),
                ("GET", "/api/device"): be.device,
                ("POST", "/api/reboot"): be.reboot,
                ("GET", "/api/messages"): lambda: be.messages_list(q),
                ("POST", "/api/messages/send"): lambda: be.messages_send(self._read_json()),
                ("POST", "/api/messages/send-mms"): lambda: be.messages_send_mms(self._read_json(MAX_UPLOAD)),
                ("POST", "/api/messages/delete"): lambda: be.messages_delete(self._read_json()),
                ("POST", "/api/messages/read"): lambda: be.messages_read(self._read_json()),
                ("POST", "/api/messages/retry"): lambda: be.messages_retry(self._read_json()),
                ("GET", "/api/telegram"): be.telegram_get,
                ("POST", "/api/telegram"): lambda: be.telegram_set(self._read_json()),
                ("POST", "/api/telegram/test"): lambda: be.telegram_test(self._read_json()),
                ("GET", "/api/thermal"): be.thermal_get,
                ("POST", "/api/thermal"): lambda: be.thermal_set(self._read_json()),
                ("GET", "/api/usage"): be.usage_get,
                ("POST", "/api/usage"): lambda: be.usage_set(self._read_json()),
                ("POST", "/api/usage/reset"): be.usage_reset,
                ("POST", "/api/clients/block"): lambda: be.client_block(self._read_json(), self.client_address[0]),
                ("POST", "/api/clients/unblock"): lambda: be.client_unblock(self._read_json()),
                ("GET", "/api/system"): be.system_get,
                ("POST", "/api/system/leds"): lambda: be.leds_set(self._read_json()),
                ("POST", "/api/system/timezone"): lambda: be.timezone_set(self._read_json()),
                ("POST", "/api/system/restart"): lambda: be.service_restart(self._read_json()),
                ("GET", "/api/logs"): lambda: be.logs(q),
                ("GET", "/api/dns"): be.dns_get,
                ("POST", "/api/dns"): lambda: be.dns_set(self._read_json()),
                ("POST", "/api/diag"): lambda: be.diag(self._read_json()),
            }
            fn = routes.get((method, path))
            if not fn:
                raise ApiError(HTTPStatus.NOT_FOUND, "Unknown API")
            self._send_json(200, fn())
        except ApiError as e:
            self._send_json(e.status, {"error": str(e)})
        except ValueError as e:          # backend input validation failed
            self._send_json(400, {"error": str(e)})
        except Exception as e:           # noqa: BLE001
            self.log_error("API error %s: %r", path, e)
            self._send_json(500, {"error": f"Internal error: {e}"})

    SAFE_INLINE = ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp")

    def _send_media(self, ctype, data, name):
        """MMS attachment. Only plain images are shown inline; everything else downloads."""
        inline = ctype in self.SAFE_INLINE
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype if inline else "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        fname = re.sub(r"[^A-Za-z0-9._-]", "_", name or "attachment")
        self.send_header("Content-Disposition", f'{"inline" if inline else "attachment"}; filename="{fname}"')
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _login_response(self):
        token, csrf = self.server.app["sessions"].create()
        self._send_json(200, {"ok": True, "csrf": csrf}, [("Set-Cookie", _cookie(token, SESSION_TTL))])


def _cookie(value, max_age):
    return f"osa_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"


def _check_new_password(pw):
    if not isinstance(pw, str) or not (8 <= len(pw) <= 64):
        raise ApiError(HTTPStatus.BAD_REQUEST, "Password must be 8-64 characters")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="192.168.100.1:80",
                    help="host:port, or several separated by commas (0.0.0.0:80 = every interface)")
    ap.add_argument("--allow-nets", default="192.168.0.0/16,10.0.0.0/8,172.16.0.0/12,127.0.0.0/8",
                    help="client networks that get an answer; everything else is refused")
    ap.add_argument("--config", default="/etc/openstick-admin/config.json")
    ap.add_argument("--mock", action="store_true", help="run with mock data, without a device")
    ap.add_argument("--dev-autologin", action="store_true", help="--mock only: skip login (for development/screenshots)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if a.dev_autologin and not a.mock:
        ap.error("--dev-autologin can only be used together with --mock")
    if a.mock:
        import mock as backend_mod
    else:
        import backend as backend_mod
    app = {
        "config": Config(a.config), "sessions": Sessions(), "throttle": LoginThrottle(),
        "backend": backend_mod.Backend(), "mock": a.mock, "verbose": a.verbose,
        "dev_autologin": a.dev_autologin,
        "allow_nets": [ipaddress.ip_network(n.strip()) for n in a.allow_nets.split(",") if n.strip()],
    }
    servers = []
    for addr in [x.strip() for x in a.listen.split(",") if x.strip()]:
        host, _, port = addr.rpartition(":")
        httpd = ThreadingHTTPServer((host, int(port)), Handler)
        httpd.daemon_threads = True
        httpd.app = app
        servers.append(httpd)
    print(f"OpenStick admin page: http://{a.listen}/ ({'mock' if a.mock else 'real'} mode)", flush=True)
    for httpd in servers[1:]:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
