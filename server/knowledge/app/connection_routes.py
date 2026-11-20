"""Browser binding for personal provider consent; account authority lives in knowledge."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from server.auth import account_lifecycle, tenancy
from server.auth.bearer import missing_bearer, verify_bearer_header
from server.auth.oauth_store import PENDING_TTL, token_id
from server.clients.connected_oauth import OAuthError
from server.core import request_work
from server.core.diagnostics import private_work
from server.knowledge.app import connected_accounts as accounts
from server.settings import settings

router = APIRouter()


def _cookie(state: str) -> str:
    return "loom_connection_" + token_id(state)[:16]


@router.post("/v1/connect/{kind}/start")
async def start(kind: str, request: Request) -> JSONResponse:
    auth = await verify_bearer_header(request.headers.get("Authorization"))
    if auth is None:
        raise missing_bearer()
    # This route never applies an on-behalf header, including from a trusted caller.
    if request.headers.get("X-Loom-On-Behalf-Of"):
        raise accounts.unavailable()
    url, state, binding = await accounts.start(auth, kind)
    redirect = urlsplit(accounts.client(kind).redirect_uri)
    response = JSONResponse({"authorization_url": url}, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        _cookie(state),
        binding,
        max_age=PENDING_TTL,
        path=redirect.path,
        secure=redirect.scheme == "https",
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/v1/connect/{kind}/callback")
async def callback(kind: str, request: Request) -> Response:
    # Preserve the privacy mark through middleware and access-log completion,
    # including malformed requests whose URL still contains a code or state.
    with request_work.bind(request_work.RequestWork()):
        request_work.mark_mirrored()
        with private_work():
            try:
                async with asyncio.timeout(settings.knowledge_connected_oauth_timeout_s):
                    return await _callback(kind, request)
            except (OAuthError, TimeoutError):
                raise accounts.unavailable() from None


def _prefers_html(accept: str) -> bool:
    """Prefer JSON on ties and wildcards; browsers explicitly prefer HTML."""
    ranges = []
    for part in accept.lower().split(","):
        media, *params = part.strip().split(";")
        quality = 1.0
        for param in params:
            name, _, value = param.strip().partition("=")
            if name == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if not 0 <= quality <= 1:
            quality = 0.0
        ranges.append((media.strip(), quality))

    def quality_for(media: str) -> float:
        family = media.split("/")[0] + "/*"
        matches = [
            (2 if name == media else 1 if name == family else 0, quality)
            for name, quality in ranges
            if name in (media, family, "*/*")
        ]
        return max(matches, default=(-1, 0.0))[1]

    return quality_for("text/html") > quality_for("application/json")


async def _callback(kind: str, request: Request) -> Response:
    query = request.query_params
    if any(len(query.getlist(key)) > 1 for key in ("state", "code", "error")):
        raise accounts.unavailable()
    state = query.get("state", "")
    binding = request.cookies.get(_cookie(state), "")
    _key, pending = await accounts.verified_callback_pending(kind, state, binding)
    if not await tenancy.is_held(pending["org"]):
        raise accounts.unavailable()
    await accounts.callback(
        kind,
        state,
        binding,
        query.get("code", ""),
        denied=bool(query.get("error")),
    )
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Vary": "Accept"}
    response: Response
    if _prefers_html(request.headers.get("accept", "")):
        response = RedirectResponse(
            "/settings/connected-accounts", status_code=302, headers=headers
        )
    else:
        response = JSONResponse({"connected": True}, headers=headers)
    redirect = urlsplit(accounts.client(kind).redirect_uri)
    response.delete_cookie(_cookie(state), path=redirect.path)
    return response


def attach(app: FastAPI) -> None:
    account_lifecycle.bind(accounts.stop_subjects)
    app.include_router(router)
