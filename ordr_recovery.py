"""Recover an existing o!rdr render when the upload API reports a duplicate."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class ReplayIdentity:
    filename: str = ""
    hash_prefix: str = ""
    username: str = ""
    beatmap_hash: str = ""
    accuracy_percent: float | None = None
    score: int | None = None
    mods: tuple[str, ...] = ()


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
            normalized = str(key).replace(" ", "").casefold()
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
    percentages = []
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


def _match_score(render: dict[str, Any], identity: ReplayIdentity) -> float:
    text, username, mods, percentages, score = _render_values(render)
    normalized_text = _normal(text)
    points = 0.0

    if identity.hash_prefix and identity.hash_prefix in text.casefold():
        points += 100.0
    if identity.filename and identity.filename.casefold() in text.casefold():
        points += 100.0
    if identity.beatmap_hash and identity.beatmap_hash in text.casefold():
        points += 80.0

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
            points += 22.0
            replay_specific = True
        elif distance <= 0.08:
            points += 12.0
            replay_specific = True
    if identity.score is not None and score == identity.score:
        points += 24.0
        replay_specific = True
    if identity.mods and mods:
        if set(identity.mods) == set(mods):
            points += 10.0
            replay_specific = True
        elif set(identity.mods).issubset(set(mods)) or set(mods).issubset(set(identity.mods)):
            points += 4.0

    if username_match and replay_specific:
        points += 20.0
    return points


async def _find_recent_render(client: httpx.AsyncClient, identity: ReplayIdentity) -> int | None:
    searches: list[dict[str, Any]] = []
    for value in (identity.hash_prefix, identity.username):
        if value:
            searches.append({"search": value, "pageSize": 100})
    searches.extend({"pageSize": 100, "page": page} for page in range(1, 5))

    seen: set[int] = set()
    ranked: list[tuple[float, int]] = []
    for params in searches:
        try:
            response = await client.get(
                ORDR_RENDER_URL,
                params=params,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError:
            continue
        if response.status_code != 200:
            continue
        payload = _payload(response)
        renders = payload.get("renders") if isinstance(payload, dict) else payload
        if not isinstance(renders, list):
            continue
        for render in renders:
            if not isinstance(render, dict):
                continue
            candidate = _render_id(render)
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            score = _match_score(render, identity)
            if score >= 45.0:
                ranked.append((score, candidate))
        if ranked and max(score for score, _ in ranked) >= 80.0:
            break

    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return ranked[0][1]


def install() -> None:
    if getattr(httpx.AsyncClient, "_rankguess_ordr_recovery", False):
        return

    original_post = httpx.AsyncClient.post

    async def recovered_post(self: httpx.AsyncClient, url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
        response = await original_post(self, url, *args, **kwargs)
        if str(url).rstrip("/") != ORDR_RENDER_URL or response.status_code == 201:
            return response

        payload = _payload(response)
        text = _flatten_text(payload).casefold()
        if not any(marker in text for marker in _DUPLICATE_MARKERS):
            return response

        render_id = _render_id(payload)
        if not render_id:
            for header in ("x-render-id", "render-id", "x-ordr-render-id"):
                try:
                    render_id = int(response.headers.get(header) or 0)
                except (TypeError, ValueError):
                    render_id = 0
                if render_id:
                    break
        if not render_id:
            render_id = await _find_recent_render(self, _upload_identity(kwargs))
        if not render_id:
            return response

        return httpx.Response(
            status_code=201,
            json={"renderID": render_id, "reused": True},
            request=response.request,
            headers={"x-rankguess-reused-render": "1"},
        )

    httpx.AsyncClient.post = recovered_post
    httpx.AsyncClient._rankguess_ordr_recovery = True
