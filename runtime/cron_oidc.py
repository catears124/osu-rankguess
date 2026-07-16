"""Authenticate production cron calls with GitHub Actions OIDC.

The server still accepts the configured CRON_SECRET for manual or provider-owned
cron callers. GitHub Actions no longer needs a synchronized copy: a valid,
short-lived GitHub OIDC token is verified and translated into the application's
internal cron authorization header before FastAPI dispatches the route.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import threading
from typing import Any

_ISSUER = "https://token.actions.githubusercontent.com"
_JWKS_URL = f"{_ISSUER}/.well-known/jwks"
_AUDIENCE = "osu-rankguess-cron"
_REPOSITORY = "catears124/osu-rankguess"
_REPOSITORY_ID = "1297721323"
_REF = "refs/heads/main"
_WORKFLOW_REF = f"{_REPOSITORY}/.github/workflows/gallery-cron.yml@{_REF}"
_ALLOWED_EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})
_CRON_PATH_PREFIX = "/api/cron/"
_INTERNAL_SECRET = secrets.token_urlsafe(32)
_JWK_CLIENT: Any = None
_JWK_LOCK = threading.Lock()


def _claims_allowed(claims: dict[str, Any]) -> bool:
    return (
        str(claims.get("repository") or "") == _REPOSITORY
        and str(claims.get("repository_id") or "") == _REPOSITORY_ID
        and str(claims.get("ref") or "") == _REF
        and str(claims.get("workflow_ref") or "") == _WORKFLOW_REF
        and str(claims.get("event_name") or "") in _ALLOWED_EVENTS
    )


def _jwk_client():
    global _JWK_CLIENT
    if _JWK_CLIENT is not None:
        return _JWK_CLIENT
    with _JWK_LOCK:
        if _JWK_CLIENT is None:
            from jwt import PyJWKClient

            _JWK_CLIENT = PyJWKClient(_JWKS_URL)
    return _JWK_CLIENT


def _verify_github_oidc_token(token: str) -> dict[str, Any] | None:
    if token.count(".") != 2:
        return None
    try:
        import jwt

        signing_key = _jwk_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=_AUDIENCE,
            issuer=_ISSUER,
            leeway=30,
            options={
                "require": [
                    "aud",
                    "exp",
                    "iat",
                    "iss",
                    "nbf",
                    "repository",
                    "repository_id",
                    "ref",
                    "workflow_ref",
                    "event_name",
                ]
            },
        )
    except Exception as exc:
        print(
            json.dumps(
                {"event": "cron_oidc_rejected", "error": type(exc).__name__},
                separators=(",", ":"),
            ),
            flush=True,
        )
        return None
    return claims if _claims_allowed(dict(claims)) else None


def _authorization_header(scope: dict[str, Any]) -> str:
    for name, value in scope.get("headers") or []:
        if bytes(name).lower() == b"authorization":
            return bytes(value).decode("latin-1")
    return ""


def _replace_authorization(scope: dict[str, Any], value: str) -> dict[str, Any]:
    rewritten = [
        (bytes(name), bytes(header_value))
        for name, header_value in (scope.get("headers") or [])
        if bytes(name).lower() != b"authorization"
    ]
    rewritten.append((b"authorization", value.encode("latin-1")))
    updated = dict(scope)
    updated["headers"] = rewritten
    updated["rankguess.cron_auth"] = "github_oidc"
    return updated


def _internal_secret() -> str:
    configured = os.getenv("CRON_SECRET")
    if configured:
        return configured
    os.environ["CRON_SECRET"] = _INTERNAL_SECRET
    return _INTERNAL_SECRET


class GitHubOIDCCronMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not str(scope.get("path") or "").startswith(_CRON_PATH_PREFIX):
            await self.app(scope, receive, send)
            return

        supplied = _authorization_header(scope)
        configured = os.getenv("CRON_SECRET")
        if configured and hmac.compare_digest(supplied, f"Bearer {configured}"):
            await self.app(scope, receive, send)
            return

        token = supplied.removeprefix("Bearer ").strip() if supplied.startswith("Bearer ") else ""
        claims = await asyncio.to_thread(_verify_github_oidc_token, token)
        if claims is None:
            await self.app(scope, receive, send)
            return

        print(
            json.dumps(
                {
                    "event": "cron_oidc_authorized",
                    "eventName": claims.get("event_name"),
                    "runID": claims.get("run_id"),
                    "workflowRef": claims.get("workflow_ref"),
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        await self.app(
            _replace_authorization(scope, f"Bearer {_internal_secret()}"),
            receive,
            send,
        )


def install() -> None:
    try:
        from fastapi import FastAPI
    except Exception:
        return
    if getattr(FastAPI, "_rankguess_cron_oidc_patch", False):
        return

    original_init = FastAPI.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        title = kwargs.get("title") or getattr(self, "title", "")
        if title == "osu!rankguess":
            self.add_middleware(GitHubOIDCCronMiddleware)

    FastAPI.__init__ = patched_init
    FastAPI._rankguess_cron_oidc_patch = True
