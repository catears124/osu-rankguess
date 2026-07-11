from __future__ import annotations

import asyncio
import math
import os
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse

from replay_features import (
    ACTION_WINDOW_COUNT,
    ACTION_WINDOW_LENGTH,
    EVENT_SEQUENCE_LENGTH,
    REPLAY_SUMMARY_NAMES,
    WINDOW_CHANNEL_NAMES,
    EVENT_CHANNEL_NAMES,
    build_action_windows,
    build_event_sequence,
    build_replay_summary,
    parse_osr,
)

ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
MODEL_PATH = ROOT / "model" / "model.onnx"
PREPROCESSING_PATH = ROOT / "model" / "preprocessing.json"

MAX_REPLAY_BYTES = 4_000_000

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
    "NF", "EZ", "HD", "HR", "SD", "DT", "HT", "NC", "FL", "SO", "PF"
]

# Mods sent to the difficulty-attributes endpoint. NC subsumes DT; PF subsumes SD.
DIFFICULTY_MOD_TOKENS = {"EZ", "HD", "HR", "DT", "HT", "NC", "FL"}

app = FastAPI(
    title="osu!rankguess",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.add_middleware(GZipMiddleware, minimum_size=1_000)

_session: ort.InferenceSession | None = None
_session_lock = asyncio.Lock()
_token_lock = asyncio.Lock()
_osu_token: str | None = None
_osu_token_expires_at = 0.0
_beatmap_cache: dict[tuple[str, int], dict[str, Any]] = {}


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


def canonical_mods_from_mask(mask: int) -> tuple[list[str], list[str]]:
    """Return model feature mods and human/API display mods."""
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

    # Match the training feature construction.
    model_mods = []
    for token in MODEL_MOD_TOKENS:
        active = token in enabled
        if token == "DT" and "NC" in enabled:
            active = True
        if token == "SD" and "PF" in enabled:
            # Dataset mod strings use PF rather than the implied SD component.
            active = False
        if active:
            model_mods.append(token)

    display_mods = [
        token for token in MODEL_MOD_TOKENS
        if token in enabled
        and not (token == "DT" and "NC" in enabled)
        and not (token == "SD" and "PF" in enabled)
    ]

    return model_mods, display_mods


def calculate_accuracy(header: dict[str, Any]) -> float:
    count_300 = int(header["count_300"])
    count_100 = int(header["count_100"])
    count_50 = int(header["count_50"])
    count_miss = int(header["count_miss"])
    total = count_300 + count_100 + count_50 + count_miss
    if total <= 0:
        raise HTTPException(
            status_code=422,
            detail={"code": "empty_score", "message": "Replay has no scored hit results."},
        )
    return (300.0 * count_300 + 100.0 * count_100 + 50.0 * count_50) / (300.0 * total)


def build_tabular_core(star: float, accuracy: float, length_seconds: float, model_mods: list[str]) -> np.ndarray:
    gap = 1.0 - accuracy
    values = [
        star,
        accuracy,
        gap,
        math.log1p(max(length_seconds, 0.0)),
        star ** 2,
        star * accuracy,
        star * accuracy ** 2,
        math.log1p(max(gap, 0.0) * 100.0),
    ]
    enabled = set(model_mods)
    values.extend(1.0 if token in enabled else 0.0 for token in MODEL_MOD_TOKENS)
    array = np.asarray(values, dtype=np.float32).reshape(1, -1)
    if array.shape != (1, 19):
        raise RuntimeError(f"Unexpected tabular shape: {array.shape}")
    return array


async def get_osu_access_token(client: httpx.AsyncClient) -> str:
    global _osu_token, _osu_token_expires_at

    now = time.time()
    if _osu_token and now < _osu_token_expires_at - 60:
        return _osu_token

    client_id = os.getenv("OSU_CLIENT_ID")
    client_secret = os.getenv("OSU_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("OSU_CLIENT_ID and OSU_CLIENT_SECRET are not configured")

    async with _token_lock:
        now = time.time()
        if _osu_token and now < _osu_token_expires_at - 60:
            return _osu_token

        response = await client.post(
            "https://osu.ppy.sh/oauth/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
                "scope": "public",
            },
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        _osu_token = str(payload["access_token"])
        _osu_token_expires_at = now + float(payload.get("expires_in", 86_400))
        return _osu_token


async def resolve_beatmap_metadata(beatmap_hash: str, mods_mask: int, display_mods: list[str]) -> dict[str, Any]:
    cache_key = (beatmap_hash, mods_mask)
    if cache_key in _beatmap_cache:
        return _beatmap_cache[cache_key]

    timeout = httpx.Timeout(12.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        token = await get_osu_access_token(client)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        lookup = await client.get(
            "https://osu.ppy.sh/api/v2/beatmaps/lookup",
            params={"checksum": beatmap_hash},
            headers=headers,
        )
        if lookup.status_code == 404:
            raise RuntimeError("Beatmap checksum was not found by osu!api")
        lookup.raise_for_status()
        beatmap = lookup.json()

        beatmap_id = int(beatmap["id"])
        attribute_mods = [
            token for token in display_mods if token in DIFFICULTY_MOD_TOKENS
        ]
        attributes_response = await client.post(
            f"https://osu.ppy.sh/api/v2/beatmaps/{beatmap_id}/attributes",
            headers=headers,
            json={"mods": attribute_mods, "ruleset": "osu"},
        )
        attributes_response.raise_for_status()
        attributes = attributes_response.json().get("attributes", {})

    star = float(attributes.get("star_rating", beatmap["difficulty_rating"]))
    clock_rate = 1.5 if (mods_mask & (MOD_BITS["DT"] | MOD_BITS["NC"])) else 0.75 if (mods_mask & MOD_BITS["HT"]) else 1.0
    length_seconds = float(beatmap["total_length"]) / clock_rate

    beatmapset = beatmap.get("beatmapset") or {}
    title = beatmapset.get("title") or f"Beatmap {beatmap_id}"
    artist = beatmapset.get("artist") or "Unknown artist"
    version = beatmap.get("version") or "Unknown difficulty"

    result = {
        "id": beatmap_id,
        "beatmapsetId": beatmap.get("beatmapset_id"),
        "star": star,
        "lengthSeconds": length_seconds,
        "title": title,
        "artist": artist,
        "version": version,
        "creator": beatmapset.get("creator"),
        "url": beatmap.get("url") or f"https://osu.ppy.sh/beatmaps/{beatmap_id}",
        "cover": (beatmapset.get("covers") or {}).get("cover@2x") or (beatmapset.get("covers") or {}).get("cover"),
        "source": "osu_api",
    }
    _beatmap_cache[cache_key] = result
    return result


def confidence_label(uncertainty: float) -> str:
    if uncertainty <= 0.08:
        return "high"
    if uncertainty <= 0.16:
        return "medium"
    return "low"


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "index.html")


@app.get("/app.js", include_in_schema=False)
async def frontend_script() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "app.js", media_type="text/javascript")


@app.get("/styles.css", include_in_schema=False)
async def frontend_styles() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "styles.css", media_type="text/css")


@app.get("/favicon.svg", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    session = await get_model_session()
    return {
        "ok": True,
        "model": MODEL_PATH.name,
        "inputs": {value.name: value.shape for value in session.get_inputs()},
        "osuApiConfigured": bool(os.getenv("OSU_CLIENT_ID") and os.getenv("OSU_CLIENT_SECRET")),
    }


@app.post("/api/predict")
async def predict(
    replay: UploadFile = File(...),
    star: float | None = Form(default=None),
    length_seconds: float | None = Form(default=None),
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
            detail={
                "code": "file_too_large",
                "message": "Replay exceeds the 4 MB application limit.",
            },
        )

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
            detail={
                "code": "unsupported_mode",
                "message": "This model currently supports osu!standard replays only.",
            },
        )
    if len(events) < 2:
        raise HTTPException(
            status_code=422,
            detail={"code": "empty_replay", "message": "Replay contains too few cursor events."},
        )

    mods_mask = int(header["mods_mask"])
    model_mods, display_mods = canonical_mods_from_mask(mods_mask)
    accuracy = calculate_accuracy(header)

    metadata: dict[str, Any]
    if star is not None and length_seconds is not None:
        if not (0.1 <= star <= 30.0 and 1.0 <= length_seconds <= 3_600.0):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_metadata", "message": "Manual star/length values are out of range."},
            )
        metadata = {
            "id": None,
            "beatmapsetId": None,
            "star": float(star),
            "lengthSeconds": float(length_seconds),
            "title": "Manual beatmap metadata",
            "artist": "",
            "version": "",
            "creator": None,
            "url": None,
            "cover": None,
            "source": "manual",
        }
    else:
        try:
            metadata = await resolve_beatmap_metadata(
                str(header["beatmap_hash"]), mods_mask, display_mods
            )
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "metadata_required",
                    "message": (
                        "Automatic beatmap lookup failed. Enter the mod-adjusted star rating "
                        "and map length to run the model manually."
                    ),
                    "reason": str(exc),
                    "beatmapHash": header["beatmap_hash"],
                },
            ) from exc

    tabular_core = build_tabular_core(
        float(metadata["star"]), accuracy, float(metadata["lengthSeconds"]), model_mods
    )
    event_sequence = build_event_sequence(events)[None, :, :].astype(np.float32, copy=False)
    action_windows = build_action_windows(events)[None, :, :, :].astype(np.float32, copy=False)
    replay_summary = build_replay_summary(parsed)[None, :].astype(np.float32, copy=False)

    expected_shapes = {
        "tabular_core": (1, 19),
        "event_sequence": (1, len(EVENT_CHANNEL_NAMES), EVENT_SEQUENCE_LENGTH),
        "action_windows": (1, ACTION_WINDOW_COUNT, len(WINDOW_CHANNEL_NAMES), ACTION_WINDOW_LENGTH),
        "replay_summary": (1, len(REPLAY_SUMMARY_NAMES)),
    }
    tensors = {
        "tabular_core": tabular_core,
        "event_sequence": event_sequence,
        "action_windows": action_windows,
        "replay_summary": replay_summary,
    }
    for name, expected in expected_shapes.items():
        if tensors[name].shape != expected:
            raise RuntimeError(f"{name}: expected {expected}, got {tensors[name].shape}")

    session = await get_model_session()
    outputs = await asyncio.to_thread(session.run, None, tensors)

    skill = float(np.asarray(outputs[0]).reshape(-1)[0])
    base_skill = float(np.asarray(outputs[1]).reshape(-1)[0])
    replay_correction = float(np.asarray(outputs[2]).reshape(-1)[0])
    replay_gate = float(np.asarray(outputs[3]).reshape(-1)[0])
    uncertainty = float(np.asarray(outputs[4]).reshape(-1)[0])
    ordinal = np.asarray(outputs[5], dtype=np.float64).reshape(-1)

    if not all(math.isfinite(value) for value in [skill, base_skill, replay_correction, replay_gate, uncertainty]):
        raise RuntimeError("Model produced non-finite output")

    rank_percentile = 10.0 ** (-skill)
    top_percent = 100.0 * rank_percentile
    one_in = max(1, int(round(1.0 / max(rank_percentile, 1e-12))))
    population = int(os.getenv("OSU_RANK_POPULATION", "0") or 0)
    estimated_rank = max(1, int(round(rank_percentile * population))) if population > 0 else None

    response = {
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
        "player": header["player_name"],
        "accuracy": accuracy,
        "accuracyPercent": 100.0 * accuracy,
        "mods": display_mods or ["NM"],
        "score": int(header["score"]),
        "maxCombo": int(header["max_combo"]),
        "hitCounts": {
            "300": int(header["count_300"]),
            "100": int(header["count_100"]),
            "50": int(header["count_50"]),
            "miss": int(header["count_miss"]),
        },
        "eventCount": int(len(events)),
        "beatmapHash": header["beatmap_hash"],
        "beatmap": metadata,
    }
    return JSONResponse(response)
