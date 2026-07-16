"""Durable cron tick for gallery ingestion and daily challenge rotation.

The old cron performed replay discovery, o!rdr submission, render polling,
inference, and persistence in one serverless request. A timeout anywhere after
submission lost the in-flight work and the next tick started over. This module
stores one pending render in Postgres, polls it on later ticks, and only starts a
new render after the pending one has been finalized.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import threading
from datetime import date, datetime, timezone
from typing import Any

import community_runtime as _community
import database as _database
from backend import database as _backend_database

_INSTALLED = False
_SEED_LOCK: asyncio.Lock | None = None
_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()
_JOB_KEY = "gallery-random-seed-v3"
_PENDING_JOB_KEY = "gallery"
_MINIMUM_MINUTES = 20
_MAXIMUM_MINUTES = 120
_RETRY_MINUTES = 20
_MAX_PENDING_HOURS = 12
_MAX_FINALIZE_FAILURES = 4
_ORIGINAL_CLAIM_SEED_WINDOW = _community._claim_seed_window  # noqa: SLF001
_ORIGINAL_SCHEDULE_RETRY = _community._schedule_retry  # noqa: SLF001


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _daily_replay_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item]
    return []


def _daily_ids_for_date(challenge_date: date, count: int) -> list[str]:
    """Select never-used replays first, then the least recently used replays.

    Existing daily rows stay stable once anyone has guessed them. A duplicate
    current-day row may be regenerated before the first guess when the pool is
    large enough to provide alternatives.
    """
    salt = os.getenv("DAILY_CHALLENGE_SALT") or os.getenv("CACHE_SIGNING_SECRET") or "osu-rankguess-daily"
    with _backend_database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT replay_ids FROM daily_challenges WHERE challenge_date = %s",
                (challenge_date,),
            )
            existing = cursor.fetchone()
            existing_ids = _daily_replay_ids(existing["replay_ids"]) if existing else []

            cursor.execute(
                """
                SELECT public_id
                FROM replay_submissions
                WHERE published = TRUE
                  AND actual_rank IS NOT NULL
                  AND actual_rank > 0
                ORDER BY created_at DESC
                LIMIT 2000
                """
            )
            candidates = [str(row["public_id"]) for row in cursor.fetchall()]
            if len(candidates) < count:
                return existing_ids if len(existing_ids) >= count else []

            if len(existing_ids) >= count:
                cursor.execute(
                    """
                    SELECT replay_ids
                    FROM daily_challenges
                    WHERE challenge_date < %s
                    ORDER BY challenge_date DESC
                    LIMIT 1
                    """,
                    (challenge_date,),
                )
                previous = cursor.fetchone()
                previous_ids = _daily_replay_ids(previous["replay_ids"]) if previous else []
                repeated_previous_day = bool(previous_ids) and set(existing_ids) == set(previous_ids)

                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM challenge_guesses
                    WHERE mode = 'daily' AND challenge_key = %s
                    """,
                    (challenge_date.isoformat(),),
                )
                guess_row = cursor.fetchone() or {"count": 0}
                has_guesses = int(guess_row["count"] or 0) > 0

                if not repeated_previous_day or has_guesses or len(candidates) < count * 2:
                    return existing_ids
                cursor.execute(
                    "DELETE FROM daily_challenges WHERE challenge_date = %s",
                    (challenge_date,),
                )

            cursor.execute(
                """
                SELECT challenge_date, replay_ids
                FROM daily_challenges
                WHERE challenge_date < %s
                ORDER BY challenge_date DESC
                LIMIT 365
                """,
                (challenge_date,),
            )
            last_used: dict[str, date] = {}
            for row in cursor.fetchall():
                used_date = row["challenge_date"]
                for replay_id in _daily_replay_ids(row["replay_ids"]):
                    if replay_id not in last_used:
                        last_used[replay_id] = used_date

            def selection_key(public_id: str) -> tuple[int, date, bytes]:
                used = last_used.get(public_id)
                digest = hashlib.sha256(
                    f"{salt}:{challenge_date.isoformat()}:{public_id}".encode("utf-8")
                ).digest()
                return (1 if used else 0, used or date.min, digest)

            selected = sorted(candidates, key=selection_key)[:count]
            cursor.execute(
                """
                INSERT INTO daily_challenges (challenge_date, replay_ids)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (challenge_date) DO NOTHING
                """,
                (challenge_date, json.dumps(selected)),
            )
            cursor.execute(
                "SELECT replay_ids FROM daily_challenges WHERE challenge_date = %s",
                (challenge_date,),
            )
            row = cursor.fetchone()
            return _daily_replay_ids(row["replay_ids"]) if row else selected


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


def _ensure_pending_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        _database.ensure_schema()
        with _database._connect() as connection:  # noqa: SLF001
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS gallery_seed_jobs (
                        job_key TEXT PRIMARY KEY,
                        slot INTEGER NOT NULL,
                        replay_hash TEXT NOT NULL UNIQUE,
                        replay_bytes BYTEA NOT NULL,
                        score_id BIGINT NOT NULL,
                        render_id BIGINT NOT NULL UNIQUE,
                        player TEXT NOT NULL,
                        score_payload JSONB NOT NULL,
                        user_payload JSONB NOT NULL,
                        finalize_failures INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
        _SCHEMA_READY = True


def _pending_job() -> dict[str, Any] | None:
    _ensure_pending_schema()
    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM gallery_seed_jobs WHERE job_key = %s",
                (_PENDING_JOB_KEY,),
            )
            return cursor.fetchone()


def _save_pending_job(candidate: dict[str, Any], render_id: int, slot: int) -> dict[str, Any]:
    _ensure_pending_schema()
    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO gallery_seed_jobs (
                    job_key, slot, replay_hash, replay_bytes, score_id, render_id,
                    player, score_payload, user_payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                ON CONFLICT (job_key) DO NOTHING
                RETURNING *
                """,
                (
                    _PENDING_JOB_KEY,
                    int(slot),
                    str(candidate["replayHash"]),
                    bytes(candidate["replayBytes"]),
                    int(candidate["scoreID"]),
                    int(render_id),
                    str(candidate["cached"].player),
                    json.dumps(candidate.get("score") or {}, default=str),
                    json.dumps(candidate.get("user") or {}, default=str),
                ),
            )
            saved = cursor.fetchone()
            if saved:
                return saved
            cursor.execute(
                "SELECT * FROM gallery_seed_jobs WHERE job_key = %s",
                (_PENDING_JOB_KEY,),
            )
            current = cursor.fetchone()
            if not current:
                raise RuntimeError("Pending gallery render was not persisted")
            return current


def _delete_pending_job() -> None:
    _ensure_pending_schema()
    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM gallery_seed_jobs WHERE job_key = %s",
                (_PENDING_JOB_KEY,),
            )


def _record_finalize_failure(message: str) -> dict[str, Any] | None:
    _ensure_pending_schema()
    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE gallery_seed_jobs
                SET finalize_failures = finalize_failures + 1,
                    last_error = %s,
                    updated_at = NOW()
                WHERE job_key = %s
                RETURNING *
                """,
                (message[:2000], _PENDING_JOB_KEY),
            )
            return cursor.fetchone()


def _pending_expired(job: dict[str, Any]) -> bool:
    created_at = job.get("created_at")
    if not isinstance(created_at, datetime):
        return False
    age = datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)
    return age.total_seconds() >= _MAX_PENDING_HOURS * 3600


async def _start_pending_job(app_module: Any, slot: int) -> dict[str, Any]:
    existing = await asyncio.to_thread(_pending_job)
    if existing:
        return {
            "ok": True,
            "phase": "rendering",
            "pending": True,
            "renderID": int(existing["render_id"]),
            "scoreID": int(existing["score_id"]),
            "slot": int(existing["slot"]),
            "startedAt": _iso(existing.get("created_at")),
        }

    run_date = datetime.now(timezone.utc).date()
    selection_key = f"resumable:{run_date.isoformat()}:{slot}:{secrets.token_hex(10)}"
    candidate = await app_module.find_seed_candidate(
        slot,
        run_date,
        selection_key=selection_key,
    )
    cached = candidate["cached"]
    render_id = await app_module.submit_ordr_bytes(
        candidate["replayBytes"],
        cached.player,
        candidate["replayHash"],
    )
    saved = await asyncio.to_thread(_save_pending_job, candidate, render_id, slot)
    return {
        "ok": True,
        "phase": "submitted",
        "pending": True,
        "renderID": int(saved["render_id"]),
        "scoreID": int(saved["score_id"]),
        "slot": int(saved["slot"]),
        "player": saved.get("player"),
        "startedAt": _iso(saved.get("created_at")),
    }


async def _finalize_pending_job(app_module: Any, job: dict[str, Any]) -> dict[str, Any]:
    render_id = int(job["render_id"])
    snapshot = await app_module.fetch_ordr_snapshot(render_id)
    if not snapshot.get("ready"):
        if _pending_expired(job):
            await asyncio.to_thread(_delete_pending_job)
            raise RuntimeError(
                f"o!rdr render {render_id} remained incomplete for more than {_MAX_PENDING_HOURS} hours"
            )
        return {
            "ok": True,
            "phase": "rendering",
            "pending": True,
            "renderID": render_id,
            "scoreID": int(job["score_id"]),
            "slot": int(job["slot"]),
            "progress": snapshot.get("progress"),
            "startedAt": _iso(job.get("created_at")),
        }

    replay_hash = str(job["replay_hash"])
    replay_bytes = bytes(job["replay_bytes"])
    cached = app_module.build_cached_replay_from_bytes(
        replay_bytes,
        replay_hash,
        keep_replay_bytes=True,
    )
    render_metadata = dict(snapshot.get("renderMetadata") or {})
    score = _json_object(job.get("score_payload"))
    user = _json_object(job.get("user_payload"))
    score_pp = app_module._finite_number(score.get("pp"))  # noqa: SLF001
    if score_pp is not None:
        render_metadata["scorePP"] = score_pp
        render_metadata["scoreMatchQuality"] = 1.0

    inference = await app_module.infer_cached_replay(
        cached,
        render_metadata,
        description=snapshot.get("description"),
    )
    metadata = inference["metadata"]
    beatmap_payload = score.get("beatmap") or {}
    if score.get("beatmapset"):
        beatmap_payload = dict(beatmap_payload)
        beatmap_payload["beatmapset"] = score.get("beatmapset")
    thumbnail_url = app_module._cover_url_from_beatmap_payload(beatmap_payload)  # noqa: SLF001
    if not thumbnail_url:
        osu_beatmap = await app_module.fetch_osu_beatmap(metadata.get("id"))
        thumbnail_url = app_module._cover_url_from_beatmap_payload(osu_beatmap)  # noqa: SLF001

    public_id = _database.make_public_id(replay_hash)
    record = {
        "public_id": public_id,
        "replay_hash": replay_hash,
        "render_id": render_id,
        "player": cached.player,
        "osu_user_id": user.get("id"),
        "avatar_url": user.get("avatarURL"),
        "country_code": user.get("countryCode"),
        "actual_rank": user.get("globalRank"),
        "predicted_rank": inference["predictedRank"],
        "skill": inference["skill"],
        "top_percent": inference["topPercent"],
        "confidence": inference["confidence"],
        "star": float(metadata["star"]),
        "accuracy_percent": inference["accuracyPercent"],
        "mods": ",".join(cached.display_mods or ["NM"]),
        "artist": metadata.get("artist") or (score.get("beatmapset") or {}).get("artist"),
        "title": metadata.get("title") or (score.get("beatmapset") or {}).get("title"),
        "version": metadata.get("version") or (score.get("beatmap") or {}).get("version"),
        "creator": metadata.get("creator") or (score.get("beatmapset") or {}).get("creator"),
        "length_seconds": float(metadata["lengthSeconds"]),
        "map_id": metadata.get("id") or (score.get("beatmap") or {}).get("id"),
        "map_link": metadata.get("url") or render_metadata.get("mapLink"),
        "video_url": app_module.validate_video_url(str(snapshot["videoURL"])),
        "thumbnail_url": thumbnail_url,
        "source": "cron",
        "published": True,
    }
    saved = await asyncio.to_thread(_database.save_submission, record)
    if not saved:
        raise RuntimeError("Database did not save finalized gallery replay")
    await asyncio.to_thread(_delete_pending_job)
    return {
        "ok": True,
        "phase": "finalized",
        "pending": False,
        "publicID": public_id,
        "renderID": render_id,
        "scoreID": int(job["score_id"]),
        "slot": int(job["slot"]),
        "player": cached.player,
        "actualRank": user.get("globalRank"),
        "predictedRank": inference["predictedRank"],
        "thumbnailURL": thumbnail_url,
        "eligible": await asyncio.to_thread(_database.challenge_count),
    }


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

            global _SEED_LOCK
            if _SEED_LOCK is None:
                _SEED_LOCK = asyncio.Lock()

            try:
                async with _SEED_LOCK:
                    pending = await asyncio.to_thread(_pending_job)
                    if pending:
                        result = await _finalize_pending_job(app_module, pending)
                        daily = await asyncio.to_thread(_daily_status)
                        result.update({"schedule": "random-20-120m", "daily": daily})
                        print(
                            json.dumps(
                                {"event": "cron_tick_complete", **result},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            flush=True,
                        )
                        return JSONResponse(result)

                    daily_before = await asyncio.to_thread(_daily_status)
                    claim = await asyncio.to_thread(_claim_window)
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

                    slot = secrets.randbelow(3)
                    result = await _start_pending_job(app_module, slot)
                    result.update(
                        {
                            "schedule": "random-20-120m",
                            "delayMinutes": claim["delayMinutes"],
                            "nextRunAt": _iso(claim.get("nextRunAt")),
                            "daily": daily_before,
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
            except Exception as exc:
                failed = await asyncio.to_thread(_record_finalize_failure, str(exc))
                if failed and (
                    int(failed.get("finalize_failures") or 0) >= _MAX_FINALIZE_FAILURES
                    or _pending_expired(failed)
                ):
                    await asyncio.to_thread(_delete_pending_job)
                await asyncio.to_thread(_retry_current_job)
                print(
                    json.dumps(
                        {"event": "cron_tick_failed", "error": repr(exc)},
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                raise HTTPException(
                    status_code=502,
                    detail={"code": "gallery_seed_failed", "message": str(exc)},
                ) from exc

    FastAPI.__init__ = patched_init
    FastAPI._rankguess_cron_runtime_patch = True


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _backend_database._daily_ids_for_date = _daily_ids_for_date  # noqa: SLF001
    _community._claim_seed_window = _claim_seed_window  # noqa: SLF001
    _community._schedule_retry = _schedule_retry  # noqa: SLF001
    _install_fastapi_route()
    _INSTALLED = True
