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
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# The names the studio answers to: this computer's, and the compose service's.
LOCAL_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]", "studio"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

NOT_FROM_STUDIO = "This request did not come from the studio."
NOT_LOCAL = "The studio answers only on this computer, at localhost."


class LocalOnlyGuard:
    """ASGI middleware: the Host check on every request, the Origin check on every change."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "").strip().lower()
        if host_name(host) not in LOCAL_NAMES:
            response = PlainTextResponse(NOT_LOCAL, status_code=400)
        elif scope["method"] not in SAFE_METHODS and not _from_studio(headers, host):
            response = PlainTextResponse(NOT_FROM_STUDIO, status_code=403)
        else:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)


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
