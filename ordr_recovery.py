"""Deduplicate o!rdr submissions without polling the public render list."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx

from render_jobs import get_render_job, save_render_job

ORDR_RENDER_URL = "https://apis.issou.best/ordr/renders"
_DUPLICATE_ERROR_CODE = 29
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
    hash_prefix: str = ""
    username: str = ""
    accuracy_percent: float | None = None
    score: int | None = None
    mods: tuple[str, ...] = ()


def _payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return {"message": response.text[:1000]}


def _render_id(value: Any) -> int | None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if "render" in normalized and "id" in normalized:
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


def _error_code(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("errorCode", "error_code", "code"):
        try:
            return int(payload.get(key))
        except (TypeError, ValueError):
            continue
    detail = payload.get("detail")
    return _error_code(detail) if detail is not None else None


def _mods_from_mask(mask: int) -> tuple[str, ...]:
    enabled = {name for name, bit in _MOD_BITS.items() if mask & bit}
    if "NC" in enabled:
        enabled.discard("DT")
    if "PF" in enabled:
        enabled.discard("SD")
    return tuple(sorted(enabled))


def _accuracy(header: dict[str, Any]) -> float | None:
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


def _identity(kwargs: dict[str, Any]) -> ReplayIdentity:
    files = kwargs.get("files") or {}
    replay = files.get("replayFile") if isinstance(files, dict) else None
    filename = str(replay[0]) if isinstance(replay, (tuple, list)) and replay else ""
    replay_bytes = replay[1] if isinstance(replay, (tuple, list)) and len(replay) > 1 else None
    match = re.search(r"-([0-9a-f]{12})\.osr$", filename, re.IGNORECASE)
    prefix = match.group(1).lower() if match else ""
    username = re.sub(r"-[0-9a-f]{12}\.osr$", "", filename, flags=re.IGNORECASE).replace("_", " ").strip()
    if not isinstance(replay_bytes, (bytes, bytearray)):
        return ReplayIdentity(hash_prefix=prefix, username=username)
    try:
        from replay_features import parse_osr

        header = dict(parse_osr(bytes(replay_bytes)).get("header") or {})
        try:
            score = int(header.get("score") or 0) or None
        except (TypeError, ValueError):
            score = None
        try:
            mods_mask = int(header.get("mods_mask") or 0)
        except (TypeError, ValueError):
            mods_mask = 0
        return ReplayIdentity(
            hash_prefix=prefix,
            username=str(header.get("player_name") or username).strip(),
            accuracy_percent=_accuracy(header),
            score=score,
            mods=_mods_from_mask(mods_mask),
        )
    except Exception:
        return ReplayIdentity(hash_prefix=prefix, username=username)


def _normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _render_match_score(render: dict[str, Any], identity: ReplayIdentity) -> float:
    text = " ".join(str(value or "") for value in render.values())
    replay_username = str(render.get("replayUsername") or "")
    points = 0.0
    if identity.username and _normal(identity.username) == _normal(replay_username):
        points += 40.0
    elif identity.username and _normal(identity.username) in _normal(text):
        points += 28.0

    percentages = []
    for match in _PERCENT_PATTERN.finditer(text):
        try:
            percentages.append(float(match.group(1)))
        except ValueError:
            pass
    if identity.accuracy_percent is not None and percentages:
        distance = min(abs(value - identity.accuracy_percent) for value in percentages)
        if distance <= 0.02:
            points += 30.0
        elif distance <= 0.10:
            points += 16.0

    raw_mods = str(render.get("replayMods") or "").upper()
    if identity.mods and all(mod in raw_mods for mod in identity.mods):
        points += 10.0
    try:
        if identity.score is not None and int(render.get("score") or render.get("replayScore") or 0) == identity.score:
            points += 30.0
    except (TypeError, ValueError):
        pass
    return points


async def _single_recovery_lookup(
    original_get,
    client: httpx.AsyncClient,
    identity: ReplayIdentity,
) -> int | None:
    candidates: list[tuple[float, int]] = []
    for page in (1, 2):
        try:
            response = await original_get(
                client,
                ORDR_RENDER_URL,
                params={"pageSize": 100, "page": page},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError:
            break
        if response.status_code != 200:
            break
        payload = _payload(response)
        renders = payload.get("renders") if isinstance(payload, dict) else None
        if not isinstance(renders, list):
            break
        for render in renders:
            if not isinstance(render, dict):
                continue
            render_id = _render_id(render)
            if not render_id:
                continue
            score = _render_match_score(render, identity)
            if score >= 55.0:
                candidates.append((score, render_id))
        if candidates:
            break
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _synthetic_success(response: httpx.Response, render_id: int, *, reused: bool) -> httpx.Response:
    return httpx.Response(
        status_code=201,
        json={"message": "Render reused", "renderID": int(render_id), "errorCode": 0, "reused": reused},
        request=response.request,
        headers={"x-rankguess-reused-render": "1"} if reused else {},
    )


def install() -> None:
    if getattr(httpx.AsyncClient, "_rankguess_ordr_recovery", False):
        return

    original_post = httpx.AsyncClient.post
    original_get = httpx.AsyncClient.get

    async def recovered_post(self: httpx.AsyncClient, url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
        if str(url).rstrip("/") != ORDR_RENDER_URL:
            return await original_post(self, url, *args, **kwargs)

        identity = _identity(kwargs)
        if identity.hash_prefix:
            existing = await asyncio.to_thread(get_render_job, identity.hash_prefix)
            if existing and int(existing.get("render_id") or 0) > 0:
                request = httpx.Request("POST", str(url))
                placeholder = httpx.Response(status_code=200, request=request)
                return _synthetic_success(placeholder, int(existing["render_id"]), reused=True)

        response = await original_post(self, url, *args, **kwargs)
        payload = _payload(response)
        render_id = _render_id(payload)

        if response.status_code == 201 and render_id:
            if identity.hash_prefix:
                await asyncio.to_thread(save_render_job, identity.hash_prefix, render_id, identity.username)
            return response

        if _error_code(payload) != _DUPLICATE_ERROR_CODE:
            return response

        if not render_id:
            render_id = await _single_recovery_lookup(original_get, self, identity)
        if not render_id:
            return response

        if identity.hash_prefix:
            await asyncio.to_thread(save_render_job, identity.hash_prefix, render_id, identity.username)
        return _synthetic_success(response, render_id, reused=True)

    httpx.AsyncClient.post = recovered_post
    httpx.AsyncClient._rankguess_ordr_recovery = True
