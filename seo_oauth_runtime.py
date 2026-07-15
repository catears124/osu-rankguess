"""Optional osu! OAuth routes for osu!rankguess."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

_INSTALLED = False


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _session_secret() -> bytes | None:
    value = (
        os.getenv("OAUTH_SESSION_SECRET")
        or os.getenv("CACHE_SIGNING_SECRET")
        or os.getenv("OSU_CLIENT_SECRET")
    )
    return value.encode("utf-8") if value else None


def _encode_user(user: dict[str, Any]) -> str:
    secret = _session_secret()
    if not secret:
        raise RuntimeError("OAuth session signing is not configured")
    payload = json.dumps(user, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64encode(payload)
    signature = _b64encode(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def _decode_user(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    secret = _session_secret()
    if not secret:
        return None
    try:
        encoded, signature = value.split(".", 1)
        expected = _b64encode(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64decode(encoded))
        if float(payload.get("exp") or 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def _configured() -> bool:
    return bool(os.getenv("OSU_CLIENT_ID") and os.getenv("OSU_CLIENT_SECRET"))


def _redirect_uri(request: Any) -> str:
    configured = (os.getenv("OSU_OAUTH_REDIRECT_URI") or "").strip()
    if configured:
        return configured
    return str(request.url_for("osu_oauth_callback"))


def register_routes(app: Any) -> None:
    """Register OAuth routes directly on an existing FastAPI application."""
    if getattr(app.state, "rankguess_oauth_routes", False):
        return

    from fastapi import HTTPException, Request
    from fastapi.responses import JSONResponse, RedirectResponse

    app.state.rankguess_oauth_routes = True

    @app.get("/api/auth/status", include_in_schema=False)
    async def osu_auth_status(request: Request) -> JSONResponse:
        user = _decode_user(request.cookies.get("rankguess_osu_user"))
        return JSONResponse(
            {
                "configured": _configured(),
                "authenticated": user is not None,
                "user": user,
            }
        )

    @app.get("/api/auth/osu", include_in_schema=False)
    async def osu_oauth_start(request: Request) -> RedirectResponse:
        if not _configured():
            raise HTTPException(status_code=503, detail="osu! OAuth is not configured")

        state = secrets.token_urlsafe(32)
        params = {
            "client_id": os.environ["OSU_CLIENT_ID"],
            "redirect_uri": _redirect_uri(request),
            "response_type": "code",
            "scope": "public identify",
            "state": state,
        }
        response = RedirectResponse(
            url=f"https://osu.ppy.sh/oauth/authorize?{urlencode(params)}",
            status_code=302,
        )
        response.set_cookie(
            "rankguess_oauth_state",
            state,
            max_age=600,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/api/auth/osu/callback", name="osu_oauth_callback", include_in_schema=False)
    async def osu_oauth_callback(request: Request, code: str, state: str) -> RedirectResponse:
        expected_state = request.cookies.get("rankguess_oauth_state")
        if not expected_state or not hmac.compare_digest(expected_state, state):
            raise HTTPException(status_code=400, detail="Invalid OAuth state")

        token_data = {
            "client_id": os.environ.get("OSU_CLIENT_ID", ""),
            "client_secret": os.environ.get("OSU_CLIENT_SECRET", ""),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _redirect_uri(request),
        }
        timeout = httpx.Timeout(20.0, connect=8.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            token_response = await client.post(
                "https://osu.ppy.sh/oauth/token",
                data=token_data,
                headers={"Accept": "application/json"},
            )
            if token_response.status_code != 200:
                raise HTTPException(status_code=502, detail="osu! token exchange failed")

            access_token = str(token_response.json().get("access_token") or "")
            me_response = await client.get(
                "https://osu.ppy.sh/api/v2/me/osu",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
            if me_response.status_code != 200:
                raise HTTPException(status_code=502, detail="osu! profile lookup failed")
            profile = me_response.json()

        user = {
            "id": profile.get("id"),
            "username": profile.get("username"),
            "avatarURL": profile.get("avatar_url"),
            "countryCode": profile.get("country_code"),
            "exp": int(time.time()) + 7 * 24 * 60 * 60,
        }
        response = RedirectResponse(url="/daily", status_code=302)
        response.delete_cookie("rankguess_oauth_state", path="/")
        response.set_cookie(
            "rankguess_osu_user",
            _encode_user(user),
            max_age=7 * 24 * 60 * 60,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/api/auth/logout", include_in_schema=False)
    async def osu_oauth_logout() -> RedirectResponse:
        response = RedirectResponse(url="/daily", status_code=302)
        response.delete_cookie("rankguess_osu_user", path="/")
        return response


def _install_fastapi_patch() -> None:
    try:
        from fastapi import FastAPI
    except Exception:
        return
    if getattr(FastAPI, "_rankguess_oauth_patch", False):
        return

    original_init = FastAPI.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        title = kwargs.get("title") or getattr(self, "title", "")
        if title == "osu!rankguess":
            register_routes(self)

    FastAPI.__init__ = patched_init
    FastAPI._rankguess_oauth_patch = True


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_fastapi_patch()
    _INSTALLED = True
