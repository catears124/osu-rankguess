from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from replay_features import (
    ACTION_WINDOW_COUNT,
    ACTION_WINDOW_LENGTH,
    EVENT_CHANNEL_NAMES,
    EVENT_SEQUENCE_LENGTH,
    REPLAY_SUMMARY_NAMES,
    WINDOW_CHANNEL_NAMES,
    build_action_windows,
    build_event_sequence,
    build_replay_summary,
    parse_osr,
)

ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "model" / "model.onnx"

MAX_REPLAY_BYTES = 4_000_000
MAX_CACHE_TOKEN_BYTES = 1_500_000
CACHE_TTL_SECONDS = int(os.getenv("REPLAY_CACHE_TTL_SECONDS", "1800"))
ORDR_RENDER_URL = "https://apis.issou.best/ordr/renders"
ORDR_DYNLINK_URL = "https://apis.issou.best/dynlink/ordr/gen"

MOD_BITS: dict[str, int] = {
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

MODEL_MOD_TOKENS = [
    "NF",
    "EZ",
    "HD",
    "HR",
    "SD",
    "DT",
    "HT",
    "NC",
    "FL",
    "SO",
    "PF",
]

HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

DESCRIPTION_PATTERN = re.compile(
    r"^\s*Player:\s*(?P<player>.*?),\s*"
    r"Map:\s*(?P<map>.*),\s*"
    r"song length is\s*(?P<length>\d+(?::\d{2}){1,2})\s*"
    r"\((?P<star>\d+(?:\.\d+)?)\s*(?:⭐|★|\*)\)\s*"
    r"\|\s*Accuracy:\s*(?P<accuracy>\d+(?:\.\d+)?)%\s*$",
    re.IGNORECASE,
)

MAP_PATTERN = re.compile(
    r"^(?P<artist>.*?)\s+-\s+(?P<title>.*)\s+"
    r"\[(?P<version>[^\]]+)\]\s+by\s+(?P<creator>.+)$"
)

app = FastAPI(
    title="osu!rankguess",
    version="2.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.add_middleware(GZipMiddleware, minimum_size=1_000)

_session: ort.InferenceSession | None = None
_session_lock = asyncio.Lock()
_cache_lock = asyncio.Lock()


@dataclass
class CachedReplay:
    replay_hash: str
    player: str
    header: dict[str, Any]
    event_count: int
    event_sequence: np.ndarray
    action_windows: np.ndarray
    replay_summary: np.ndarray
    model_mods: list[str]
    display_mods: list[str]
    expires_at: float
    replay_bytes: bytes | None = None


_replay_cache: dict[str, CachedReplay] = {}


class PredictPayload(BaseModel):
    replay_hash: str = Field(alias="replayHash")
    description: str
    video_url: str = Field(alias="videoURL")
    cache_token: str | None = Field(default=None, alias="cacheToken")
    render_id: int | None = Field(default=None, alias="renderID")

    model_config = {"populate_by_name": True}


def get_model_session_sync() -> ort.InferenceSession:
    global _session
    if _session is None:
        if not MODEL_PATH.exists():
            raise RuntimeError(f"Model missing: {MODEL_PATH}")
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = max(1, min(2, os.cpu_count() or 1))
        options.inter_op_num_threads = 1
        _session = ort.InferenceSession(
            str(MODEL_PATH),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
    return _session


async def get_model_session() -> ort.InferenceSession:
    if _session is not None:
        return _session
    async with _session_lock:
        return await asyncio.to_thread(get_model_session_sync)


def normalize_hash(value: str) -> str:
    normalized = value.strip().lower()
    if not HASH_PATTERN.fullmatch(normalized):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_hash",
                "message": "Replay hash must be a lowercase SHA-256 hex digest.",
            },
        )
    return normalized


def compute_replay_hash(replay_bytes: bytes) -> str:
    return hashlib.sha256(replay_bytes).hexdigest()


def validate_replay_hash(replay_bytes: bytes, claimed_hash: str) -> str:
    claimed_hash = normalize_hash(claimed_hash)
    actual_hash = compute_replay_hash(replay_bytes)
    if not hmac.compare_digest(actual_hash, claimed_hash):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "hash_mismatch",
                "message": "The supplied replay hash does not match the uploaded file.",
            },
        )
    return actual_hash


def canonical_mods_from_mask(mask: int) -> tuple[list[str], list[str]]:
    enabled = {name for name, bit in MOD_BITS.items() if mask & bit}

    if "RX" in enabled or "AP" in enabled:
        unsupported = sorted(enabled.intersection({"RX", "AP"}))
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unsupported_mods",
                "message": (
                    "Relax and Autopilot replays were excluded from training. "
                    f"Unsupported mods: {', '.join(unsupported)}."
                ),
            },
        )

    model_mods: list[str] = []
    for token in MODEL_MOD_TOKENS:
        active = token in enabled
        if token == "DT" and "NC" in enabled:
            active = True
        if token == "SD" and "PF" in enabled:
            active = False
        if active:
            model_mods.append(token)

    display_mods = [
        token
        for token in MODEL_MOD_TOKENS
        if token in enabled
        and not (token == "DT" and "NC" in enabled)
        and not (token == "SD" and "PF" in enabled)
    ]

    return model_mods, display_mods


def build_tabular_core(
    star: float,
    accuracy: float,
    length_seconds: float,
    model_mods: list[str],
) -> np.ndarray:
    gap = 1.0 - accuracy
    values = [
        star,
        accuracy,
        gap,
        math.log1p(max(length_seconds, 0.0)),
        star**2,
        star * accuracy,
        star * accuracy**2,
        math.log1p(max(gap, 0.0) * 100.0),
    ]
    enabled = set(model_mods)
    values.extend(1.0 if token in enabled else 0.0 for token in MODEL_MOD_TOKENS)
    array = np.asarray(values, dtype=np.float32).reshape(1, -1)
    if array.shape != (1, 19):
        raise RuntimeError(f"Unexpected tabular shape: {array.shape}")
    return array


def confidence_label(uncertainty: float) -> str:
    if uncertainty <= 0.08:
        return "high"
    if uncertainty <= 0.16:
        return "medium"
    return "low"


def cache_signing_secret() -> bytes:
    value = (
        os.getenv("CACHE_SIGNING_SECRET")
        or os.getenv("ORDR_API_KEY")
        or os.getenv("OSU_CLIENT_SECRET")
    )
    if not value:
        raise RuntimeError(
            "Set CACHE_SIGNING_SECRET (or ORDR_API_KEY/OSU_CLIENT_SECRET) "
            "so replay feature tokens remain valid across Vercel instances."
        )
    return value.encode("utf-8")


def encode_cache_token(cached: CachedReplay) -> str:
    metadata = {
        "version": 1,
        "replayHash": cached.replay_hash,
        "player": cached.player,
        "header": cached.header,
        "eventCount": cached.event_count,
        "modelMods": cached.model_mods,
        "displayMods": cached.display_mods,
        "expiresAt": cached.expires_at,
    }
    metadata_bytes = json.dumps(
        metadata, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    buffer = io.BytesIO()
    np.savez_compressed(
        buffer,
        metadata=np.frombuffer(metadata_bytes, dtype=np.uint8),
        event_sequence=cached.event_sequence.astype(np.float32, copy=False),
        action_windows=cached.action_windows.astype(np.float32, copy=False),
        replay_summary=cached.replay_summary.astype(np.float32, copy=False),
    )
    payload = buffer.getvalue()
    signature = hmac.new(cache_signing_secret(), payload, hashlib.sha256).digest()
    encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{encoded_payload}.{encoded_signature}"


def _decode_urlsafe(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def decode_cache_token(token: str, expected_hash: str) -> CachedReplay:
    if len(token.encode("utf-8")) > MAX_CACHE_TOKEN_BYTES:
        raise HTTPException(
            status_code=400,
            detail={"code": "token_too_large", "message": "Replay cache token is too large."},
        )

    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = _decode_urlsafe(encoded_payload)
        signature = _decode_urlsafe(encoded_signature)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_cache_token", "message": "Malformed replay cache token."},
        ) from exc

    expected_signature = hmac.new(
        cache_signing_secret(), payload, hashlib.sha256
    ).digest()
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_cache_token", "message": "Replay cache token signature is invalid."},
        )

    try:
        archive = np.load(io.BytesIO(payload), allow_pickle=False)
        metadata = json.loads(bytes(archive["metadata"].tolist()).decode("utf-8"))
        cached = CachedReplay(
            replay_hash=str(metadata["replayHash"]),
            player=str(metadata["player"]),
            header=dict(metadata["header"]),
            event_count=int(metadata["eventCount"]),
            event_sequence=np.asarray(archive["event_sequence"], dtype=np.float32),
            action_windows=np.asarray(archive["action_windows"], dtype=np.float32),
            replay_summary=np.asarray(archive["replay_summary"], dtype=np.float32),
            model_mods=[str(value) for value in metadata["modelMods"]],
            display_mods=[str(value) for value in metadata["displayMods"]],
            expires_at=float(metadata["expiresAt"]),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_cache_token", "message": "Replay cache token payload is invalid."},
        ) from exc

    if not hmac.compare_digest(cached.replay_hash, expected_hash):
        raise HTTPException(
            status_code=400,
            detail={"code": "cache_hash_mismatch", "message": "Cache token belongs to another replay."},
        )
    if cached.expires_at < time.time():
        raise HTTPException(
            status_code=410,
            detail={"code": "cache_expired", "message": "Replay cache expired. Upload the replay again."},
        )
    return cached


def validate_cached_shapes(cached: CachedReplay) -> None:
    expected_shapes = {
        "event_sequence": (1, len(EVENT_CHANNEL_NAMES), EVENT_SEQUENCE_LENGTH),
        "action_windows": (
            1,
            ACTION_WINDOW_COUNT,
            len(WINDOW_CHANNEL_NAMES),
            ACTION_WINDOW_LENGTH,
        ),
        "replay_summary": (1, len(REPLAY_SUMMARY_NAMES)),
    }
    actual = {
        "event_sequence": cached.event_sequence.shape,
        "action_windows": cached.action_windows.shape,
        "replay_summary": cached.replay_summary.shape,
    }
    for name, expected in expected_shapes.items():
        if actual[name] != expected:
            raise RuntimeError(f"{name}: expected {expected}, got {actual[name]}")


async def put_cached_replay(cached: CachedReplay) -> None:
    async with _cache_lock:
        now = time.time()
        expired = [key for key, item in _replay_cache.items() if item.expires_at < now]
        for key in expired:
            _replay_cache.pop(key, None)
        _replay_cache[cached.replay_hash] = cached


async def get_cached_replay(replay_hash: str) -> CachedReplay | None:
    async with _cache_lock:
        cached = _replay_cache.get(replay_hash)
        if cached is None:
            return None
        if cached.expires_at < time.time():
            _replay_cache.pop(replay_hash, None)
            return None
        return cached


def parse_duration(value: str) -> float:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return float(minutes * 60 + seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return float(hours * 3600 + minutes * 60 + seconds)
    raise ValueError("Unsupported duration format")


def parse_ordr_description(description: str) -> dict[str, Any]:
    match = DESCRIPTION_PATTERN.fullmatch(description.strip())
    if not match:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "description_parse_failed",
                "message": "Could not parse o!rdr replay description.",
                "description": description,
            },
        )

    star = float(match.group("star"))
    accuracy_percent = float(match.group("accuracy"))
    length_seconds = parse_duration(match.group("length"))
    if not (0.1 <= star <= 30.0):
        raise HTTPException(status_code=422, detail={"code": "invalid_star", "message": "o!rdr star rating is out of range."})
    if not (0.0 <= accuracy_percent <= 100.0):
        raise HTTPException(status_code=422, detail={"code": "invalid_accuracy", "message": "o!rdr accuracy is out of range."})
    if not (1.0 <= length_seconds <= 7200.0):
        raise HTTPException(status_code=422, detail={"code": "invalid_length", "message": "o!rdr song length is out of range."})

    map_text = match.group("map").strip()
    map_match = MAP_PATTERN.fullmatch(map_text)
    if map_match:
        artist = map_match.group("artist").strip()
        title = map_match.group("title").strip()
        version = map_match.group("version").strip()
        creator = map_match.group("creator").strip()
    else:
        artist = ""
        title = map_text
        version = ""
        creator = None

    return {
        "player": match.group("player").strip(),
        "accuracy": accuracy_percent / 100.0,
        "accuracyPercent": accuracy_percent,
        "star": star,
        "lengthSeconds": length_seconds,
        "map": {
            "id": None,
            "beatmapsetId": None,
            "star": star,
            "lengthSeconds": length_seconds,
            "title": title,
            "artist": artist,
            "version": version,
            "creator": creator,
            "url": None,
            "cover": None,
            "descriptionText": map_text,
            "source": "ordr_description",
        },
    }


def normalize_issou_video_url(value: Any) -> str | None:
    """Return a canonical HTTPS issou.best URL, or None while it is unavailable.

    o!rdr can briefly expose an empty, protocol-relative, or HTTP URL while the
    render is transitioning from complete to CDN-ready.  The status endpoint
    must not mark the render ready until a usable HTTPS URL exists.
    """

    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate or candidate.lower() in {"null", "none", "undefined"}:
        return None

    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif candidate.startswith("http://"):
        candidate = "https://" + candidate[len("http://") :]
    elif candidate.startswith("/"):
        candidate = "https://apis.issou.best" + candidate

    parsed = urlparse(candidate)
    if parsed.scheme != "https" or not parsed.hostname:
        return None

    hostname = parsed.hostname.lower().rstrip(".")
    if not (hostname == "issou.best" or hostname.endswith(".issou.best")):
        return None

    return candidate


def validate_video_url(value: str) -> str:
    normalized = normalize_issou_video_url(value)
    if normalized is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_video_url",
                "message": "o!rdr has not produced a valid HTTPS video URL yet.",
            },
        )
    return normalized


def safe_ordr_error(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {"message": response.text[:500] or f"HTTP {response.status_code}"}


def sanitize_replay_filename(username: str, replay_hash: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", username).strip("._")
    if not safe_name:
        safe_name = "player"
    return f"{safe_name}-{replay_hash[:12]}.osr"


@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse(url="/index.html", status_code=307)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    session = await get_model_session()
    return {
        "ok": True,
        "model": MODEL_PATH.name,
        "version": "2.1.0",
        "inputs": {value.name: value.shape for value in session.get_inputs()},
        "ordrApiKeyConfigured": bool(os.getenv("ORDR_API_KEY")),
        "cacheSigningConfigured": bool(
            os.getenv("CACHE_SIGNING_SECRET")
            or os.getenv("ORDR_API_KEY")
            or os.getenv("OSU_CLIENT_SECRET")
        ),
    }


@app.post("/api/replay/cache")
async def cache_replay(
    replay: UploadFile = File(...),
    replay_hash: str = Form(...),
) -> JSONResponse:
    filename = replay.filename or "replay.osr"
    if not filename.lower().endswith(".osr"):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_file", "message": "Upload a .osr replay file."},
        )

    replay_bytes = await replay.read(MAX_REPLAY_BYTES + 1)
    if len(replay_bytes) > MAX_REPLAY_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "file_too_large", "message": "Replay exceeds the 4 MB application limit."},
        )
    replay_hash = validate_replay_hash(replay_bytes, replay_hash)

    try:
        parsed = parse_osr(replay_bytes)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "parse_failed", "message": f"Could not parse replay: {exc}"},
        ) from exc

    header = parsed["header"]
    events = parsed["events"]
    if int(header["mode"]) != 0:
        raise HTTPException(
            status_code=422,
            detail={"code": "unsupported_mode", "message": "This model supports osu!standard replays only."},
        )
    if len(events) < 2:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "empty_replay",
                "message": "Replay contains too few cursor events after decoding.",
                "eventCount": int(len(events)),
                "gameVersion": int(header["game_version"]),
            },
        )

    mods_mask = int(header["mods_mask"])
    model_mods, display_mods = canonical_mods_from_mask(mods_mask)

    event_sequence = build_event_sequence(events)[None, :, :].astype(np.float32, copy=False)
    action_windows = build_action_windows(events)[None, :, :, :].astype(np.float32, copy=False)
    replay_summary = build_replay_summary(parsed)[None, :].astype(np.float32, copy=False)

    cached = CachedReplay(
        replay_hash=replay_hash,
        player=str(header["player_name"]),
        header=header,
        event_count=int(len(events)),
        event_sequence=event_sequence,
        action_windows=action_windows,
        replay_summary=replay_summary,
        model_mods=model_mods,
        display_mods=display_mods,
        expires_at=time.time() + CACHE_TTL_SECONDS,
        replay_bytes=replay_bytes,
    )
    validate_cached_shapes(cached)
    await put_cached_replay(cached)

    try:
        cache_token = encode_cache_token(cached)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "cache_signing_unconfigured", "message": str(exc)},
        ) from exc

    return JSONResponse(
        {
            "ok": True,
            "replayHash": replay_hash,
            "cacheToken": cache_token,
            "cacheExpiresAt": cached.expires_at,
            "player": cached.player,
            "eventCount": cached.event_count,
            "mods": display_mods or ["NM"],
            "beatmapHash": header["beatmap_hash"],
        }
    )


@app.post("/api/ordr/render")
async def create_ordr_render(
    replay: UploadFile = File(...),
    replay_hash: str = Form(...),
    username: str = Form(...),
) -> JSONResponse:
    replay_bytes = await replay.read(MAX_REPLAY_BYTES + 1)
    if len(replay_bytes) > MAX_REPLAY_BYTES:
        raise HTTPException(status_code=413, detail={"code": "file_too_large", "message": "Replay exceeds 4 MB."})
    replay_hash = validate_replay_hash(replay_bytes, replay_hash)

    try:
        parsed = parse_osr(replay_bytes)
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"code": "parse_failed", "message": f"Could not parse replay: {exc}"}) from exc

    parsed_username = str(parsed["header"]["player_name"])
    if parsed_username and parsed_username.casefold() != username.strip().casefold():
        raise HTTPException(
            status_code=400,
            detail={"code": "username_mismatch", "message": "Replay username changed between pipeline steps."},
        )

    data = {
        "skin": os.getenv("ORDR_SKIN", "whitecatCK1.0"),
        "resolution": os.getenv("ORDR_RESOLUTION", "960x540"),
        "showPPCounter": "false",
        "showScoreboard": "false",
        "showResultScreen": "true",
        "skip": "true",
        "customSkin": "false",
    }
    verification_key = os.getenv("ORDR_API_KEY")
    if verification_key:
        data["verificationKey"] = verification_key

    upload_name = sanitize_replay_filename(parsed_username or username, replay_hash)
    timeout = httpx.Timeout(45.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            ORDR_RENDER_URL,
            data={key: str(value) for key, value in data.items()},
            files={"replayFile": (upload_name, replay_bytes, "application/octet-stream")},
            headers={"Accept": "application/json"},
        )

    payload = safe_ordr_error(response)
    if response.status_code != 201:
        status = 429 if response.status_code == 429 else 502
        raise HTTPException(
            status_code=status,
            detail={
                "code": "ordr_render_failed",
                "message": str(payload.get("message") or "o!rdr rejected the render request."),
                "ordrStatus": response.status_code,
                "ordrErrorCode": payload.get("errorCode"),
            },
        )

    render_id = payload.get("renderID")
    if render_id is None:
        raise HTTPException(status_code=502, detail={"code": "ordr_invalid_response", "message": "o!rdr did not return a render ID."})

    return JSONResponse(
        {
            "ok": True,
            "renderID": int(render_id),
            "player": parsed_username or username,
            "replayHash": replay_hash,
        }
    )


@app.get("/api/ordr/status")
async def get_ordr_status(render_id: int = Query(..., ge=1, alias="renderID")) -> JSONResponse:
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            ORDR_RENDER_URL,
            params={"renderID": render_id},
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            payload = safe_ordr_error(response)
            raise HTTPException(
                status_code=502,
                detail={"code": "ordr_status_failed", "message": str(payload.get("message") or "Could not read o!rdr status.")},
            )
        payload = response.json()
        renders = payload.get("renders") or []
        if not renders:
            return JSONResponse({"ok": True, "renderID": render_id, "ready": False, "progress": "Queued"})

        render = renders[0]
        error_code = int(render.get("errorCode") or 0)
        progress = str(render.get("progress") or "Queued")
        description = str(render.get("description") or "").strip() or None
        if error_code != 0:
            return JSONResponse(
                {
                    "ok": False,
                    "renderID": render_id,
                    "ready": False,
                    "failed": True,
                    "errorCode": error_code,
                    "progress": progress,
                }
            )

        # Do not infer readiness from o!rdr's progress string alone.  In
        # particular, "Waiting for client..." can coexist with a description
        # and a transient/non-HTTPS videoUrl value.  Ask dynlink on every poll
        # and only report ready after we have a canonical HTTPS CDN URL.
        dynlink_url: Any = None
        try:
            dynlink_response = await client.get(
                ORDR_DYNLINK_URL,
                params={"id": render_id},
                headers={"Accept": "application/json"},
            )
            if dynlink_response.status_code == 200:
                dynlink_payload = dynlink_response.json()
                if isinstance(dynlink_payload, dict):
                    dynlink_url = dynlink_payload.get("url")
        except (httpx.HTTPError, ValueError):
            # A transient dynlink failure just means the client should poll again.
            dynlink_url = None

        video_candidates = [
            dynlink_url,
            render.get("videoUrl"),
            render.get("videoURL"),
            render.get("url"),
        ]
        video_url = next(
            (
                normalized
                for candidate in video_candidates
                if (normalized := normalize_issou_video_url(candidate)) is not None
            ),
            None,
        )

    ready = bool(description and video_url)
    client_progress = progress
    if description and not video_url:
        client_progress = "Finalizing video link"

    return JSONResponse(
        {
            "ok": True,
            "renderID": render_id,
            "ready": ready,
            "failed": False,
            "progress": client_progress,
            "ordrProgress": progress,
            "description": description,
            "videoURL": video_url,
            "replayUsername": render.get("replayUsername"),
            "title": render.get("title"),
        }
    )


@app.post("/api/predict")
async def predict(payload: PredictPayload) -> JSONResponse:
    replay_hash = normalize_hash(payload.replay_hash)
    video_url = validate_video_url(payload.video_url)

    cached = await get_cached_replay(replay_hash)
    if cached is None and payload.cache_token:
        cached = decode_cache_token(payload.cache_token, replay_hash)
        validate_cached_shapes(cached)
        await put_cached_replay(cached)
    if cached is None:
        raise HTTPException(
            status_code=410,
            detail={"code": "cache_miss", "message": "Replay cache was lost. Upload the replay again."},
        )

    parsed_description = parse_ordr_description(payload.description)
    accuracy = float(parsed_description["accuracy"])
    metadata = parsed_description["map"]

    tabular_core = build_tabular_core(
        float(metadata["star"]),
        accuracy,
        float(metadata["lengthSeconds"]),
        cached.model_mods,
    )
    tensors = {
        "tabular_core": tabular_core,
        "event_sequence": cached.event_sequence,
        "action_windows": cached.action_windows,
        "replay_summary": cached.replay_summary,
    }

    session = await get_model_session()
    outputs = await asyncio.to_thread(session.run, None, tensors)

    skill = float(np.asarray(outputs[0]).reshape(-1)[0])
    base_skill = float(np.asarray(outputs[1]).reshape(-1)[0])
    replay_correction = float(np.asarray(outputs[2]).reshape(-1)[0])
    replay_gate = float(np.asarray(outputs[3]).reshape(-1)[0])
    uncertainty = float(np.asarray(outputs[4]).reshape(-1)[0])
    ordinal = np.asarray(outputs[5], dtype=np.float64).reshape(-1)

    scalar_values = [skill, base_skill, replay_correction, replay_gate, uncertainty]
    if not all(math.isfinite(value) for value in scalar_values):
        raise RuntimeError("Model produced non-finite output")

    rank_percentile = 10.0 ** (-skill)
    top_percent = 100.0 * rank_percentile
    one_in = max(1, int(round(1.0 / max(rank_percentile, 1e-12))))
    population = int(os.getenv("OSU_RANK_POPULATION", "0") or 0)
    estimated_rank = max(1, int(round(rank_percentile * population))) if population > 0 else None

    header = cached.header
    player_warning = None
    description_player = str(parsed_description["player"])
    if description_player.casefold() != cached.player.casefold():
        player_warning = f"o!rdr reported {description_player}; replay header reported {cached.player}."

    return JSONResponse(
        {
            "skill": skill,
            "rankPercentile": rank_percentile,
            "topPercent": top_percent,
            "oneInPlayers": one_in,
            "estimatedRank": estimated_rank,
            "baseSkill": base_skill,
            "replayCorrection": replay_correction,
            "replayGate": replay_gate,
            "uncertainty": uncertainty,
            "confidence": confidence_label(uncertainty),
            "ordinalProbabilities": {
                "gt1": float(ordinal[0]),
                "gt2": float(ordinal[1]),
                "gt3": float(ordinal[2]),
                "gt4": float(ordinal[3]),
                "gt5": float(ordinal[4]),
            },
            "player": cached.player,
            "playerWarning": player_warning,
            "accuracy": accuracy,
            "accuracyPercent": 100.0 * accuracy,
            "mods": cached.display_mods or ["NM"],
            "score": int(header["score"]),
            "maxCombo": int(header["max_combo"]),
            "hitCounts": {
                "300": int(header["count_300"]),
                "100": int(header["count_100"]),
                "50": int(header["count_50"]),
                "miss": int(header["count_miss"]),
            },
            "eventCount": cached.event_count,
            "beatmapHash": header["beatmap_hash"],
            "beatmap": metadata,
            "renderID": payload.render_id,
            "renderDescription": payload.description,
            "videoURL": video_url,
        }
    )
