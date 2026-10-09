"""The request guards (v6): a button can now spend money, so the studio answers only to this
computer's own names, and only its own pages may change anything.

- **Only localhost names.** Every request's Host must be `localhost`, `127.0.0.1` or `[::1]`
  with the studio's port, or it gets 400. This stops DNS rebinding, where a website points
  its own name at 127.0.0.1 to reach the studio from the browser.
- **Only the studio's own pages.** Every request that can change something (POST and the
  other unsafe methods) must carry an Origin, or a Referer when the Origin is absent, that is
  the studio's own address, or it gets 403. This stops another website open in the same
  browser from posting to the studio (cross-site request forgery), for example to start paid
  rounds. Browsers send an Origin with every cross-site POST, an opaque one as "null", which
  is refused; a request with neither header comes from no browser page (the sample script,
  a test client), so it is let through.

The port binding in `docker-compose.yml` (127.0.0.1 only) keeps other machines out.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOCAL_NAMES = ("localhost", "127.0.0.1", "[::1]")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

NOT_FROM_STUDIO = "This request did not come from the studio."
NOT_LOCAL = "The studio answers only on this computer, at localhost:{port}."


class LocalOnlyGuard:
    """ASGI middleware: the Host check on every request, the Origin check on every change."""

    def __init__(self, app: ASGIApp, *, port: int) -> None:
        self.app = app
        self.port = port
        hosts = {f"{name}:{port}" for name in LOCAL_NAMES}
        if port == 80:  # a browser leaves the default port out of Host and Origin
            hosts.update(LOCAL_NAMES)
        self.hosts = frozenset(hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if headers.get("host", "").strip().lower() not in self.hosts:
            response = PlainTextResponse(NOT_LOCAL.format(port=self.port), status_code=400)
        elif scope["method"] not in SAFE_METHODS and not self._from_studio(headers):
            response = PlainTextResponse(NOT_FROM_STUDIO, status_code=403)
        else:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)

    def _from_studio(self, headers: Headers) -> bool:
        """Whether the request's Origin, or its Referer when it has no Origin, is the studio's
        own address. True when it has neither: no browser page sent it."""
        origin = headers.get("origin")
        if origin is not None:
            return self._is_own(origin)
        referer = headers.get("referer")
        if referer is not None:
            return self._is_own(referer)
        return True

    def _is_own(self, address: str) -> bool:
        parts = urlsplit(address.strip())
        return parts.scheme in ("http", "https") and parts.netloc.lower() in self.hosts
