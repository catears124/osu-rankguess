"""Recover duplicate o!rdr jobs and expose useful queue progress."""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

ORDR_RENDER_URL = "https://apis.issou.best/ordr/renders"
_DUPLICATE_MARKERS = (
    "already rendering",
    "already in queue",
    "rendering or in queue",
    "already rendered",
)
_RENDER_ID_KEYS = {"renderid", "render_id", "render-id"}
_RENDER_ID_PATTERN = re.compile(r"(?:render(?:\s*id)?|#)\D{0,12}(\d{3,})", re.IGNORECASE)
_PERCENT_PATTERN = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%")
_QUEUE_PATTERN = re.compile(r"(?:queue|position|place)\D{0,12}(\d+)", re.IGNORECASE)
_MOD_BITS = {
    "NF": 1,
    "EZ": 2,
    "TD": 4,
    "HD": 8,
    "HR": 16,
    "SD": 32,
    "DT": 64,
    "RX": 128,
    "HT": 256,
    "NC": 512,
    "FL": 1024,
    "AT": 2048,
    "SO": 4096,
    "AP": 8192,
    "PF": 16384,
}
_QUEUE_CACHE: dict[int, tuple[float, int | None, int | None]] = {}


@dataclass(frozen=True)
class ReplayIdentity:
    filename: str = ""
    hash_prefix: str = ""
    username: str = ""
    beatmap_hash: str = ""
    accuracy_percent: float | None = None
    score: int | None = None
    mods: tuple[str, ...] = ()


RawGet = Callable[..., Awaitable[httpx.Response]]


def _payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return {"message": response.text[:1000]}


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {_flatten_text(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten_text(item) for item in value)
    return str(value or "")


def _render_id(value: Any) -> int | None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized in _RENDER_ID_KEYS or ("render" in normalized and "id" in normalized):
                try:
                    candidate = int(item)
                except (TypeError, ValueError):
                    candidate = 0
                if candidate > 0:
                    return candidate
        for item in value.values():
            candidate = _render_id(item)
            if candidate:
                return candidate
    elif isinstance(value, (list, tuple)):
        for item in value:
            candidate = _render_id(item)
            if candidate:
                return candidate
    elif isinstance(value, str):
        match = _RENDER_ID_PATTERN.search(value)
        if match:
            return int(match.group(1))
    return None


def _normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _mods_from_mask(mask: int) -> tuple[str, ...]:
    enabled = {name for name, bit in _MOD_BITS.items() if mask & bit}
    if "NC" in enabled:
        enabled.discard("DT")
    if "PF" in enabled:
        enabled.discard("SD")
    return tuple(sorted(enabled))


def _replay_accuracy(header: dict[str, Any]) -> float | None:
    try:
        c300 = int(header.get("count_300") or 0)
        c100 = int(header.get("count_100") or 0)
        c50 = int(header.get("count_50") or 0)
        miss = int(header.get("count_miss") or 0)
    except (TypeError, ValueError):
        return None
    total = c300 + c100 + c50 + miss
    if total <= 0:
        return None
    return 100.0 * (300 * c300 + 100 * c100 + 50 * c50) / (300 * total)


def _upload_identity(kwargs: dict[str, Any]) -> ReplayIdentity:
    files = kwargs.get("files") or {}
    replay = files.get("replayFile") if isinstance(files, dict) else None
    filename = str(replay[0]) if isinstance(replay, (tuple, list)) and replay else ""
    replay_bytes = replay[1] if isinstance(replay, (tuple, list)) and len(replay) > 1 else None
    hash_match = re.search(r"-([0-9a-f]{12})\.osr$", filename, re.IGNORECASE)
    hash_prefix = hash_match.group(1).lower() if hash_match else ""
    username = re.sub(r"-[0-9a-f]{12}\.osr$", "", filename, flags=re.IGNORECASE).replace("_", " ").strip()
    identity = ReplayIdentity(filename=filename, hash_prefix=hash_prefix, username=username)

    if not isinstance(replay_bytes, (bytes, bytearray)):
        return identity
    try:
        from replay_features import parse_osr

        parsed = parse_osr(bytes(replay_bytes))
        header = dict(parsed.get("header") or {})
        parsed_username = str(header.get("player_name") or "").strip()
        beatmap_hash = str(header.get("beatmap_hash") or "").strip().lower()
        try:
            score = int(header.get("score"))
        except (TypeError, ValueError):
            score = None
        try:
            mods_mask = int(header.get("mods_mask") or 0)
        except (TypeError, ValueError):
            mods_mask = 0
        return ReplayIdentity(
            filename=filename,
            hash_prefix=hash_prefix,
            username=parsed_username or username,
            beatmap_hash=beatmap_hash,
            accuracy_percent=_replay_accuracy(header),
            score=score if score and score > 0 else None,
            mods=_mods_from_mask(mods_mask),
        )
    except Exception:
        return identity


def _render_values(render: dict[str, Any]) -> tuple[str, str, tuple[str, ...], list[float], int | None]:
    text = _flatten_text(render)
    nested_replay = render.get("replay") if isinstance(render.get("replay"), dict) else {}
    username = str(
        render.get("replayUsername")
        or render.get("replay_username")
        or render.get("username")
        or nested_replay.get("username")
        or ""
    )
    raw_mods = render.get("replayMods") or render.get("mods") or nested_replay.get("mods") or ""
    if isinstance(raw_mods, list):
        mods = tuple(
            sorted(
                str(item.get("acronym") if isinstance(item, dict) else item).upper()
                for item in raw_mods
                if item
            )
        )
    else:
        token = re.sub(r"[^A-Z0-9]", "", str(raw_mods).upper())
        mods = tuple(sorted({name for name in _MOD_BITS if name in token}))
    percentages: list[float] = []
    for match in _PERCENT_PATTERN.finditer(text):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if 0 <= value <= 100:
            percentages.append(value)
    score = None
    for key in ("score", "replayScore", "replay_score"):
        try:
            candidate = int(render.get(key))
        except (TypeError, ValueError):
            continue
        if candidate > 0:
            score = candidate
            break
    return text, username, mods, percentages, score


def _match_score(render: dict[str, Any], identity: ReplayIdentity) -> tuple[float, bool]:
    text, username, mods, percentages, score = _render_values(render)
    folded = text.casefold()
    normalized_text = _normal(text)
    points = 0.0

    if identity.hash_prefix and identity.hash_prefix in folded:
        points += 120.0
    if identity.filename and identity.filename.casefold() in folded:
        points += 120.0
    if identity.beatmap_hash and identity.beatmap_hash in folded:
        points += 90.0

    username_match = bool(identity.username and _normal(identity.username) == _normal(username))
    if username_match:
        points += 30.0
    elif identity.username and _normal(identity.username) in normalized_text:
        username_match = True
        points += 24.0

    replay_specific = False
    if identity.accuracy_percent is not None and percentages:
        distance = min(abs(value - identity.accuracy_percent) for value in percentages)
        if distance <= 0.015:
            points += 24.0
            replay_specific = True
        elif distance <= 0.10:
            points += 13.0
            replay_specific = True
    if identity.score is not None and score == identity.score:
        points += 28.0
        replay_specific = True
    if identity.mods and mods:
        if set(identity.mods) == set(mods):
            points += 10.0
            replay_specific = True
        elif set(identity.mods).issubset(set(mods)) or set(mods).issubset(set(identity.mods)):
            points += 4.0

    if username_match and replay_specific:
        points += 20.0
    return points, username_match


def _render_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        values = payload.get("renders") or payload.get("items") or payload.get("data") or []
    else:
        values = payload
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


async def _raw_get_json(
    raw_get: RawGet,
    client: httpx.AsyncClient,
    params: dict[str, Any],
) -> Any:
    try:
        response = await raw_get(
            client,
            ORDR_RENDER_URL,
            params=params,
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    return _payload(response)


async def _find_recent_render(
    raw_get: RawGet,
    client: httpx.AsyncClient,
    identity: ReplayIdentity,
    *,
    deep: bool = False,
) -> int | None:
    searches: list[dict[str, Any]] = []
    if identity.hash_prefix:
        searches.append({"search": identity.hash_prefix, "pageSize": 100})
    if identity.username:
        searches.extend(
            (
                {"search": identity.username, "pageSize": 100},
                {"username": identity.username, "pageSize": 100},
                {"replayUsername": identity.username, "pageSize": 100},
            )
        )
    page_count = 12 if deep else 4
    searches.extend({"pageSize": 100, "page": page} for page in range(1, page_count + 1))

    candidates: dict[int, tuple[float, bool]] = {}
    for params in searches:
        payload = await _raw_get_json(raw_get, client, params)
        for render in _render_list(payload):
            render_id = _render_id(render)
            if not render_id:
                continue
            score, username_match = _match_score(render, identity)
            previous = candidates.get(render_id)
            if previous is None or score > previous[0]:
                candidates[render_id] = (score, username_match)
        if candidates and max(score for score, _ in candidates.values()) >= 80.0:
            break

    if not candidates:
        return None

    ranked = sorted(
        ((score, username_match, render_id) for render_id, (score, username_match) in candidates.items()),
        reverse=True,
    )
    best_score, best_username_match, best_id = ranked[0]
    if best_score >= 45.0:
        return best_id

    # Queued items frequently expose only the username. A duplicate response proves
    # that the job exists, so choose the newest exact-username render as a fallback.
    username_candidates = [render_id for score, matched, render_id in ranked if matched and score >= 24.0]
    if best_username_match and username_candidates:
        return max(username_candidates)
    return None


async def _recover_duplicate_render(
    raw_get: RawGet,
    client: httpx.AsyncClient,
    kwargs: dict[str, Any],
    payload: Any,
) -> int | None:
    render_id = _render_id(payload)
    if render_id:
        return render_id

    identity = _upload_identity(kwargs)
    deadline = time.monotonic() + 90.0
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        render_id = await _find_recent_render(
            raw_get,
            client,
            identity,
            deep=attempt >= 3,
        )
        if render_id:
            return render_id
        await asyncio.sleep(min(5.0, 1.5 + attempt * 0.5))
    return None


def _integer(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _queue_position(value: Any) -> int | None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if "queue" in normalized and any(token in normalized for token in ("position", "place", "index", "number")):
                candidate = _integer(item)
                if candidate is not None:
                    return candidate + 1 if "index" in normalized and candidate == 0 else candidate
        for item in value.values():
            candidate = _queue_position(item)
            if candidate is not None:
                return candidate
    elif isinstance(value, (list, tuple)):
        for item in value:
            candidate = _queue_position(item)
            if candidate is not None:
                return candidate
    elif isinstance(value, str):
        match = _QUEUE_PATTERN.search(value)
        if match:
            return int(match.group(1))
    return None


def _is_pending(render: dict[str, Any]) -> bool:
    try:
        if int(render.get("errorCode") or 0) != 0:
            return False
    except (TypeError, ValueError):
        pass
    text = str(render.get("progress") or "").casefold()
    if any(token in text for token in ("done", "complete", "finished", "error", "failed")):
        return False
    if any(render.get(key) for key in ("videoUrl", "videoURL", "url")):
        return False
    return True


async def _estimate_queue_position(
    raw_get: RawGet,
    client: httpx.AsyncClient,
    render_id: int,
) -> tuple[int | None, int | None]:
    cached = _QUEUE_CACHE.get(render_id)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1], cached[2]

    pending_ids: set[int] = set()
    for page in range(1, 7):
        payload = await _raw_get_json(raw_get, client, {"pageSize": 100, "page": page})
        renders = _render_list(payload)
        if not renders:
            break
        for render in renders:
            candidate = _render_id(render)
            if candidate and _is_pending(render):
                pending_ids.add(candidate)
        if render_id in pending_ids and len(renders) < 100:
            break

    if render_id not in pending_ids:
        result = (None, len(pending_ids) or None)
    else:
        ordered = sorted(pending_ids)
        result = (ordered.index(render_id) + 1, len(ordered))
    _QUEUE_CACHE[render_id] = (now + 5.0, result[0], result[1])
    return result


def _clean_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.casefold() not in {"content-length", "content-encoding", "transfer-encoding"}
    }


def _json_response(response: httpx.Response, payload: Any, *, status_code: int | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code or response.status_code,
        json=payload,
        request=response.request,
        headers=_clean_headers(response),
    )


def install() -> None:
    if getattr(httpx.AsyncClient, "_rankguess_ordr_recovery", False):
        return

    original_post = httpx.AsyncClient.post
    original_get = httpx.AsyncClient.get

    async def recovered_post(self: httpx.AsyncClient, url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
        response = await original_post(self, url, *args, **kwargs)
        if str(url).rstrip("/") != ORDR_RENDER_URL or response.status_code == 201:
            return response

        payload = _payload(response)
        text = _flatten_text(payload).casefold()
        if not any(marker in text for marker in _DUPLICATE_MARKERS):
            return response

        render_id = await _recover_duplicate_render(original_get, self, kwargs, payload)
        if not render_id:
            return response

        return _json_response(
            response,
            {"renderID": render_id, "reused": True},
            status_code=201,
        )

    async def recovered_get(self: httpx.AsyncClient, url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
        response = await original_get(self, url, *args, **kwargs)
        if str(url).rstrip("/") != ORDR_RENDER_URL or response.status_code != 200:
            return response

        params = kwargs.get("params") or {}
        try:
            render_id = int(params.get("renderID") or params.get("render_id") or 0)
        except (AttributeError, TypeError, ValueError):
            render_id = 0
        if render_id <= 0:
            return response

        payload = _payload(response)
        renders = _render_list(payload)
        if not renders:
            return response
        render = renders[0]
        progress = str(render.get("progress") or "Queued")
        if not _is_pending(render):
            return response

        position = _queue_position(render) or _queue_position(payload)
        queue_size = None
        if position is None and "queue" in progress.casefold():
            position, queue_size = await _estimate_queue_position(original_get, self, render_id)
        if position is None:
            return response

        render["queuePosition"] = position
        if queue_size:
            render["queueSize"] = queue_size
            render["progress"] = f"Queued · #{position} of {queue_size}"
        else:
            render["progress"] = f"Queued · #{position}"
        return _json_response(response, payload)

    httpx.AsyncClient.post = recovered_post
    httpx.AsyncClient.get = recovered_get
    httpx.AsyncClient._rankguess_ordr_recovery = True
