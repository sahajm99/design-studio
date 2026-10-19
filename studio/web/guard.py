"""The request guards (v6): a button can now spend money, so the studio answers only to this
computer's own names, and only its own pages may change anything.

- **Only local names.** Every request's Host must name `localhost`, `127.0.0.1` or `[::1]`,
  on any port, or it gets 400. This stops DNS rebinding, where a website points its own
  name at 127.0.0.1 to reach the studio from the browser: the name is what gives it away,
  so the port is free and the studio works wherever compose publishes it. `studio`, the
  compose service's name, is accepted too, so the sample script reaches the studio from its
  own container. A browser cannot be rebound to a one-word name its machine does not resolve.
- **Only the studio's own pages.** Every request that can change something (POST and the
  other unsafe methods) must carry an Origin, or a Referer when the Origin is absent, whose
  host and port are the request's own Host, or it gets 403. This stops another website open
  in the same browser from posting to the studio (cross-site request forgery), for example to
  start paid rounds. Browsers send an Origin with every cross-site POST, an opaque one as
  "null", which is refused; a request with neither header comes from no browser page (the
  sample script, a test client), so it is let through.

The port binding in `docker-compose.yml` (127.0.0.1 only) keeps other machines out.

v7 hosting: on a server, `STUDIO_PUBLIC_HOST` is one more name the studio answers to, behind
an HTTPS proxy on the same machine, and the passcode gate stands behind the Host check. With
`STUDIO_PASSCODE` set, every request needs the sign-in cookie, except the sign-in page and the
stylesheet, scripts, font and logo it draws with: a page the browser navigates to is sent to
`/signin`; a script's call, and anything under `/api/`, gets 401. The cookie is a Fernet
token, under a key derived from the studio's secret for this use alone, that holds a
fingerprint of the passcode and lasts 30 days, so changing the passcode signs everyone out; on
the public host it is a `__Host-` cookie, kept to that exact host over HTTPS. Wrong passcodes
are counted by address, the proxy's report of it trusted only from the proxy: ten in ten
minutes, and that address waits. It is a gate, not accounts: everyone past it shares one
studio. Without a passcode the studio refuses any request that came through a proxy, so a
proxy that rewrote the Host could not put it online open; and it speaks no websockets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import time
from collections import deque
from urllib.parse import quote, urlsplit

from cryptography.fernet import Fernet, InvalidToken
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

# The names the studio answers to: this computer's, and the compose service's.
LOCAL_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]", "studio"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

NOT_FROM_STUDIO = "This request did not come from the studio."
NOT_LOCAL = "The studio answers only on this computer, at localhost."
# v7 hosting: a request through a proxy to a studio with no passcode.
NOT_WITHOUT_PASSCODE = (
    "This studio has no passcode, so it answers only on this computer, not through a proxy. "
    "Set STUDIO_PUBLIC_HOST and STUDIO_PASSCODE to host it."
)

# v7: the passcode gate.
PASS_COOKIE = "studio_pass"
# On the public host: the browser keeps a __Host- cookie to that exact host, over HTTPS, so no
# sibling site under the same domain (sslip.io is not a public suffix) can set one in its place.
HOST_PASS_COOKIE = "__Host-studio_pass"
PASS_DAYS = 30
PASS_SECONDS = PASS_DAYS * 24 * 60 * 60
SIGN_IN_PATH = "/signin"
SIGN_OUT_PATH = "/signout"
# What the sign-in page draws with, reachable before signing in; nothing else is. Widening
# either of these opens that part of the studio to anyone on the public address.
OPEN_PATHS = frozenset({SIGN_IN_PATH, SIGN_OUT_PATH, "/brand/font", "/brand/logo"})
OPEN_PREFIXES = ("/static/",)
SIGN_IN_FIRST = "Sign in first."
# The derived key's purpose, so the cookie's key is never the saved keys' key itself.
_PASS_KEY_PURPOSE = b"design-studio passcode cookie v7"
# Wrong passcodes an address may send in the window before it must wait, and how many
# addresses are kept before the old ones are swept.
MAX_WRONG = 10
WRONG_WINDOW_SECONDS = 10 * 60
_MAX_ADDRESSES = 1024


class LocalOnlyGuard:
    """ASGI middleware: the Host check on every request, the Origin check on every change.
    v7: `public_host`, when given, is one more name the Host may carry; without a passcode
    (`gated` false) a request that came through a proxy is refused; a websocket handshake is
    refused, since the studio has none."""

    def __init__(self, app: ASGIApp, public_host: str = "", *, gated: bool = False) -> None:
        self.app = app
        self.names = LOCAL_NAMES | ({public_host.lower()} if public_host else set())
        self.gated = gated

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # Refused before any route sees it: a websocket would pass neither check below.
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "").strip().lower()
        if host_name(host) not in self.names:
            response = PlainTextResponse(NOT_LOCAL, status_code=400)
        elif not self.gated and ("x-forwarded-for" in headers or "forwarded" in headers):
            # Nothing proxies the studio on the designer's own computer; a proxy in front of a
            # studio with no passcode would put it online open.
            response = PlainTextResponse(NOT_WITHOUT_PASSCODE, status_code=403)
        elif scope["method"] not in SAFE_METHODS and not _from_studio(headers, host):
            response = PlainTextResponse(NOT_FROM_STUDIO, status_code=403)
        else:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)


class PasscodeGate:
    """ASGI middleware (v7): with a passcode, every request but the open ones needs a valid
    sign-in cookie. The cookie's key is `app.state.pass_key`, set at start-up; without it no
    cookie is valid, so the gate stays shut. It must stand behind LocalOnlyGuard."""

    def __init__(self, app: ASGIApp, passcode: str = "", public_host: str = "") -> None:
        self.app = app
        self.passcode = passcode
        self.cookie = pass_cookie_name(public_host)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.passcode or is_open(scope["path"]):
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        key = getattr(getattr(scope.get("app"), "state", None), "pass_key", None)
        if key is not None and pass_is_valid(
            connection.cookies.get(self.cookie, ""), key, self.passcode
        ):
            await self.app(scope, receive, send)
            return
        await sign_in_first(connection)(scope, receive, send)


def pass_cookie_name(public_host: str) -> str:
    """The sign-in cookie's name: a __Host- cookie on the public host, which is served only over
    HTTPS; the plain name on this computer, where the studio may be plain HTTP."""
    return HOST_PASS_COOKIE if public_host else PASS_COOKIE


def is_open(path: str) -> bool:
    """Whether a path is reachable before signing in: the sign-in page and what it draws with."""
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def sign_in_first(connection: HTTPConnection) -> Response:
    """The answer to a request without a valid cookie: a page the browser navigates to goes to
    the sign-in page, which comes back to it afterwards; a script's call (any Sec-Fetch-Mode
    but "navigate") and anything under `/api/` gets 401, which the script can read, rather
    than the sign-in page it would take for an answer."""
    path = connection.url.path
    navigating = connection.headers.get("sec-fetch-mode", "navigate") == "navigate"
    if path.startswith("/api/") or not navigating:
        return JSONResponse({"error": SIGN_IN_FIRST}, status_code=401)
    if connection.scope["method"] in SAFE_METHODS:
        query = connection.url.query
        wanted = f"{path}?{query}" if query else path
        return RedirectResponse(f"{SIGN_IN_PATH}?next={quote(wanted, safe='')}", status_code=303)
    return RedirectResponse(SIGN_IN_PATH, status_code=303)


def pass_key(secret: bytes) -> bytes:
    """The Fernet key for sign-in cookies, derived from the studio's secret for this use only."""
    digest = hmac.new(secret, _PASS_KEY_PURPOSE, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest)


def issue_pass(key: bytes, passcode: str) -> str:
    """A new sign-in cookie: the passcode's fingerprint, encrypted and signed, dated now."""
    return Fernet(key).encrypt(_fingerprint(passcode)).decode("ascii")


def pass_is_valid(token: str, key: bytes, passcode: str) -> bool:
    """Whether a cookie was issued by this studio, under this passcode, within 30 days."""
    if not token:
        return False
    try:
        held = Fernet(key).decrypt(token.encode("ascii"), ttl=PASS_SECONDS)
    except (InvalidToken, ValueError):
        return False
    return hmac.compare_digest(held, _fingerprint(passcode))


def passcode_matches(typed: str, passcode: str) -> bool:
    """Whether the typed passcode is the passcode, compared in constant time."""
    return hmac.compare_digest(typed.encode("utf-8"), passcode.encode("utf-8"))


def client_address(connection: HTTPConnection) -> str:
    """Who is signing in, for counting wrong passcodes: when the connection comes from the
    proxy in front (this machine, or Docker's gateway to it), the address the proxy reports last
    in X-Forwarded-For, the one it saw; else the connection's own, whatever it claims."""
    peer = connection.client.host if connection.client is not None else ""
    if _may_be_the_proxy(peer):
        forwarded = connection.headers.get("x-forwarded-for", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    return peer


def _may_be_the_proxy(address: str) -> bool:
    """Whether a connection's address is this machine's or a private network's, as the proxy's
    is when it reaches the container's port through Docker."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def served_over_https(connection: HTTPConnection, public_host: str) -> bool:
    """Whether the cookie may be marked Secure: always on a public host, which is served only
    over HTTPS, else when the request came over HTTPS, itself or through the proxy."""
    proto = connection.headers.get("x-forwarded-proto", connection.url.scheme)
    return bool(public_host) or proto.strip().lower() == "https"


class WrongPasscodes:
    """The wrong passcodes each address sent in the last ten minutes, in memory. An address is
    forgotten once its window is empty, and past 1,024 addresses the stale ones are swept."""

    def __init__(self) -> None:
        self._times: dict[str, deque[float]] = {}

    def must_wait(self, address: str) -> bool:
        """Whether the address sent ten wrong passcodes within the window."""
        return self._recent(address) >= MAX_WRONG

    def add(self, address: str) -> None:
        if address not in self._times and len(self._times) >= _MAX_ADDRESSES:
            for known in list(self._times):
                self._recent(known)
        self._times.setdefault(address, deque()).append(time.monotonic())

    def clear(self, address: str) -> None:
        self._times.pop(address, None)

    def _recent(self, address: str) -> int:
        """How many wrong passcodes the address sent within the window, the older ones
        dropped; an address with none left is forgotten."""
        times = self._times.get(address)
        if times is None:
            return 0
        cutoff = time.monotonic() - WRONG_WINDOW_SECONDS
        while times and times[0] < cutoff:
            times.popleft()
        if not times:
            del self._times[address]
        return len(times)


def local_address(raw: str, fallback: str = "/studio") -> str:
    """Where the sign-in page sends the visitor afterwards: a path on this site only, never
    another site's address and never the sign-in page itself; else `fallback`."""
    address = raw.strip()
    try:
        parts = urlsplit(address)
    except ValueError:  # an unclosed IPv6 bracket, say
        return fallback
    if (
        parts.scheme
        or parts.netloc
        or not address.startswith("/")
        or address.startswith(("//", "/\\"))
        or parts.path in (SIGN_IN_PATH, SIGN_OUT_PATH)
    ):
        return fallback
    return address


def _fingerprint(passcode: str) -> bytes:
    return hashlib.sha256(passcode.encode("utf-8")).digest()


def host_name(host: str) -> str:
    """A Host header's name without its port: "localhost:8001" gives "localhost", and
    "[::1]:8000" gives "[::1]"."""
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.split(":", 1)[0]


def _from_studio(headers: Headers, host: str) -> bool:
    """Whether the request's Origin, or its Referer when it has no Origin, has the request's own
    host and port. True when it has neither: no browser page sent it."""
    origin = headers.get("origin")
    address = origin if origin is not None else headers.get("referer")
    if address is None:
        return True
    parts = urlsplit(address.strip())
    return parts.scheme in ("http", "https") and parts.netloc.lower() == host
