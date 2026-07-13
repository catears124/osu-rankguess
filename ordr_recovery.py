"""Recover an existing o!rdr render when the upload API reports a duplicate."""
from __future__ import annotations

import json
import re
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


def _upload_identity(kwargs: dict[str, Any]) -> tuple[str, str]:
    files = kwargs.get("files") or {}
    replay = files.get("replayFile") if isinstance(files, dict) else None
    filename = str(replay[0]) if isinstance(replay, (tuple, list)) and replay else ""
    match = re.search(r"-([0-9a-f]{12})\.osr$", filename, re.IGNORECASE)
    return filename, match.group(1).lower() if match else ""


async def _find_recent_render(client: httpx.AsyncClient, filename: str, hash_prefix: str) -> int | None:
    if not filename and not hash_prefix:
        return None
    needles = tuple(value.casefold() for value in (filename, hash_prefix) if value)
    for params in ({"search": hash_prefix, "pageSize": 100}, {"pageSize": 100}):
        try:
            response = await client.get(ORDR_RENDER_URL, params=params, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            continue
        if response.status_code != 200:
            continue
        payload = _payload(response)
        renders = payload.get("renders") if isinstance(payload, dict) else payload
        if not isinstance(renders, list):
            continue
        for render in renders:
            haystack = json.dumps(render, ensure_ascii=False, separators=(",", ":")).casefold()
            if needles and any(needle in haystack for needle in needles):
                candidate = _render_id(render)
                if candidate:
                    return candidate
    return None


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
            filename, hash_prefix = _upload_identity(kwargs)
            render_id = await _find_recent_render(self, filename, hash_prefix)
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
