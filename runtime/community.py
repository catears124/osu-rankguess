from __future__ import annotations

import asyncio
import hashlib
import math
import os
import random
import secrets
import threading
from typing import Any

import database as _database

_INSTALLED = False
_SCHEDULER_READY = False
_SCHEDULER_LOCK = threading.Lock()
_SEED_LOCK: asyncio.Lock | None = None
_ORIGINAL_DISTRIBUTION = _database.challenge_guess_distribution


def _baseline_target() -> int:
    try:
        value = int(os.getenv("COMMUNITY_BASELINE_TARGET", "24") or 24)
    except (TypeError, ValueError):
        value = 24
    return max(0, min(100, value))


def _bin_edges(rank_population: int, bin_count: int) -> list[int]:
    maximum = max(2, int(rank_population))
    count = max(4, int(bin_count))
    log_maximum = math.log10(maximum)
    edges = [1]
    for index in range(1, count + 1):
        edge = int(round(10 ** (log_maximum * index / count)))
        edges.append(max(edges[-1] + 1, min(maximum, edge)))
    edges[-1] = maximum
    return edges


def _bin_index(rank: int, rank_population: int, bin_count: int) -> int:
    maximum = max(2, int(rank_population))
    clipped = max(1, min(maximum, int(rank)))
    ratio = math.log10(clipped) / math.log10(maximum)
    return min(bin_count - 1, max(0, int(ratio * bin_count)))


def _synthetic_baseline(
    *,
    replay_id: str,
    challenge_key: str,
    rank_population: int,
    count: int,
    actual_rank: int | None,
    predicted_rank: int | None,
) -> list[int]:
    if count <= 0:
        return []

    maximum = max(2, int(rank_population))
    log_maximum = math.log10(maximum)
    actual_log = math.log10(max(1, min(maximum, int(actual_rank or predicted_rank or 50_000))))
    predicted_log = math.log10(max(1, min(maximum, int(predicted_rank or actual_rank or 50_000))))
    seed = hashlib.sha256(f"community-prior-v1:{replay_id}:{challenge_key}".encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(seed[:8], "big"))

    values: list[int] = []
    for _ in range(count):
        draw = rng.random()
        if draw < 0.58:
            log_rank = rng.gauss(actual_log, 0.30)
        elif draw < 0.88:
            log_rank = rng.gauss(predicted_log, 0.36)
        else:
            log_rank = rng.uniform(0.0, log_maximum)
        values.append(max(1, min(maximum, int(round(10 ** log_rank)))))
    return values


def _smoothed_distribution(
    *,
    replay_id: str,
    mode: str,
    challenge_key: str,
    rank_population: int,
    bin_count: int = 12,
) -> dict[str, Any]:
    raw = _ORIGINAL_DISTRIBUTION(
        replay_id=replay_id,
        mode=mode,
        challenge_key=challenge_key,
        rank_population=rank_population,
        bin_count=bin_count,
    )
    observed_count = max(0, int(raw.get("count") or 0))
    baseline_count = max(0, _baseline_target() - observed_count)
    count = max(4, int(bin_count))
    edges = _bin_edges(rank_population, count)

    observed_bins = [0] * count
    for index, item in enumerate((raw.get("bins") or [])[:count]):
        if isinstance(item, dict):
            observed_bins[index] = max(0, int(item.get("count") or 0))

    row = _database.get_challenge_submission(replay_id) or {}
    baseline_bins = [0] * count
    for rank in _synthetic_baseline(
        replay_id=replay_id,
        challenge_key=challenge_key,
        rank_population=rank_population,
        count=baseline_count,
        actual_rank=row.get("actual_rank"),
        predicted_rank=row.get("predicted_rank"),
    ):
        baseline_bins[_bin_index(rank, rank_population, count)] += 1

    result = dict(raw)
    result.update(
        {
            "count": observed_count,
            "observedCount": observed_count,
            "baselineCount": baseline_count,
            "baselineTarget": _baseline_target(),
            "smoothed": baseline_count > 0,
            "bins": [
                {
                    "lower": edges[index],
                    "upper": edges[index + 1],
                    "count": observed_bins[index] + baseline_bins[index],
                    "observedCount": observed_bins[index],
                    "baselineCount": baseline_bins[index],
                }
                for index in range(count)
            ],
            "rawBins": [
                {
                    "lower": edges[index],
                    "upper": edges[index + 1],
                    "count": observed_bins[index],
                }
                for index in range(count)
            ],
        }
    )
    return result


def _ensure_scheduler_schema() -> None:
    global _SCHEDULER_READY
    if _SCHEDULER_READY or not _database.database_configured():
        return
    with _SCHEDULER_LOCK:
        if _SCHEDULER_READY:
            return
        _database.ensure_schema()
        with _database._connect() as connection:  # noqa: SLF001
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS background_job_schedule (
                        job_key TEXT PRIMARY KEY,
                        next_run_at TIMESTAMPTZ NOT NULL,
                        last_run_at TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
        _SCHEDULER_READY = True


def _claim_seed_window(
    job_key: str = "gallery-random-seed",
    minimum_minutes: int = 20,
    maximum_minutes: int = 120,
) -> dict[str, Any]:
    _ensure_scheduler_schema()
    minimum = max(20, int(minimum_minutes))
    maximum = max(minimum, int(maximum_minutes))
    delay = minimum + secrets.randbelow(maximum - minimum + 1)

    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO background_job_schedule (job_key, next_run_at)
                VALUES (%s, NOW() - INTERVAL '1 second')
                ON CONFLICT (job_key) DO NOTHING
                """,
                (job_key,),
            )
            cursor.execute(
                """
                UPDATE background_job_schedule
                SET last_run_at = NOW(),
                    next_run_at = NOW() + (%s * INTERVAL '1 minute'),
                    updated_at = NOW()
                WHERE job_key = %s
                  AND next_run_at <= NOW()
                RETURNING job_key, next_run_at, last_run_at
                """,
                (delay, job_key),
            )
            claimed = cursor.fetchone()
            if claimed:
                return {
                    "claimed": True,
                    "delayMinutes": delay,
                    "nextRunAt": claimed["next_run_at"],
                    "lastRunAt": claimed["last_run_at"],
                }
            cursor.execute(
                """
                SELECT job_key, next_run_at, last_run_at
                FROM background_job_schedule
                WHERE job_key = %s
                """,
                (job_key,),
            )
            current = cursor.fetchone() or {}
            return {
                "claimed": False,
                "delayMinutes": None,
                "nextRunAt": current.get("next_run_at"),
                "lastRunAt": current.get("last_run_at"),
            }


def _schedule_retry(job_key: str = "gallery-random-seed", minutes: int = 20) -> None:
    try:
        _ensure_scheduler_schema()
        with _database._connect() as connection:  # noqa: SLF001
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE background_job_schedule
                    SET next_run_at = LEAST(next_run_at, NOW() + (%s * INTERVAL '1 minute')),
                        updated_at = NOW()
                    WHERE job_key = %s
                    """,
                    (max(20, int(minutes)), job_key),
                )
    except Exception:
        pass


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _install_fastapi_route() -> None:
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import JSONResponse
    except Exception:
        return
    if getattr(FastAPI, "_rankguess_community_patch", False):
        return

    original_init = FastAPI.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        title = kwargs.get("title") or getattr(self, "title", "")
        if title != "osu!rankguess":
            return

        @self.get("/api/cron/seed-gallery", include_in_schema=False)
        async def random_gallery_seed(request: Request) -> JSONResponse:
            import app as app_module

            if not app_module._cron_authorized(request):
                raise HTTPException(
                    status_code=401,
                    detail={"code": "unauthorized", "message": "Missing or invalid cron authorization."},
                )
            if not _database.database_configured():
                raise HTTPException(
                    status_code=503,
                    detail={"code": "database_not_configured", "message": "Connect Postgres before seeding the gallery."},
                )

            claim = await asyncio.to_thread(_claim_seed_window)
            if not claim["claimed"]:
                return JSONResponse(
                    {
                        "ok": True,
                        "skipped": True,
                        "reason": "not_due",
                        "schedule": "random-20-120m",
                        "nextRunAt": _iso(claim.get("nextRunAt")),
                        "lastRunAt": _iso(claim.get("lastRunAt")),
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
            except Exception as exc:
                await asyncio.to_thread(_schedule_retry)
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
                }
            )
            return JSONResponse(result)

    FastAPI.__init__ = patched_init
    FastAPI._rankguess_community_patch = True


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _database.challenge_guess_distribution = _smoothed_distribution
    _install_fastapi_route()
    _INSTALLED = True
