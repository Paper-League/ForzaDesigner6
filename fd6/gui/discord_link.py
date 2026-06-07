"""Discord OAuth (PKCE) link + server-membership check + Rich Presence for FD6.

Why it exists: auto-updates are gated behind being a member of the FD6 Discord
server. Linking proves identity + membership without a bot or client secret
(PKCE flow, public client id only). Linking is OPTIONAL — the app works fully
without it; the only thing you lose by not linking is the automatic
update-check panel on launch (manual Help -> Check for updates still works).

Persisted in QSettings (org "FD6", app "Forza Designer 6"):
  - discord/access_token   the OAuth token (lets us re-verify membership silently)
  - discord/user_id, discord/username
  - discord/rich_presence  "1"/"0" — show "Using Forza Designer 6" on Discord

Everything is best-effort and offline-safe: no network / denied / parse error
just means "not linked / can't verify", never a crash.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import socketserver
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QObject, QSettings, QThread, Signal

# ── Discord app config (public id only — PKCE, no secret) ────────────────────
DISCORD_CLIENT_ID = "1513003378354028554"
DISCORD_REDIRECT_PORT = 53691  # distinct from NoMansMovies' 53682
DISCORD_REDIRECT_URI = f"http://localhost:{DISCORD_REDIRECT_PORT}/discord-callback"
DISCORD_SCOPES = "identify guilds"
# The FD6 community server. Auto-updates require membership here.
FD6_GUILD_ID = "1457468089317982301"
DISCORD_INVITE_URL = "https://discord.gg/PJFWdykGmS"

# Rich Presence asset key (upload an image named this under the Discord app's
# Rich Presence -> Art Assets to show an icon; text works without it).
RP_LARGE_IMAGE = "fd6_logo"

_ORG = "FD6"
_APP = "Forza Designer 6"


class DiscordError(Exception):
    pass


@dataclass
class DiscordLink:
    access_token: str
    user_id: str
    username: str


# ── settings helpers ─────────────────────────────────────────────────────────
def _settings() -> QSettings:
    return QSettings(_ORG, _APP)


def is_linked() -> bool:
    return bool(_settings().value("discord/access_token"))


def linked_username() -> str:
    return str(_settings().value("discord/username") or "")


def save_link(link: DiscordLink) -> None:
    s = _settings()
    s.setValue("discord/access_token", link.access_token)
    s.setValue("discord/user_id", link.user_id)
    s.setValue("discord/username", link.username)
    s.sync()


def clear_link() -> None:
    s = _settings()
    for k in ("discord/access_token", "discord/user_id", "discord/username"):
        s.remove(k)
    s.sync()


def rich_presence_enabled() -> bool:
    v = _settings().value("discord/rich_presence", "0")
    return str(v) in ("1", "true", "True", "yes")


def set_rich_presence_enabled(enabled: bool) -> None:
    s = _settings()
    s.setValue("discord/rich_presence", "1" if enabled else "0")
    s.sync()


# ── PKCE + local callback ─────────────────────────────────────────────────────
def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


_HTML_OK = b"""<!doctype html><meta charset=utf-8><title>Forza Designer 6</title>
<body style="font-family:Segoe UI,sans-serif;background:#14121a;color:#f0f0f5;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="background:#1d1830;border:1px solid #5dd62c;border-radius:14px;padding:32px;text-align:center">
<h1 style="margin:0 0 8px">Discord linked</h1><p>You can close this tab and return to Forza Designer 6.</p></div>"""

_HTML_FAIL = b"""<!doctype html><meta charset=utf-8><title>Forza Designer 6</title>
<body style="font-family:Segoe UI,sans-serif;background:#14121a;color:#f0f0f5;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="background:#1d1830;border:1px solid #e50914;border-radius:14px;padding:32px;text-align:center">
<h1 style="margin:0 0 8px">Link failed</h1><p>Return to Forza Designer 6 and try again.</p></div>"""


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    captured_code: Optional[str] = None
    captured_error: Optional[str] = None
    expected_state: str = ""

    def do_GET(self):  # noqa: N802
        if not self.path.startswith("/discord-callback"):
            self.send_response(404); self.end_headers(); return
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        if params.get("error"):
            _CallbackHandler.captured_error = params.get("error_description") or params["error"]
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(_HTML_FAIL); return
        code, state = params.get("code"), params.get("state")
        if not code or state != _CallbackHandler.expected_state:
            _CallbackHandler.captured_error = "State mismatch or missing code."
            self.send_response(400); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(_HTML_FAIL); return
        _CallbackHandler.captured_code = code
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(_HTML_OK)

    def log_message(self, *_a, **_k):
        return


def _await_callback(state: str, timeout_s: int = 180) -> str:
    _CallbackHandler.captured_code = None
    _CallbackHandler.captured_error = None
    _CallbackHandler.expected_state = state
    server = socketserver.TCPServer(("127.0.0.1", DISCORD_REDIRECT_PORT), _CallbackHandler)
    server.timeout = 1
    done = threading.Event()

    def serve():
        while not done.is_set():
            server.handle_request()
            if _CallbackHandler.captured_code or _CallbackHandler.captured_error:
                done.set()

    threading.Thread(target=serve, daemon=True).start()
    done.wait(timeout=timeout_s)
    try:
        server.server_close()
    except Exception:
        pass
    if _CallbackHandler.captured_error:
        raise DiscordError(_CallbackHandler.captured_error)
    if not _CallbackHandler.captured_code:
        raise DiscordError("Timed out waiting for Discord authorization.")
    return _CallbackHandler.captured_code


# ── API calls ─────────────────────────────────────────────────────────────────
def _link_blocking() -> DiscordLink:
    """Full PKCE link flow. Blocking — run off the UI thread."""
    import requests
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    auth_url = "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": DISCORD_REDIRECT_URI,
        "scope": DISCORD_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
    })
    if not webbrowser.open(auth_url):
        raise DiscordError("Could not open the system browser.")
    code = _await_callback(state)
    tr = requests.post(
        "https://discord.com/api/oauth2/token",
        data={
            "client_id": DISCORD_CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT_URI,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if not tr.ok:
        raise DiscordError(f"Token exchange failed: {tr.status_code}")
    token = tr.json().get("access_token")
    if not token:
        raise DiscordError("No access token returned.")
    me = requests.get("https://discord.com/api/v10/users/@me",
                      headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if not me.ok:
        raise DiscordError(f"/users/@me failed: {me.status_code}")
    mej = me.json()
    return DiscordLink(access_token=token, user_id=str(mej.get("id", "")),
                       username=mej.get("global_name") or mej.get("username") or "")


def is_member_of_fd6_guild(access_token: str | None = None) -> bool:
    """True if the linked user is a member of the FD6 server. Offline / error
    -> False (caller treats that as 'can't verify -> no auto-update panel')."""
    import requests
    token = access_token or str(_settings().value("discord/access_token") or "")
    if not token:
        return False
    try:
        r = requests.get("https://discord.com/api/v10/users/@me/guilds",
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if not r.ok:
            return False
        return any(str(g.get("id")) == FD6_GUILD_ID for g in (r.json() or []))
    except Exception:
        return False


# ── threaded link worker (for the GUI) ────────────────────────────────────────
class LinkWorker(QObject):
    succeeded = Signal(object)  # DiscordLink
    failed = Signal(str)
    # member: True/False membership result after a successful link
    membership = Signal(bool)

    def run(self) -> None:
        try:
            link = _link_blocking()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        save_link(link)
        try:
            member = is_member_of_fd6_guild(link.access_token)
        except Exception:
            member = False
        self.succeeded.emit(link)
        self.membership.emit(member)


# ── Rich Presence ─────────────────────────────────────────────────────────────
class RichPresence:
    """Discord Rich Presence wrapper that does ALL pypresence work on its own
    daemon thread with its own asyncio loop.

    CRITICAL: pypresence's connect()/update() block on Discord's IPC pipe and
    create an asyncio event loop. Calling them on the Qt GUI thread froze the
    app at startup for users who'd linked + enabled presence (the dreaded
    "not responding" after the splash on the SECOND launch). Everything here is
    off-thread and best-effort — if Discord isn't running or pypresence is
    missing, it silently does nothing and never blocks the UI.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="FD6-RichPresence", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # Own asyncio loop for this thread (pypresence requires one and Qt's
        # main thread has none we can safely share).
        try:
            import asyncio
            asyncio.set_event_loop(asyncio.new_event_loop())
        except Exception:
            pass
        rpc = None
        try:
            from pypresence import Presence
            import time
            rpc = Presence(DISCORD_CLIENT_ID)
            rpc.connect()
            rpc.update(
                state="Designing liveries",
                details="Using Forza Designer 6",
                large_image=RP_LARGE_IMAGE,
                large_text="Forza Designer 6",
                start=int(time.time()),
            )
        except Exception:
            # Discord not running, IPC blocked, or pypresence missing — give up
            # quietly; the GUI thread is never touched.
            try:
                if rpc is not None:
                    rpc.close()
            except Exception:
                pass
            return
        # Keep the presence alive until stop() is requested.
        try:
            while not self._stop_evt.wait(15):
                try:
                    rpc.update(
                        state="Designing liveries",
                        details="Using Forza Designer 6",
                        large_image=RP_LARGE_IMAGE,
                        large_text="Forza Designer 6",
                    )
                except Exception:
                    break
        finally:
            try:
                rpc.clear()
            except Exception:
                pass
            try:
                rpc.close()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2)
        self._thread = None
