"""Reliable cron tick for gallery ingestion and daily challenge prewarming."""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timezone
from typing import Any

import community_runtime as _community
import database as _database

_INSTALLED = False
_SEED_LOCK: asyncio.Lock | None = None
_JOB_KEY = "gallery-random-seed-v2"
_MINIMUM_MINUTES = 20
_MAXIMUM_MINUTES = 120
_RETRY_MINUTES = 20
_ORIGINAL_CLAIM_SEED_WINDOW = _community._claim_seed_window  # noqa: SLF001
_ORIGINAL_SCHEDULE_RETRY = _community._schedule_retry  # noqa: SLF001


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _daily_status() -> dict[str, Any]:
    challenge_date = datetime.now(timezone.utc).date()
    rows = _database.get_daily_challenge(challenge_date, 3)
    return {
        "date": challenge_date.isoformat(),
        "ready": len(rows) == 3,
        "replayCount": len(rows),
        "eligibleReplays": _database.challenge_count(),
    }


def _claim_seed_window(
    job_key: str = "gallery-random-seed",
    minimum_minutes: int = _MINIMUM_MINUTES,
    maximum_minutes: int = _MAXIMUM_MINUTES,
) -> dict[str, Any]:
    """Use the requested 20-minute to two-hour window for every cron caller."""
    minimum = max(_MINIMUM_MINUTES, int(minimum_minutes))
    maximum = max(minimum, int(maximum_minutes))
    return _ORIGINAL_CLAIM_SEED_WINDOW(job_key, minimum, maximum)


def _schedule_retry(
    job_key: str = "gallery-random-seed",
    minutes: int = _RETRY_MINUTES,
) -> None:
    _ORIGINAL_SCHEDULE_RETRY(job_key, max(_RETRY_MINUTES, int(minutes)))


def _claim_window() -> dict[str, Any]:
    return _claim_seed_window(_JOB_KEY, _MINIMUM_MINUTES, _MAXIMUM_MINUTES)


def _retry_current_job() -> None:
    _schedule_retry(_JOB_KEY, _RETRY_MINUTES)


def _install_fastapi_route() -> None:
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import JSONResponse
    except Exception:
        return
    if getattr(FastAPI, "_rankguess_cron_runtime_patch", False):
        return

    original_init = FastAPI.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        title = kwargs.get("title") or getattr(self, "title", "")
        if title != "osu!rankguess":
            return

        @self.get("/api/cron/tick", include_in_schema=False)
        async def cron_tick(request: Request) -> JSONResponse:
            import app as app_module

            if not app_module._cron_authorized(request):
                raise HTTPException(
                    status_code=401,
                    detail={"code": "unauthorized", "message": "Missing or invalid cron authorization."},
                )
            if not _database.database_configured():
                raise HTTPException(
                    status_code=503,
                    detail={"code": "database_not_configured", "message": "Connect Postgres before running cron."},
                )

            try:
                daily_before = await asyncio.to_thread(_daily_status)
                claim = await asyncio.to_thread(_claim_window)
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "cron_state_failed", "message": str(exc)},
                ) from exc

            if not claim["claimed"]:
                return JSONResponse(
                    {
                        "ok": True,
                        "skipped": True,
                        "reason": "not_due",
                        "schedule": "random-20-120m",
                        "nextRunAt": _iso(claim.get("nextRunAt")),
                        "lastRunAt": _iso(claim.get("lastRunAt")),
                        "daily": daily_before,
                    }
                )

            global _SEED_LOCK
            if _SEED_LOCK is None:
                _SEED_LOCK = asyncio.Lock()

            slot = secrets.randbelow(3)
            try:
                async with _SEED_LOCK:
                    previous_target = app_module.GALLERY_SEED_TARGET
                    app_module.GALLERY_SEED_TARGET = max(previous_target, 1_000_000_000)
                    try:
                        result = await app_module.seed_gallery_once(slot)
                    finally:
                        app_module.GALLERY_SEED_TARGET = previous_target
                daily_after = await asyncio.to_thread(_daily_status)
            except Exception as exc:
                await asyncio.to_thread(_retry_current_job)
                print(
                    json.dumps(
                        {
                            "event": "cron_tick_failed",
                            "slot": slot,
                            "error": repr(exc),
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                raise HTTPException(
                    status_code=502,
                    detail={"code": "gallery_seed_failed", "message": str(exc)},
                ) from exc

            result.update(
                {
                    "schedule": "random-20-120m",
                    "delayMinutes": claim["delayMinutes"],
                    "nextRunAt": _iso(claim.get("nextRunAt")),
                    "slot": slot,
                    "daily": daily_after,
                }
            )
            print(
                json.dumps(
                    {"event": "cron_tick_complete", **result},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                flush=True,
            )
            return JSONResponse(result)

    FastAPI.__init__ = patched_init
    FastAPI._rankguess_cron_runtime_patch = True


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _community._claim_seed_window = _claim_seed_window  # noqa: SLF001
    _community._schedule_retry = _schedule_retry  # noqa: SLF001
    _install_fastapi_route()
    _INSTALLED = True
