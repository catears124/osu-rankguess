from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any

from backend import database as _database
from runtime import cron as _cron

_INSTALLED = False
_DAILY_COUNT = 3
_ORIGINAL_CLAIM_WINDOW = _cron._claim_window  # noqa: SLF001
_ORIGINAL_START_PENDING_JOB = _cron._start_pending_job  # noqa: SLF001
_ORIGINAL_FINALIZE_PENDING_JOB = _cron._finalize_pending_job  # noqa: SLF001


def _daily_ids(challenge_date: date) -> list[str]:
    _database.ensure_schema()
    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT replay_ids FROM daily_challenges WHERE challenge_date = %s",
                (challenge_date,),
            )
            row = cursor.fetchone()
    return _cron._daily_replay_ids(row["replay_ids"]) if row else []  # noqa: SLF001


def _daily_ids_for_date(challenge_date: date, count: int) -> list[str]:
    """Return only explicitly prepared daily replays; never sample the gallery."""
    return _daily_ids(challenge_date)[: max(0, int(count))]


def _daily_has_guesses(challenge_date: date) -> bool:
    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM challenge_guesses
                WHERE mode = 'daily' AND challenge_key = %s
                LIMIT 1
                """,
                (challenge_date.isoformat(),),
            )
            return cursor.fetchone() is not None


def _contains_published_replay(replay_ids: list[str]) -> bool:
    if not replay_ids:
        return False
    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM replay_submissions
                WHERE public_id = ANY(%s) AND published = TRUE
                LIMIT 1
                """,
                (replay_ids,),
            )
            return cursor.fetchone() is not None


def _reset_future_gallery_row(challenge_date: date, replay_ids: list[str]) -> list[str]:
    today = datetime.now(timezone.utc).date()
    if challenge_date <= today or not replay_ids or _daily_has_guesses(challenge_date):
        return replay_ids
    if not _contains_published_replay(replay_ids):
        return replay_ids
    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM daily_challenges WHERE challenge_date = %s",
                (challenge_date,),
            )
    return []


def _next_daily_target() -> tuple[date, int] | None:
    today = datetime.now(timezone.utc).date()
    for challenge_date in (today, today + timedelta(days=1)):
        replay_ids = _reset_future_gallery_row(challenge_date, _daily_ids(challenge_date))
        if len(replay_ids) < _DAILY_COUNT:
            return challenge_date, len(replay_ids)
    return None


def _daily_status() -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    today_ids = _daily_ids(today)
    tomorrow = today + timedelta(days=1)
    tomorrow_ids = _daily_ids(tomorrow)
    return {
        "date": today.isoformat(),
        "ready": len(today_ids) == _DAILY_COUNT,
        "replayCount": len(today_ids),
        "nextDate": tomorrow.isoformat(),
        "nextReady": len(tomorrow_ids) == _DAILY_COUNT,
        "nextReplayCount": len(tomorrow_ids),
        "eligibleReplays": _database.challenge_count(),
    }


def _claim_window() -> dict[str, Any]:
    target = _next_daily_target()
    if target is None:
        return _ORIGINAL_CLAIM_WINDOW()
    challenge_date, slot = target
    return {
        "claimed": True,
        "delayMinutes": 0,
        "nextRunAt": None,
        "lastRunAt": None,
        "dailyTarget": challenge_date.isoformat(),
        "dailySlot": slot,
    }


def _pending_job() -> dict[str, Any] | None:
    _cron._ensure_pending_schema()  # noqa: SLF001
    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM gallery_seed_jobs ORDER BY created_at ASC LIMIT 1"
            )
            return cursor.fetchone()


def _save_pending_job(
    candidate: dict[str, Any],
    render_id: int,
    slot: int,
    job_key: str = "gallery",
) -> dict[str, Any]:
    _cron._ensure_pending_schema()  # noqa: SLF001
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
                    job_key,
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
                (job_key,),
            )
            current = cursor.fetchone()
            if not current:
                raise RuntimeError("Pending replay render was not persisted")
            return current


def _delete_pending_job(job_key: str | None = None) -> None:
    _cron._ensure_pending_schema()  # noqa: SLF001
    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            if job_key:
                cursor.execute("DELETE FROM gallery_seed_jobs WHERE job_key = %s", (job_key,))
            else:
                cursor.execute(
                    """
                    DELETE FROM gallery_seed_jobs
                    WHERE job_key = (
                        SELECT job_key FROM gallery_seed_jobs
                        ORDER BY created_at ASC LIMIT 1
                    )
                    """
                )


def _record_finalize_failure(message: str) -> dict[str, Any] | None:
    _cron._ensure_pending_schema()  # noqa: SLF001
    with _database._connect() as connection:  # noqa: SLF001
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE gallery_seed_jobs
                SET finalize_failures = finalize_failures + 1,
                    last_error = %s,
                    updated_at = NOW()
                WHERE job_key = (
                    SELECT job_key FROM gallery_seed_jobs
                    ORDER BY created_at ASC LIMIT 1
                )
                RETURNING *
                """,
                (message[:2000],),
            )
            return cursor.fetchone()


def _daily_job_target(job: dict[str, Any]) -> tuple[date, int] | None:
    parts = str(job.get("job_key") or "").split(":")
    if len(parts) != 3 or parts[0] != "daily":
        return None
    try:
        return date.fromisoformat(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return None


def _band_for_slot(challenge_date: date, slot: int) -> int:
    """Cover top, middle, and lower ranks without making round order predictable."""
    order = sorted(
        range(_DAILY_COUNT),
        key=lambda index: hashlib.sha256(
            f"daily-band:{challenge_date.isoformat()}:{index}".encode("utf-8")
        ).digest(),
    )
    return order[slot % _DAILY_COUNT]


async def _start_pending_job(app_module: Any, slot: int) -> dict[str, Any]:
    existing = await asyncio.to_thread(_pending_job)
    if existing:
        target = _daily_job_target(existing)
        result = {
            "ok": True,
            "phase": "rendering",
            "pending": True,
            "renderID": int(existing["render_id"]),
            "scoreID": int(existing["score_id"]),
            "slot": int(existing["slot"]),
            "startedAt": _cron._iso(existing.get("created_at")),  # noqa: SLF001
        }
        if target:
            result.update(
                {
                    "purpose": "daily",
                    "challengeDate": target[0].isoformat(),
                    "dailySlot": target[1],
                }
            )
        return result

    target = await asyncio.to_thread(_next_daily_target)
    if target is None:
        return await _ORIGINAL_START_PENDING_JOB(app_module, slot)

    challenge_date, daily_slot = target
    band = _band_for_slot(challenge_date, daily_slot)
    selection_key = (
        f"daily:{challenge_date.isoformat()}:{daily_slot}:"
        f"band-{band}:{secrets.token_hex(10)}"
    )
    candidate = await app_module.find_seed_candidate(
        band,
        challenge_date,
        selection_key=selection_key,
    )
    cached = candidate["cached"]
    render_id = await app_module.submit_ordr_bytes(
        candidate["replayBytes"],
        cached.player,
        candidate["replayHash"],
    )
    job_key = f"daily:{challenge_date.isoformat()}:{daily_slot}"
    saved = await asyncio.to_thread(
        _save_pending_job,
        candidate,
        render_id,
        daily_slot,
        job_key,
    )
    return {
        "ok": True,
        "phase": "submitted",
        "pending": True,
        "purpose": "daily",
        "challengeDate": challenge_date.isoformat(),
        "dailySlot": daily_slot,
        "rankBand": band,
        "renderID": int(saved["render_id"]),
        "scoreID": int(saved["score_id"]),
        "slot": int(saved["slot"]),
        "player": saved.get("player"),
        "startedAt": _cron._iso(saved.get("created_at")),  # noqa: SLF001
    }


def _append_daily_replay(challenge_date: date, slot: int, public_id: str) -> list[str]:
    _database.ensure_schema()
    with _database._connect() as connection:  # noqa: SLF001
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT replay_ids FROM daily_challenges WHERE challenge_date = %s FOR UPDATE",
                    (challenge_date,),
                )
                row = cursor.fetchone()
                replay_ids = _cron._daily_replay_ids(row["replay_ids"]) if row else []  # noqa: SLF001
                if public_id in replay_ids:
                    return replay_ids
                if len(replay_ids) != slot:
                    raise RuntimeError(
                        f"Daily slot changed while render was pending: expected {slot}, found {len(replay_ids)}"
                    )
                replay_ids.append(public_id)
                if row:
                    cursor.execute(
                        "UPDATE daily_challenges SET replay_ids = %s::jsonb WHERE challenge_date = %s",
                        (json.dumps(replay_ids), challenge_date),
                    )
                else:
                    cursor.execute(
                        "INSERT INTO daily_challenges (challenge_date, replay_ids) VALUES (%s, %s::jsonb)",
                        (challenge_date, json.dumps(replay_ids)),
                    )
    return replay_ids


async def _finalize_daily_job(
    app_module: Any,
    job: dict[str, Any],
    challenge_date: date,
    daily_slot: int,
) -> dict[str, Any]:
    job_key = str(job["job_key"])
    render_id = int(job["render_id"])
    snapshot = await app_module.fetch_ordr_snapshot(render_id)
    if not snapshot.get("ready"):
        if _cron._pending_expired(job):  # noqa: SLF001
            await asyncio.to_thread(_delete_pending_job, job_key)
            raise RuntimeError(
                f"o!rdr render {render_id} remained incomplete for more than "
                f"{_cron._MAX_PENDING_HOURS} hours"  # noqa: SLF001
            )
        return {
            "ok": True,
            "phase": "rendering",
            "pending": True,
            "purpose": "daily",
            "challengeDate": challenge_date.isoformat(),
            "dailySlot": daily_slot,
            "renderID": render_id,
            "scoreID": int(job["score_id"]),
            "slot": int(job["slot"]),
            "progress": snapshot.get("progress"),
            "startedAt": _cron._iso(job.get("created_at")),  # noqa: SLF001
        }

    replay_hash = str(job["replay_hash"])
    cached = app_module.build_cached_replay_from_bytes(
        bytes(job["replay_bytes"]),
        replay_hash,
        keep_replay_bytes=True,
    )
    render_metadata = dict(snapshot.get("renderMetadata") or {})
    score = _cron._json_object(job.get("score_payload"))  # noqa: SLF001
    user = _cron._json_object(job.get("user_payload"))  # noqa: SLF001
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
    actual_rank = user.get("globalRank")
    if not actual_rank:
        raise RuntimeError("Selected daily player no longer has a global rank")

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
        "actual_rank": int(actual_rank),
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
        "source": "daily",
        "published": False,
    }
    saved = await asyncio.to_thread(_database.save_submission, record)
    if not saved:
        raise RuntimeError("Database did not save finalized daily replay")
    replay_ids = await asyncio.to_thread(
        _append_daily_replay,
        challenge_date,
        daily_slot,
        public_id,
    )
    await asyncio.to_thread(_delete_pending_job, job_key)
    return {
        "ok": True,
        "phase": "finalized",
        "pending": False,
        "purpose": "daily",
        "challengeDate": challenge_date.isoformat(),
        "dailySlot": daily_slot,
        "dailyReplayCount": len(replay_ids),
        "dailyReady": len(replay_ids) == _DAILY_COUNT,
        "publicID": public_id,
        "renderID": render_id,
        "scoreID": int(job["score_id"]),
        "slot": int(job["slot"]),
        "player": cached.player,
        "actualRank": int(actual_rank),
        "predictedRank": inference["predictedRank"],
        "thumbnailURL": thumbnail_url,
    }


async def _finalize_pending_job(app_module: Any, job: dict[str, Any]) -> dict[str, Any]:
    target = _daily_job_target(job)
    if target is None:
        return await _ORIGINAL_FINALIZE_PENDING_JOB(app_module, job)
    return await _finalize_daily_job(app_module, job, target[0], target[1])


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _database._daily_ids_for_date = _daily_ids_for_date  # noqa: SLF001
    _cron._daily_ids_for_date = _daily_ids_for_date  # noqa: SLF001
    _cron._daily_status = _daily_status  # noqa: SLF001
    _cron._claim_window = _claim_window  # noqa: SLF001
    _cron._pending_job = _pending_job  # noqa: SLF001
    _cron._save_pending_job = _save_pending_job  # noqa: SLF001
    _cron._delete_pending_job = _delete_pending_job  # noqa: SLF001
    _cron._record_finalize_failure = _record_finalize_failure  # noqa: SLF001
    _cron._start_pending_job = _start_pending_job  # noqa: SLF001
    _cron._finalize_pending_job = _finalize_pending_job  # noqa: SLF001
    _INSTALLED = True
