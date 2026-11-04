"""The dashboard: the React single-page app served at the server root.

`GET /` and every page path (`/memories`, `/admin/review_queue`, ...) answer
`index.html` with the boot snippet; `/assets/*` serves the built Vite bundle
and `/favicon.svg` the tab icon. Only the first path segments of the app's
views (`PAGE_SEGMENTS`) are pages, and `/me`, where a sign-in lands; any other
path keeps the router's 404.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from server.core.paths import FRONTEND_DIST
from server.settings import settings

_FRONTEND_DIST = FRONTEND_DIST

# The first URL segment of every dashboard view: `viewToPath` over `ALL_VIEWS`
# in frontend/src/lib/state.ts (a unit test keeps the two equal).
PAGE_SEGMENTS = frozenset(
    {
        "admin",
        "assistant",
        "conversations",
        "flow",
        "graph",
        "library",
        "memories",
        "profile",
        "recall",
        "repositories",
        "runs",
        "settings",
        "spaces",
        "tasks",
    }
)


def dashboard_csp(nonce: str) -> str:
    """The dashboard page's Content-Security-Policy. Scripts run only from the
    bundle's own origin or the one boot snippet carrying `nonce`; a document
    preview is a text node or a blob: image, never a script source or a frame."""
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        # Vite inlines small font subsets as data: URLs.
        "font-src 'self' data:; "
        "img-src 'self' data: blob:; "
        "frame-src 'none'; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )


def _js_string(value: str) -> str:
    """`value` as a JS string literal safe inside a `<script>`: JSON-escaped,
    with `</` and `<!--` broken up so the HTML parser cannot end the element."""
    return json.dumps(value).replace("</", "<\\/").replace("<!--", "<\\!--")


async def favicon(request: Request) -> Response:
    """Tab icon. It lives at the dist root, outside the /assets mount."""
    from starlette.responses import FileResponse

    path = _FRONTEND_DIST / "favicon.svg"
    if not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type="image/svg+xml")


async def dashboard_page(request: Request) -> Response:
    """index.html for every page path; the app reads window.location itself.

    The root serves the page like every other path: the client's landing rule
    chooses the first view from the token's namespaces, so a server redirect
    to /setup would make every visit an explicit Setup deep link.
    """
    from starlette.responses import RedirectResponse

    # Federated-deployment escape hatch: send page loads to an aggregator.
    # Auth-disabled mode skips this — the solo user IS the audience there.
    if settings.dashboard_redirect_url and settings.auth_actually_required():
        return RedirectResponse(url=settings.dashboard_redirect_url, status_code=302)
    return await _page(request)


async def signed_in_page(request: Request) -> Response:
    """`/me`, where a sign-in lands with its token in the URL fragment: the
    page itself, never the aggregator redirect, which the browser would follow
    carrying the fragment to another origin. The app reads the token, then
    picks its first view as at the root."""
    return await _page(request)


async def _not_a_page(request: Request) -> Response:
    """`/me/`: not the landing page, and no redirect to it either."""
    from starlette.responses import JSONResponse

    return JSONResponse({"detail": "Not Found"}, status_code=404)


async def _page(request: Request) -> Response:
    """index.html with the boot snippet."""
    from starlette.responses import HTMLResponse, PlainTextResponse

    from server.auth.bearer import bearer_token
    from server.auth.jwt import TokenError

    try:
        has_bearer = bearer_token(request.headers.get("Authorization")) is not None
    except TokenError:
        has_bearer = False

    # Single-user identity with auth_required left true is still auth-off.
    auth_disabled = not settings.auth_actually_required()
    # Auth off: the default namespace needs its loom.namespaces row (cached).
    default_ns = settings.default_namespace
    if auth_disabled:
        from server.auth.default_namespace import ensure_default_namespace_cached

        await ensure_default_namespace_cached()
    # Self-signup availability rides the boot snippet rather than being probed: probing by
    # POSTing a real signup would be a read that writes (claiming a namespace on a fresh
    # instance, then a 409 on every later load). The server already knows the answer.
    # With SSO on, the sign-in screen is one button to the identity provider:
    # password sign-in and self-signup are both refused server-side.
    sso = settings.oauth_login_provider == "oidc"
    signup_open = settings.self_signup == "open" and not sso
    nonce = secrets.token_urlsafe(16)
    boot_snippet = (
        f'<script nonce="{nonce}">'
        f"window.__LOOM_PROXY_AUTH = {'true' if has_bearer else 'false'};"
        f"window.__LOOM_AUTH_DISABLED = {'true' if auth_disabled else 'false'};"
        f"window.__LOOM_SELF_SIGNUP = {'true' if signup_open else 'false'};"
        f"window.__LOOM_SSO = {'true' if sso else 'false'};"
        f"window.__LOOM_DEFAULT_NAMESPACE = {_js_string(default_ns)};"
        f"</script>"
    )

    built_index = _FRONTEND_DIST / "index.html"
    if not built_index.is_file():
        return PlainTextResponse(
            "Dashboard not built. Run `npm --prefix frontend run build` "
            "or use the published Docker image.",
            status_code=503,
        )
    html = built_index.read_text()
    if "</head>" in html:
        html = html.replace("</head>", boot_snippet + "\n</head>", 1)
    else:
        html = boot_snippet + html
    # no-store: the HTML references content-hashed assets that rotate per
    # build; a cached HTML would point at a bundle hash that no longer exists.
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-store", "Content-Security-Policy": dashboard_csp(nonce)},
    )


class _BundleAssets(StaticFiles):
    """/assets from the build: a 404 until `npm run build` lands, then served
    without a restart (`npm run dev` needs no bundle on disk)."""

    def __init__(self) -> None:
        super().__init__(directory=_FRONTEND_DIST / "assets", check_dir=False)

    async def check_config(self) -> None:
        return None

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Local disk first; a file this build lacks (a page from another build
        during a rolling upgrade) from the bundle every replica publishes."""
        from starlette.exceptions import HTTPException

        try:
            return await super().get_response(path, scope)
        except HTTPException as e:
            if e.status_code != 404:
                raise
            from server.storage.frontend_assets import serve_asset

            shared = await serve_asset(path)
            if shared is None:
                raise
            return shared


def _get(path: str, endpoint: Any) -> Route:
    return Route(path, endpoint, methods=["GET"], include_in_schema=False)


def attach(app: Any) -> None:
    """Mount the dashboard: the bundle, the tab icon and a page route for `/`
    and each view segment (with any deeper path)."""
    app.mount("/assets", _BundleAssets(), name="dashboard_assets")
    pages = [_get("/", dashboard_page), _get("/me", signed_in_page), _get("/me/", _not_a_page)]
    for segment in sorted(PAGE_SEGMENTS):
        pages += [
            _get(f"/{segment}", dashboard_page),
            _get(f"/{segment}/{{rest:path}}", dashboard_page),
        ]
    app.router.routes.extend(
        [
            _get("/favicon.svg", favicon),
            *pages,
        ]
    )


__all__ = ["PAGE_SEGMENTS", "attach", "dashboard_csp", "dashboard_page", "signed_in_page"]
