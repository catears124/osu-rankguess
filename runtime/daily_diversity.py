from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from typing import Any

import httpx

from runtime import daily_fresh as _daily_fresh

_INSTALLED = False

# The global leaderboard is reliable for elite and established players. Its deep
# pages bunch near the public leaderboard boundary, so the broad slot samples
# country leaderboards and filters by the player's actual global rank instead.
_FIXED_WINDOWS = (
    ("elite", 1, 2_500, 1, 55),
    ("established", 5_000, 20_000, 90, 400),
)
_WIDE_WINDOWS = (
    ("wide-25k-75k", 25_000, 75_000),
    ("wide-75k-200k", 75_000, 200_000),
    ("wide-200k-500k", 200_000, 500_000),
)
_COUNTRIES = (
    "AR", "AT", "AU", "BE", "BG", "BR", "CA", "CH", "CL", "CO", "CZ", "DE",
    "DK", "EE", "ES", "FI", "FR", "GB", "GR", "HR", "HU", "ID", "IE", "IL",
    "IT", "LT", "LV", "MY", "MX", "NL", "NO", "NZ", "PE", "PH", "PL", "PT",
    "RO", "RS", "SE", "SG", "SI", "SK", "TH", "TR", "TW", "UA", "US", "VN",
    "ZA",
)
_DEEP_COUNTRIES = (
    "AR", "AT", "BE", "BG", "CH", "CL", "CO", "CZ", "DK", "EE", "FI", "GR",
    "HR", "HU", "ID", "IE", "IL", "LT", "LV", "MY", "MX", "NO", "NZ", "PE",
    "PH", "PT", "RO", "RS", "SG", "SI", "SK", "TH", "TR", "VN", "ZA",
)


def _rank_window(challenge_date: date, band: int) -> tuple[str, int, int]:
    if band < len(_FIXED_WINDOWS):
        name, minimum, maximum, _, _ = _FIXED_WINDOWS[band]
        return name, minimum, maximum

    digest = hashlib.sha256(
        f"daily-wide-window:{challenge_date.isoformat()}".encode("utf-8")
    ).digest()
    return _WIDE_WINDOWS[
        int.from_bytes(digest[:4], "big") % len(_WIDE_WINDOWS)
    ]


def _ranking_global_rank(row: dict[str, Any]) -> int | None:
    user = row.get("user") or {}
    statistics = row.get("statistics") or user.get("statistics") or {}
    value = row.get("global_rank") or statistics.get("global_rank")
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return None
    return rank if rank > 0 else None


def _ordered_countries(
    selection_key: str,
    countries: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            countries,
            key=lambda country: hashlib.sha256(
                f"daily-country:{selection_key}:{country}".encode("utf-8")
            ).digest(),
        )
    )


async def _fetch_country_rankings_page(
    app_module: Any,
    page: int,
    country: str,
) -> list[dict[str, Any]]:
    token = await app_module.get_osu_access_token()
    if not token:
        raise RuntimeError("osu! API credentials are not configured")

    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            f"{app_module.OSU_API_BASE}/rankings/osu/performance",
            params={"page": int(page), "country": country},
            headers=app_module._osu_headers(token),  # noqa: SLF001
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"osu! {country} rankings returned HTTP {response.status_code}: "
            f"{response.text[:240]}"
        )

    payload = response.json()
    return list(payload.get("ranking") or [])


async def _candidate_from_rankings(
    app_module: Any,
    selection_key: str,
    *,
    rank_min: int,
    rank_max: int,
    page_min: int,
    page_max: int,
    countries: tuple[str, ...] = (),
    page_attempts: int = 10,
) -> dict[str, Any]:
    country_order = _ordered_countries(selection_key, countries) if countries else ()

    for page_attempt in range(max(1, page_attempts)):
        page = app_module._stable_number(  # noqa: SLF001
            f"daily-page:{selection_key}:{page_attempt}",
            page_min,
            page_max,
        )
        country = (
            country_order[page_attempt % len(country_order)]
            if country_order
            else None
        )

        try:
            ranking = (
                await _fetch_country_rankings_page(app_module, page, country)
                if country
                else await app_module.fetch_rankings_page(page)
            )
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "daily_rankings_failed",
                        "selectionKey": selection_key,
                        "page": page,
                        "country": country,
                        "error": repr(exc),
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
            continue

        ranking = [
            row
            for row in ranking
            if (rank := _ranking_global_rank(row)) is not None
            and rank_min <= rank <= rank_max
        ]
        ordered_users = sorted(
            ranking,
            key=lambda row: hashlib.sha256(
                (
                    f"daily-candidate-user:{selection_key}:{page_attempt}:"
                    f"{(row.get('user') or {}).get('id')}"
                ).encode("utf-8")
            ).digest(),
        )

        for rank_row in ordered_users[:12]:
            user = rank_row.get("user") or {}
            try:
                user_id = int(user.get("id"))
            except (TypeError, ValueError):
                continue
            username = str(user.get("username") or "").strip()
            actual_rank = _ranking_global_rank(rank_row)
            if not username or actual_rank is None:
                continue

            try:
                scores = await app_module.fetch_user_best_scores(user_id)
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "event": "daily_scores_failed",
                            "userID": user_id,
                            "rank": actual_rank,
                            "error": repr(exc),
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                continue

            good_scores = [
                score
                for score in scores
                if app_module._seed_score_is_good(score)  # noqa: SLF001
            ]
            good_scores.sort(
                key=lambda score: hashlib.sha256(
                    (
                        f"daily-candidate-score:{selection_key}:"
                        f"{app_module._score_id(score)}"  # noqa: SLF001
                    ).encode("utf-8")
                ).digest()
            )

            for score in good_scores[:12]:
                score_id = app_module._score_id(score)  # noqa: SLF001
                if score_id is None:
                    continue
                try:
                    replay_bytes = await app_module.download_score_replay(score_id)
                    replay_hash = app_module.compute_replay_hash(replay_bytes)
                    if await asyncio.to_thread(
                        app_module.submission_exists,
                        replay_hash=replay_hash,
                    ):
                        continue
                    cached = app_module.build_cached_replay_from_bytes(
                        replay_bytes,
                        replay_hash,
                        keep_replay_bytes=True,
                    )
                except Exception as exc:
                    print(
                        json.dumps(
                            {
                                "event": "daily_replay_rejected",
                                "scoreID": score_id,
                                "rank": actual_rank,
                                "error": repr(exc),
                            },
                            separators=(",", ":"),
                        ),
                        flush=True,
                    )
                    continue

                return {
                    "score": score,
                    "scoreID": score_id,
                    "cached": cached,
                    "replayBytes": replay_bytes,
                    "replayHash": replay_hash,
                    "rankingSource": f"country:{country}" if country else "global",
                    "user": {
                        "id": user_id,
                        "username": username,
                        "avatarURL": user.get("avatar_url"),
                        "countryCode": user.get("country_code"),
                        "globalRank": actual_rank,
                    },
                }

    source = "country rankings" if countries else "global rankings"
    raise RuntimeError(
        f"Could not find a fresh replay in rank range "
        f"#{rank_min:,}-#{rank_max:,} from {source}"
    )


async def _find_daily_candidate(
    app_module: Any,
    band: int,
    challenge_date: date,
    selection_key: str,
) -> dict[str, Any]:
    band_name, rank_min, rank_max = _rank_window(challenge_date, band)

    if band < len(_FIXED_WINDOWS):
        _, _, _, page_min, page_max = _FIXED_WINDOWS[band]
        strategies = (
            {
                "rank_min": rank_min,
                "rank_max": rank_max,
                "page_min": page_min,
                "page_max": page_max,
                "page_attempts": 10,
            },
        )
    else:
        preferred_countries = (
            _DEEP_COUNTRIES if rank_min >= 75_000 else _COUNTRIES
        )
        strategies = (
            {
                "rank_min": rank_min,
                "rank_max": rank_max,
                "page_min": 1,
                "page_max": 45,
                "countries": preferred_countries,
                "page_attempts": 16,
            },
            {
                "rank_min": 25_000,
                "rank_max": 500_000,
                "page_min": 1,
                "page_max": 55,
                "countries": _COUNTRIES,
                "page_attempts": 16,
            },
            {
                "rank_min": 20_001,
                "rank_max": 200_000,
                "page_min": 1,
                "page_max": 65,
                "countries": _COUNTRIES,
                "page_attempts": 12,
            },
        )

    failures: list[str] = []
    for strategy_index, strategy in enumerate(strategies):
        try:
            candidate = await _candidate_from_rankings(
                app_module,
                f"{selection_key}:strategy-{strategy_index}",
                **strategy,
            )
            candidate["user"].update(
                {
                    "dailyRankBand": band_name,
                    "dailyTargetRankMin": rank_min,
                    "dailyTargetRankMax": rank_max,
                    "dailyRankingSource": candidate.get("rankingSource"),
                }
            )
            return candidate
        except Exception as exc:
            failures.append(str(exc))
            print(
                json.dumps(
                    {
                        "event": "daily_rank_strategy_exhausted",
                        "band": band_name,
                        "targetRankMin": rank_min,
                        "targetRankMax": rank_max,
                        "strategy": strategy_index,
                        "error": repr(exc),
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )

    # The diversity bands are soft constraints. Daily availability still wins if
    # the API cannot produce a downloadable fresh replay in the target windows.
    fallback = await app_module.find_seed_candidate(
        band,
        challenge_date,
        selection_key=f"{selection_key}:availability-fallback",
    )
    fallback["user"].update(
        {
            "dailyRankBand": f"{band_name}-fallback",
            "dailyTargetRankMin": rank_min,
            "dailyTargetRankMax": rank_max,
            "dailySelectionFailures": failures[-3:],
            "dailyRankingSource": "global:fallback",
        }
    )
    fallback["rankingSource"] = "global:fallback"
    return fallback


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _daily_fresh._find_daily_candidate = _find_daily_candidate  # noqa: SLF001
    _INSTALLED = True
