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
import secrets
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field


from database import (
    challenge_count,
    database_configured,
    database_diagnostics,
    get_daily_challenge,
    get_submission,
    get_challenge_submission,
    record_challenge_guess,
    challenge_guess_distribution,
    list_gallery,
    make_public_id,
    save_submission,
    submission_exists,
    update_submission_thumbnail,
)

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
    build_static_features_v2,
    calculate_local_score_pp,
    parse_osu_beatmap,
    STATIC_FEATURE_NAMES_V2,
    parse_osr,
)

ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "model" / "model.onnx"
MODEL_BUNDLE_PATH = ROOT / "model" / "bundle.json"

MAX_REPLAY_BYTES = 4_000_000
MAX_CACHE_TOKEN_BYTES = 1_500_000
CACHE_TTL_SECONDS = int(os.getenv("REPLAY_CACHE_TTL_SECONDS", "1800"))
ORDR_RENDER_URL = "https://apis.issou.best/ordr/renders"
ORDR_DYNLINK_URL = "https://apis.issou.best/dynlink/ordr/gen"
OSU_TOKEN_URL = "https://osu.ppy.sh/oauth/token"
OSU_API_BASE = "https://osu.ppy.sh/api/v2"
OSU_RANK_POPULATION = int(os.getenv("OSU_RANK_POPULATION", "5500000") or 5500000)
GALLERY_SEED_TARGET = max(3, int(os.getenv("GALLERY_SEED_TARGET", "12") or 12))
SEED_RENDER_TIMEOUT_SECONDS = max(30, min(200, int(os.getenv("SEED_RENDER_TIMEOUT_SECONDS", "160") or 160)))
MAX_CHALLENGE_ATTEMPTS = 5

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

# o!rdr's human-readable strings are not a stable API.  Current render
# responses expose useful structured fields (mapLength, mapTitle,
# replayDifficulty, replayMods, replayUsername) plus both a description and a
# compact title.  We accept all of them and only require a star token from
# either text field.
STAR_TOKEN_PATTERN = re.compile(
    r"[\[(]\s*(?P<star>\d+(?:\.\d+)?)\s*(?:⭐|★|\*)\s*[\])]",
    re.IGNORECASE,
)
GENERIC_STAR_PATTERN = re.compile(
    r"(?<![\d.])(?P<star>\d+(?:\.\d+)?)\s*(?:⭐|★|stars?)",
    re.IGNORECASE,
)
ACCURACY_TOKEN_PATTERN = re.compile(
    r"(?P<accuracy>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
LENGTH_TOKEN_PATTERN = re.compile(
    r"song\s+length\s+is\s+(?P<length>\d+(?::\d{2}){1,2})",
    re.IGNORECASE,
)
VERBOSE_MAP_TEXT_PATTERN = re.compile(
    r"Player:\s*(?P<player>.*?),\s*Map:\s*(?P<map>.*?),\s*song\s+length\s+is",
    re.IGNORECASE,
)
COMPACT_TITLE_PATTERN = re.compile(
    r"^\s*\[[^\]]+\]\s*(?P<player>.*?)\s*\|\s*"
    r"(?P<map>.*?)(?:\s+\+(?P<mods>[A-Z0-9]+))?"
    r"\s+(?P<accuracy>\d+(?:\.\d+)?)%\s*$",
    re.IGNORECASE,
)

MAP_PATTERN = re.compile(
    r"^(?P<artist>.*?)\s+-\s+(?P<title>.*)\s+"
    r"\[(?P<version>[^\]]+)\]"
    r"(?:\s+by\s+(?P<creator>.+))?$"
)

app = FastAPI(
    title="osu!rankguess",
    version="4.0.1",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.add_middleware(GZipMiddleware, minimum_size=1_000)

_session: ort.InferenceSession | None = None
_model_bundle: dict[str, Any] | None = None
_session_lock = asyncio.Lock()
_cache_lock = asyncio.Lock()
_osu_token_lock = asyncio.Lock()
_osu_token: str | None = None
_osu_token_expires_at = 0.0
_osu_user_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_osu_beatmap_cache: dict[int, tuple[float, dict[str, Any] | None]] = {}
_osu_beatmap_text_cache: dict[int, tuple[float, str | None]] = {}
_osu_score_match_cache: dict[tuple[str, int], tuple[float, tuple[float | None, float]]] = {}


@dataclass
class CachedReplay:
    replay_hash: str
    player: str
    header: dict[str, Any]
    event_count: int
    event_sequence: np.ndarray
    action_windows: np.ndarray
    replay_summary: np.ndarray
    events: np.ndarray | None
    model_mods: list[str]
    display_mods: list[str]
    expires_at: float
    replay_bytes: bytes | None = None


_replay_cache: dict[str, CachedReplay] = {}


class PredictPayload(BaseModel):
    replay_hash: str = Field(alias="replayHash")
    video_url: str = Field(alias="videoURL")
    description: str | None = None
    render_metadata: dict[str, Any] | None = Field(default=None, alias="renderMetadata")
    cache_token: str | None = Field(default=None, alias="cacheToken")
    render_id: int | None = Field(default=None, alias="renderID")
    publish: bool = True

    model_config = {"populate_by_name": True}


class ChallengeGuessPayload(BaseModel):
    replay_id: str = Field(alias="replayID", min_length=8, max_length=64)
    guess_rank: int = Field(alias="guessRank", ge=1, le=100_000_000)
    attempt: int = Field(ge=1, le=MAX_CHALLENGE_ATTEMPTS)
    mode: str = Field(default="infinite")
    challenge_date: date | None = Field(default=None, alias="challengeDate")
    visitor_id: str = Field(alias="visitorID", min_length=8, max_length=128)

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



def get_model_bundle_sync() -> dict[str, Any] | None:
    """Load an optional v2 ensemble bundle.

    A bundle is deliberately just JSON + ONNX files, so production keeps the
    same small runtime dependency set.  The legacy six-output replay model is
    retained as a fallback until a newly trained bundle is copied into model/.
    """
    global _model_bundle
    if _model_bundle is not None:
        return _model_bundle
    if not MODEL_BUNDLE_PATH.exists():
        return None

    config = json.loads(MODEL_BUNDLE_PATH.read_text(encoding="utf-8"))
    entries = list(config.get("models") or [])
    if not entries:
        raise RuntimeError("model/bundle.json contains no models")

    sessions: list[dict[str, Any]] = []
    for entry in entries:
        filename = str(entry.get("file") or "").strip()
        if not filename:
            raise RuntimeError("Bundle model entry is missing file")
        path = ROOT / "model" / filename
        if not path.exists():
            raise RuntimeError(f"Bundle model missing: {path}")
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = max(1, min(2, os.cpu_count() or 1))
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        sessions.append({
            "session": session,
            "file": filename,
            "type": str(entry.get("type") or "auto"),
            "weight": max(0.0, float(entry.get("weight", 1.0))),
        })

    static_names = [str(value) for value in config.get("staticFeatureNames") or []]
    if static_names and static_names != list(STATIC_FEATURE_NAMES_V2):
        raise RuntimeError(
            "V2 static feature schema differs from the production feature builder"
        )
    _model_bundle = {"config": config, "models": sessions}
    return _model_bundle


async def get_model_bundle() -> dict[str, Any] | None:
    if _model_bundle is not None:
        return _model_bundle
    async with _session_lock:
        return await asyncio.to_thread(get_model_bundle_sync)


def _onnx_dimension(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def run_bundle_model(
    session: ort.InferenceSession,
    *,
    static_features: np.ndarray,
    cached: "CachedReplay",
) -> float:
    feeds: dict[str, np.ndarray] = {}
    for model_input in session.get_inputs():
        name = model_input.name
        lower = name.casefold()
        shape = list(model_input.shape or [])
        last = _onnx_dimension(shape[-1]) if shape else None
        if "event" in lower:
            feeds[name] = cached.event_sequence
        elif "window" in lower or "action" in lower:
            feeds[name] = cached.action_windows
        elif "summary" in lower:
            feeds[name] = cached.replay_summary
        elif "static" in lower:
            feeds[name] = static_features
        elif "tabular" in lower and last == static_features.shape[1]:
            feeds[name] = static_features
        elif last == static_features.shape[1]:
            feeds[name] = static_features
        else:
            raise RuntimeError(
                f"Cannot map ONNX bundle input {name!r} with shape {shape}"
            )
    outputs = session.run(None, feeds)
    if not outputs:
        raise RuntimeError("Bundle model returned no outputs")
    prediction = float(np.asarray(outputs[0], dtype=np.float64).reshape(-1)[0])
    if not math.isfinite(prediction):
        raise RuntimeError("Bundle model returned a non-finite prediction")
    return prediction


def combine_bundle_predictions(
    bundle: dict[str, Any],
    *,
    static_features: np.ndarray,
    cached: "CachedReplay",
) -> tuple[float, float, list[float]]:
    predictions: list[float] = []
    weights: list[float] = []
    for entry in bundle["models"]:
        prediction = run_bundle_model(
            entry["session"],
            static_features=static_features,
            cached=cached,
        )
        predictions.append(prediction)
        weights.append(float(entry["weight"]))

    weight_array = np.asarray(weights, dtype=np.float64)
    if float(weight_array.sum()) <= 0:
        weight_array[:] = 1.0
    weight_array /= weight_array.sum()
    prediction_array = np.asarray(predictions, dtype=np.float64)
    raw_skill = float(np.dot(weight_array, prediction_array))

    calibration = bundle["config"].get("calibration") or {}
    intercept = float(calibration.get("intercept", 0.0))
    slope = float(calibration.get("slope", 1.0))
    skill = intercept + slope * raw_skill
    x_thresholds = calibration.get("xThresholds") or []
    y_thresholds = calibration.get("yThresholds") or []
    if len(x_thresholds) >= 2 and len(x_thresholds) == len(y_thresholds):
        skill = float(np.interp(
            skill,
            np.asarray(x_thresholds, dtype=np.float64),
            np.asarray(y_thresholds, dtype=np.float64),
        ))

    ensemble_std = float(
        np.sqrt(np.dot(weight_array, np.square(prediction_array - raw_skill)))
    )
    residual_floor = max(
        0.0,
        float(bundle["config"].get("uncertaintyFloor", 0.06)),
    )
    uncertainty = math.sqrt(ensemble_std ** 2 + residual_floor ** 2)
    return skill, uncertainty, predictions

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
        "version": 2,
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
        events=(cached.events.astype(np.float32, copy=False) if cached.events is not None else np.zeros((0, 4), dtype=np.float32)),
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
            events=(np.asarray(archive["events"], dtype=np.float32) if "events" in archive.files else None),
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


def build_cached_replay_from_bytes(
    replay_bytes: bytes,
    replay_hash: str | None = None,
    *,
    keep_replay_bytes: bool = False,
) -> CachedReplay:
    if len(replay_bytes) > MAX_REPLAY_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "file_too_large", "message": "Replay exceeds the 4 MB application limit."},
        )

    replay_hash = replay_hash or compute_replay_hash(replay_bytes)
    parsed = parse_osr(replay_bytes)
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

    model_mods, display_mods = canonical_mods_from_mask(int(header["mods_mask"]))
    cached = CachedReplay(
        replay_hash=replay_hash,
        player=str(header["player_name"]),
        header=header,
        event_count=int(len(events)),
        event_sequence=build_event_sequence(events)[None, :, :].astype(np.float32, copy=False),
        action_windows=build_action_windows(events)[None, :, :, :].astype(np.float32, copy=False),
        replay_summary=build_replay_summary(parsed)[None, :].astype(np.float32, copy=False),
        events=np.asarray(events, dtype=np.float32),
        model_mods=model_mods,
        display_mods=display_mods,
        expires_at=time.time() + CACHE_TTL_SECONDS,
        replay_bytes=replay_bytes if keep_replay_bytes else None,
    )
    validate_cached_shapes(cached)
    return cached


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


def replay_accuracy_from_header(header: dict[str, Any]) -> float:
    count_300 = int(header.get("count_300") or 0)
    count_100 = int(header.get("count_100") or 0)
    count_50 = int(header.get("count_50") or 0)
    count_miss = int(header.get("count_miss") or 0)
    total_hits = count_300 + count_100 + count_50 + count_miss
    if total_hits <= 0:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_hit_counts", "message": "Replay has no score hit counts."},
        )
    return (
        300.0 * count_300 + 100.0 * count_100 + 50.0 * count_50
    ) / (300.0 * total_hits)


def replay_duration_from_summary(cached: CachedReplay) -> float:
    try:
        index = REPLAY_SUMMARY_NAMES.index("duration_seconds")
        value = float(cached.replay_summary[0, index])
    except (ValueError, IndexError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "missing_replay_duration", "message": "Replay duration feature is unavailable."},
        ) from exc
    if not math.isfinite(value) or not (1.0 <= value <= 7200.0):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_replay_duration", "message": "Replay duration is out of range."},
        )
    return value


def _clean_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _extract_star(*texts: Any) -> float | None:
    for value in texts:
        text = _clean_text(value)
        if not text:
            continue
        match = STAR_TOKEN_PATTERN.search(text) or GENERIC_STAR_PATTERN.search(text)
        if match:
            star = float(match.group("star"))
            if 0.1 <= star <= 30.0:
                return star
    return None


def _extract_accuracy_percent(*texts: Any) -> float | None:
    for value in texts:
        text = _clean_text(value)
        if not text:
            continue
        matches = list(ACCURACY_TOKEN_PATTERN.finditer(text))
        if matches:
            accuracy = float(matches[-1].group("accuracy"))
            if 0.0 <= accuracy <= 100.0:
                return accuracy
    return None


def _parse_map_text(map_text: str) -> dict[str, Any]:
    map_text = map_text.strip()
    map_match = MAP_PATTERN.fullmatch(map_text)
    if map_match:
        creator_group = map_match.group("creator")
        return {
            "title": map_match.group("title").strip(),
            "artist": map_match.group("artist").strip(),
            "version": map_match.group("version").strip(),
            "creator": creator_group.strip() if creator_group else None,
            "descriptionText": map_text,
        }
    return {
        "title": map_text or "Unknown map",
        "artist": "",
        "version": "",
        "creator": None,
        "descriptionText": map_text,
    }


def parse_ordr_metadata(
    *,
    description: str | None,
    title: str | None,
    render_metadata: dict[str, Any] | None,
    fallback_length_seconds: float,
    fallback_accuracy: float,
    fallback_player: str,
) -> dict[str, Any]:
    """Convert o!rdr's unstable display strings + structured render fields.

    The model only needs star rating, length, accuracy and mods. Accuracy and
    mods are authoritative from the cached .osr. Length prefers o!rdr's
    structured mapLength. The only value that must be recovered from display
    text is star rating, and it is extracted independently from either title or
    description rather than requiring a complete sentence format.
    """

    metadata = dict(render_metadata or {})
    description_text = _clean_text(description or metadata.get("description"))
    title_text = _clean_text(title or metadata.get("title"))

    structured_star = _finite_number(metadata.get("star"))
    if structured_star is not None and 0.1 <= structured_star <= 30.0:
        star = structured_star
    else:
        star = _extract_star(title_text, description_text)
    if star is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "star_parse_failed",
                "message": "Could not find an o!rdr star rating in the render title or description.",
                "description": description_text,
                "title": title_text,
                "renderMetadata": {
                    key: metadata.get(key)
                    for key in (
                        "star",
                        "mapLength",
                        "mapTitle",
                        "replayDifficulty",
                        "replayMods",
                        "replayUsername",
                        "username",
                        "mapID",
                    )
                },
            },
        )

    structured_length = _finite_number(metadata.get("mapLength"))
    length_match = LENGTH_TOKEN_PATTERN.search(description_text)
    if structured_length is not None and 1.0 <= structured_length <= 7200.0:
        length_seconds = structured_length
        length_source = "ordr_mapLength"
    elif length_match:
        length_seconds = parse_duration(length_match.group("length"))
        length_source = "ordr_description"
    else:
        length_seconds = float(fallback_length_seconds)
        length_source = "replay_telemetry"

    if not 1.0 <= length_seconds <= 7200.0:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_length", "message": "Replay length is out of range."},
        )

    display_accuracy = _extract_accuracy_percent(title_text, description_text)
    if display_accuracy is None:
        display_accuracy = 100.0 * float(fallback_accuracy)

    verbose_match = VERBOSE_MAP_TEXT_PATTERN.search(description_text)
    compact_match = COMPACT_TITLE_PATTERN.fullmatch(title_text)

    structured_player = _clean_text(
        metadata.get("replayUsername") or metadata.get("username")
    )
    player = structured_player or fallback_player
    map_text = ""
    description_format = "structured"

    if verbose_match:
        player = _clean_text(verbose_match.group("player")) or player
        map_text = _clean_text(verbose_match.group("map"))
        description_format = "verbose"
    elif compact_match:
        player = _clean_text(compact_match.group("player")) or player
        map_text = _clean_text(compact_match.group("map"))
        description_format = "compact"

    map_title = _clean_text(metadata.get("mapTitle"))
    difficulty = _clean_text(metadata.get("replayDifficulty"))
    if not map_text:
        map_text = map_title
        if difficulty:
            map_text = f"{map_text} [{difficulty}]" if map_text else f"[{difficulty}]"

    parsed_map = _parse_map_text(map_text)
    if map_title and (not parsed_map["title"] or parsed_map["title"] == "Unknown map"):
        parsed_map["title"] = map_title
    if difficulty and not parsed_map["version"]:
        parsed_map["version"] = difficulty

    parsed_mods = _clean_text(metadata.get("replayMods")).upper() or None
    if parsed_mods is None and compact_match:
        parsed_mods = _clean_text(compact_match.group("mods")).upper() or None

    map_id_value = metadata.get("mapID")
    try:
        map_id = int(map_id_value) if map_id_value is not None else None
    except (TypeError, ValueError):
        map_id = None

    parsed_map.update(
        {
            "id": map_id,
            "beatmapsetId": None,
            "star": star,
            "lengthSeconds": length_seconds,
            "url": _clean_text(metadata.get("mapLink")) or None,
            "cover": None,
            "source": "ordr_structured_render",
            "lengthSource": length_source,
        }
    )

    return {
        "player": player,
        "accuracy": display_accuracy / 100.0,
        "accuracyPercent": display_accuracy,
        "star": star,
        "lengthSeconds": length_seconds,
        "descriptionFormat": description_format,
        "parsedMods": parsed_mods,
        "map": parsed_map,
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


async def get_osu_access_token() -> str | None:
    global _osu_token, _osu_token_expires_at

    client_id = os.getenv("OSU_CLIENT_ID")
    client_secret = os.getenv("OSU_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None

    now = time.time()
    if _osu_token and now < _osu_token_expires_at - 60:
        return _osu_token

    async with _osu_token_lock:
        now = time.time()
        if _osu_token and now < _osu_token_expires_at - 60:
            return _osu_token

        timeout = httpx.Timeout(15.0, connect=8.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                OSU_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                    "scope": "public",
                },
                headers={"Accept": "application/json"},
            )
        if response.status_code != 200:
            print(
                json.dumps(
                    {
                        "event": "osu_token_failed",
                        "status": response.status_code,
                        "body": response.text[:300],
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
            return None

        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            return None
        _osu_token = token
        _osu_token_expires_at = now + float(payload.get("expires_in") or 3600)
        return token


async def fetch_osu_user(username: str) -> dict[str, Any] | None:
    normalized = username.strip().casefold()
    cached = _osu_user_cache.get(normalized)
    if cached and cached[0] > time.time():
        return cached[1]

    token = await get_osu_access_token()
    if not token:
        _osu_user_cache[normalized] = (time.time() + 300, None)
        return None

    encoded_user = quote("@" + username.strip(), safe="")
    timeout = httpx.Timeout(15.0, connect=8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{OSU_API_BASE}/users/{encoded_user}/osu",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        print(json.dumps({"event": "osu_user_failed", "error": repr(exc)}), flush=True)
        return None

    if response.status_code != 200:
        print(
            json.dumps(
                {
                    "event": "osu_user_failed",
                    "username": username,
                    "status": response.status_code,
                    "body": response.text[:300],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        _osu_user_cache[normalized] = (time.time() + 300, None)
        return None

    payload = response.json()
    statistics = payload.get("statistics") or {}
    global_rank = statistics.get("global_rank")
    try:
        global_rank = int(global_rank) if global_rank is not None else None
    except (TypeError, ValueError):
        global_rank = None

    result = {
        "id": payload.get("id"),
        "username": payload.get("username") or username,
        "avatarURL": payload.get("avatar_url"),
        "countryCode": payload.get("country_code"),
        "globalRank": global_rank,
    }
    _osu_user_cache[normalized] = (time.time() + 900, result)
    return result


def _osu_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _cover_url_from_beatmap_payload(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    beatmapset = payload.get("beatmapset") or {}
    covers = beatmapset.get("covers") or {}
    for key in ("card@2x", "cover@2x", "card", "cover", "list@2x", "list"):
        value = covers.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
    beatmapset_id = beatmapset.get("id") or payload.get("beatmapset_id")
    try:
        beatmapset_id = int(beatmapset_id)
    except (TypeError, ValueError):
        return None
    return f"https://assets.ppy.sh/beatmaps/{beatmapset_id}/covers/card.jpg"


async def fetch_osu_beatmap(beatmap_id: int | None) -> dict[str, Any] | None:
    if not beatmap_id:
        return None
    beatmap_id = int(beatmap_id)
    cached = _osu_beatmap_cache.get(beatmap_id)
    if cached and cached[0] > time.time():
        return cached[1]

    token = await get_osu_access_token()
    if not token:
        return None

    timeout = httpx.Timeout(15.0, connect=8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{OSU_API_BASE}/beatmaps/{beatmap_id}",
                headers=_osu_headers(token),
            )
    except httpx.HTTPError as exc:
        print(json.dumps({"event": "osu_beatmap_failed", "error": repr(exc)}), flush=True)
        return None

    if response.status_code != 200:
        print(
            json.dumps(
                {
                    "event": "osu_beatmap_failed",
                    "beatmapID": beatmap_id,
                    "status": response.status_code,
                    "body": response.text[:300],
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        _osu_beatmap_cache[beatmap_id] = (time.time() + 300, None)
        return None

    payload = response.json()
    _osu_beatmap_cache[beatmap_id] = (time.time() + 3600, payload)
    return payload



async def fetch_osu_beatmap_text(beatmap_id: int | None) -> str | None:
    if not beatmap_id:
        return None
    beatmap_id = int(beatmap_id)
    cached = _osu_beatmap_text_cache.get(beatmap_id)
    if cached and cached[0] > time.time():
        return cached[1]

    timeout = httpx.Timeout(15.0, connect=8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(
                f"https://osu.ppy.sh/osu/{beatmap_id}",
                headers={"Accept": "text/plain, application/octet-stream;q=0.9"},
            )
    except httpx.HTTPError as exc:
        print(json.dumps({"event": "osu_beatmap_text_failed", "error": repr(exc)}), flush=True)
        return None

    text = response.text if response.status_code == 200 else None
    if text and "[HitObjects]" not in text:
        text = None
    _osu_beatmap_text_cache[beatmap_id] = (time.time() + (86400 if text else 300), text)
    return text


def _canonical_score_mods(mods: list[str]) -> set[str]:
    enabled = {str(token).upper() for token in mods}
    if "NC" in enabled:
        enabled.add("DT")
    if "PF" in enabled:
        enabled.add("SD")
    return enabled - {"CL"}


def _score_statistics(score: dict[str, Any]) -> dict[str, int]:
    stats = score.get("statistics") or {}
    aliases = {
        "count_300": ("count_300", "great"),
        "count_100": ("count_100", "ok"),
        "count_50": ("count_50", "meh"),
        "count_miss": ("count_miss", "miss"),
    }
    result: dict[str, int] = {}
    for destination, candidates in aliases.items():
        value = 0
        for candidate in candidates:
            if candidate in stats and stats[candidate] is not None:
                try:
                    value = int(stats[candidate])
                except (TypeError, ValueError):
                    value = 0
                break
        result[destination] = value
    return result


def _score_match_quality(score: dict[str, Any], cached: CachedReplay) -> float:
    header = cached.header
    replay_mods = _canonical_score_mods(cached.display_mods or cached.model_mods)
    score_mods = _canonical_score_mods(_score_mods(score))
    mod_score = 1.0 if replay_mods == score_mods else 0.0

    replay_accuracy = replay_accuracy_from_header(header)
    accuracy_difference = abs(_score_accuracy(score) - replay_accuracy)
    accuracy_score = math.exp(-accuracy_difference / 0.0008)

    score_stats = _score_statistics(score)
    stat_difference = sum(
        abs(int(header.get(name) or 0) - int(score_stats.get(name) or 0))
        for name in ("count_300", "count_100", "count_50", "count_miss")
    )
    stats_score = math.exp(-stat_difference / 3.0)

    try:
        score_combo = int(score.get("max_combo") or 0)
    except (TypeError, ValueError):
        score_combo = 0
    replay_combo = int(header.get("max_combo") or 0)
    combo_score = math.exp(-abs(score_combo - replay_combo) / max(2.0, replay_combo * 0.02))

    exact_score = 0.0
    try:
        if int(score.get("score") or 0) == int(header.get("score") or -1):
            exact_score = 1.0
    except (TypeError, ValueError):
        exact_score = 0.0

    # Exact score IDs are not encoded in legacy .osr files, so combine the
    # independent fields conservatively.  Mod mismatch caps the quality.
    quality = (
        0.30 * mod_score
        + 0.25 * accuracy_score
        + 0.25 * stats_score
        + 0.10 * combo_score
        + 0.10 * exact_score
    )
    if mod_score == 0.0:
        quality *= 0.35
    return float(np.clip(quality, 0.0, 1.0))


async def fetch_matching_score_pp(
    cached: CachedReplay,
    beatmap_id: int | None,
) -> tuple[float | None, float]:
    """Find this exact public score and return score PP, never profile PP."""
    if not beatmap_id:
        return None, 0.0
    cache_key = (cached.replay_hash, int(beatmap_id))
    cached_result = _osu_score_match_cache.get(cache_key)
    if cached_result and cached_result[0] > time.time():
        return cached_result[1]

    user = await fetch_osu_user(cached.player)
    token = await get_osu_access_token()
    if not user or not user.get("id") or not token:
        return None, 0.0

    timeout = httpx.Timeout(20.0, connect=8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{OSU_API_BASE}/beatmaps/{int(beatmap_id)}/scores/users/{int(user['id'])}/all",
                params={"mode": "osu", "legacy_only": 0},
                headers=_osu_headers(token),
            )
    except httpx.HTTPError as exc:
        print(json.dumps({"event": "score_pp_lookup_failed", "error": repr(exc)}), flush=True)
        return None, 0.0

    if response.status_code != 200:
        _osu_score_match_cache[cache_key] = (time.time() + 300, (None, 0.0))
        return None, 0.0

    payload = response.json()
    scores = payload.get("scores") if isinstance(payload, dict) else payload
    scores = list(scores or [])
    if not scores:
        _osu_score_match_cache[cache_key] = (time.time() + 300, (None, 0.0))
        return None, 0.0

    ranked = sorted(
        ((_score_match_quality(score, cached), score) for score in scores),
        key=lambda item: item[0],
        reverse=True,
    )
    quality, best = ranked[0]
    pp = _finite_number(best.get("pp"))
    if pp is None or pp < 0 or quality < 0.72:
        result = (None, float(quality))
    else:
        result = (float(pp), float(quality))
    _osu_score_match_cache[cache_key] = (time.time() + 1800, result)
    return result

def _score_mods(score: dict[str, Any]) -> list[str]:
    raw = score.get("mods") or []
    result: list[str] = []
    for item in raw:
        if isinstance(item, str):
            acronym = item
        elif isinstance(item, dict):
            acronym = item.get("acronym") or item.get("name")
        else:
            acronym = None
        if acronym:
            result.append(str(acronym).upper())
    return result


def _score_accuracy(score: dict[str, Any]) -> float:
    value = score.get("accuracy")
    try:
        accuracy = float(value)
    except (TypeError, ValueError):
        return 0.0
    if accuracy > 1.0:
        accuracy /= 100.0
    return accuracy


def _score_has_replay(score: dict[str, Any]) -> bool:
    for key in ("has_replay", "replay", "replay_available"):
        value = score.get(key)
        if isinstance(value, bool):
            return value
        if value not in (None, "", 0, "0", "false", "False"):
            return True
    return False


def _score_id(score: dict[str, Any]) -> int | None:
    for key in ("id", "legacy_score_id", "score_id"):
        try:
            value = int(score.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _seed_score_is_good(score: dict[str, Any]) -> bool:
    if not _score_has_replay(score) or _score_id(score) is None:
        return False
    accuracy = _score_accuracy(score)
    if not 0.84 <= accuracy <= 0.99999:
        return False
    mods = set(_score_mods(score))
    if mods.intersection({"RX", "AP", "AT", "FL"}):
        return False
    beatmap = score.get("beatmap") or {}
    try:
        star = float(beatmap.get("difficulty_rating") or 0.0)
        length = float(beatmap.get("total_length") or beatmap.get("hit_length") or 0.0)
    except (TypeError, ValueError):
        return False
    if not 4.5 <= star <= 10.5:
        return False
    if length and not 25 <= length <= 420:
        return False
    rank = str(score.get("rank") or "").upper()
    if rank == "F":
        return False
    return True


def _stable_number(label: str, minimum: int, maximum: int) -> int:
    if maximum < minimum:
        return minimum
    digest = hashlib.sha256(label.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    return minimum + value % (maximum - minimum + 1)


async def fetch_rankings_page(page: int) -> list[dict[str, Any]]:
    token = await get_osu_access_token()
    if not token:
        raise RuntimeError("osu! API credentials are not configured")
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            f"{OSU_API_BASE}/rankings/osu/performance",
            params={"page": int(page)},
            headers=_osu_headers(token),
        )
    if response.status_code != 200:
        raise RuntimeError(f"osu! rankings returned HTTP {response.status_code}: {response.text[:240]}")
    payload = response.json()
    return list(payload.get("ranking") or [])


async def fetch_user_best_scores(user_id: int) -> list[dict[str, Any]]:
    token = await get_osu_access_token()
    if not token:
        raise RuntimeError("osu! API credentials are not configured")
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            f"{OSU_API_BASE}/users/{int(user_id)}/scores/best",
            params={
                "mode": "osu",
                "limit": 100,
                "offset": 0,
                "include_fails": 0,
                "legacy_only": 1,
            },
            headers=_osu_headers(token),
        )
    if response.status_code != 200:
        raise RuntimeError(f"osu! best scores returned HTTP {response.status_code}: {response.text[:240]}")
    payload = response.json()
    return list(payload if isinstance(payload, list) else payload.get("scores") or [])


async def download_score_replay(score_id: int) -> bytes:
    token = await get_osu_access_token()
    if not token:
        raise RuntimeError("osu! API credentials are not configured")
    timeout = httpx.Timeout(30.0, connect=8.0)
    paths = (
        f"{OSU_API_BASE}/scores/osu/{int(score_id)}/download",
        f"{OSU_API_BASE}/scores/{int(score_id)}/download",
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        last_status = None
        last_body = ""
        replay_headers = _osu_headers(token)
        replay_headers["Accept"] = "application/octet-stream"
        for url in paths:
            response = await client.get(url, headers=replay_headers)
            last_status = response.status_code
            last_body = response.text[:240] if "text" in (response.headers.get("content-type") or "") else ""
            if response.status_code == 200 and response.content:
                return bytes(response.content)
            if response.status_code not in {404, 422}:
                break
    raise RuntimeError(f"osu! replay download failed with HTTP {last_status}: {last_body}")


def _iso_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def gallery_item_from_row(row: dict[str, Any], *, reveal_answer: bool = True) -> dict[str, Any]:
    item = {
        "id": row["public_id"],
        "renderID": row.get("render_id"),
        "videoURL": row["video_url"],
        "thumbnailURL": row.get("thumbnail_url") or f"/api/gallery/{row['public_id']}/thumbnail",
        "source": row.get("source") or "upload",
        "player": row.get("player"),
        "avatarURL": row.get("avatar_url"),
        "countryCode": row.get("country_code"),
        "actualRank": row.get("actual_rank"),
        "predictedRank": row.get("predicted_rank"),
        "skill": row.get("skill"),
        "topPercent": row.get("top_percent"),
        "confidence": row.get("confidence"),
        "star": row.get("star"),
        "accuracyPercent": row.get("accuracy_percent"),
        "mods": [token for token in str(row.get("mods") or "NM").split(",") if token],
        "lengthSeconds": row.get("length_seconds"),
        "mapID": row.get("map_id"),
        "mapLink": row.get("map_link"),
        "beatmap": {
            "artist": row.get("artist") or "",
            "title": row.get("title") or "Unknown map",
            "version": row.get("version") or "",
            "creator": row.get("creator"),
        },
        "createdAt": _iso_datetime(row.get("created_at")),
    }
    if not reveal_answer:
        for key in (
            "player",
            "avatarURL",
            "countryCode",
            "actualRank",
            "predictedRank",
            "skill",
            "topPercent",
            "confidence",
        ):
            item.pop(key, None)
    return item


def challenge_feedback(actual_rank: int, guess_rank: int) -> tuple[bool, str, str, float]:
    ratio = max(actual_rank, guess_rank) / max(1, min(actual_rank, guess_rank))
    log_error = abs(math.log10(max(guess_rank, 1) / max(actual_rank, 1)))
    correct = ratio <= 1.10
    if correct:
        direction = "correct"
    elif actual_rank < guess_rank:
        direction = "better"
    else:
        direction = "worse"

    if correct:
        closeness = "exact"
    elif ratio <= 1.35:
        closeness = "very_close"
    elif ratio <= 2.0:
        closeness = "close"
    else:
        closeness = "far"
    return correct, direction, closeness, log_error


def _render_metadata_object(render: dict[str, Any]) -> dict[str, Any]:
    description = _clean_text(render.get("description")) or None
    title = _clean_text(render.get("title")) or None
    return {
        "description": description,
        "title": title,
        "star": _extract_star(title, description),
        "username": render.get("username"),
        "replayUsername": render.get("replayUsername"),
        "replayMods": render.get("replayMods"),
        "mapTitle": render.get("mapTitle"),
        "mapLength": render.get("mapLength"),
        "drainTime": render.get("drainTime"),
        "replayDifficulty": render.get("replayDifficulty"),
        "mapID": render.get("mapID"),
        "mapLink": render.get("mapLink"),
    }


async def submit_ordr_bytes(replay_bytes: bytes, username: str, replay_hash: str) -> int:
    data = {
        "skin": os.getenv("ORDR_SKIN", "whitecatCK1.0"),
        "resolution": os.getenv("ORDR_RESOLUTION", "960x540"),
        "showPPCounter": "false",
        "showScoreboard": "false",
        "showResultScreen": "true",
        "skip": "true",
        "customSkin": "false",
        "generateThumbnail": "true",
    }
    verification_key = os.getenv("ORDR_API_KEY")
    if verification_key:
        data["verificationKey"] = verification_key

    upload_name = sanitize_replay_filename(username, replay_hash)
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
        raise RuntimeError(
            f"o!rdr rejected replay (HTTP {response.status_code}, code {payload.get('errorCode')}): "
            f"{payload.get('message') or response.text[:240]}"
        )
    render_id = payload.get("renderID")
    if render_id is None:
        raise RuntimeError("o!rdr did not return a render ID")
    return int(render_id)


async def fetch_ordr_snapshot(render_id: int) -> dict[str, Any]:
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            ORDR_RENDER_URL,
            params={"renderID": int(render_id)},
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            raise RuntimeError(f"o!rdr status returned HTTP {response.status_code}")
        payload = response.json()
        renders = payload.get("renders") or []
        if not renders:
            return {"ready": False, "progress": "Queued", "render": None, "videoURL": None}

        render = dict(renders[0])
        error_code = int(render.get("errorCode") or 0)
        if error_code:
            raise RuntimeError(f"o!rdr render failed with error code {error_code}")

        dynlink_url: Any = None
        try:
            dynlink_response = await client.get(
                ORDR_DYNLINK_URL,
                params={"id": int(render_id)},
                headers={"Accept": "application/json"},
            )
            if dynlink_response.status_code == 200:
                dynlink_payload = dynlink_response.json()
                if isinstance(dynlink_payload, dict):
                    dynlink_url = dynlink_payload.get("url")
        except (httpx.HTTPError, ValueError):
            dynlink_url = None

    video_url = next(
        (
            normalized
            for candidate in (
                dynlink_url,
                render.get("videoUrl"),
                render.get("videoURL"),
                render.get("url"),
            )
            if (normalized := normalize_issou_video_url(candidate)) is not None
        ),
        None,
    )
    render_metadata = _render_metadata_object(render)
    return {
        "ready": bool(video_url and render_metadata.get("star") is not None),
        "progress": str(render.get("progress") or "Queued"),
        "render": render,
        "renderMetadata": render_metadata,
        "description": render_metadata.get("description"),
        "title": render_metadata.get("title"),
        "videoURL": video_url,
    }


async def wait_for_ordr_render(render_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + SEED_RENDER_TIMEOUT_SECONDS
    last_snapshot: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_snapshot = await fetch_ordr_snapshot(render_id)
        if last_snapshot.get("ready"):
            return last_snapshot
        await asyncio.sleep(2.0)
    progress = (last_snapshot or {}).get("progress") or "unknown"
    raise RuntimeError(f"o!rdr did not finish before cron timeout (last state: {progress})")


async def infer_cached_replay(
    cached: CachedReplay,
    render_metadata: dict[str, Any],
    *,
    description: str | None = None,
) -> dict[str, Any]:
    replay_accuracy = replay_accuracy_from_header(cached.header)
    replay_duration = replay_duration_from_summary(cached)
    description_text = _clean_text(description or render_metadata.get("description"))
    title_text = _clean_text(render_metadata.get("title"))
    parsed = parse_ordr_metadata(
        description=description_text,
        title=title_text,
        render_metadata=render_metadata,
        fallback_length_seconds=replay_duration,
        fallback_accuracy=replay_accuracy,
        fallback_player=cached.player,
    )
    metadata = parsed["map"]
    accuracy = replay_accuracy

    bundle = await get_model_bundle()
    score_pp = _finite_number(render_metadata.get("scorePP") or render_metadata.get("_scorePP"))
    score_match_quality = _finite_number(render_metadata.get("scoreMatchQuality")) or 0.0
    score_pp_source = "render_metadata" if score_pp is not None else "missing"
    beatmap_payload: dict[str, Any] | None = None
    beatmap_text: str | None = None
    parsed_beatmap: dict[str, Any] | None = None

    if bundle is not None and cached.events is not None and len(cached.events) >= 2:
        map_id = metadata.get("id")
        beatmap_payload, beatmap_text = await asyncio.gather(
            fetch_osu_beatmap(map_id),
            fetch_osu_beatmap_text(map_id),
        )
        if beatmap_text:
            try:
                parsed_beatmap = parse_osu_beatmap(beatmap_text)
            except Exception as exc:
                print(json.dumps({"event": "beatmap_parse_failed", "error": repr(exc)}), flush=True)
                parsed_beatmap = None
            if score_pp is None:
                local_pp = await asyncio.to_thread(
                    calculate_local_score_pp,
                    beatmap_text,
                    cached.header,
                    cached.display_mods,
                )
                if local_pp is not None:
                    score_pp = local_pp
                    score_match_quality = 1.0
                    score_pp_source = "local_rosu"
        if score_pp is None:
            score_pp, score_match_quality = await fetch_matching_score_pp(cached, map_id)
            if score_pp is not None:
                score_pp_source = "osu_api_match"

        status_value: float = 0.0
        status = (beatmap_payload or {}).get("status")
        if isinstance(status, (int, float)):
            status_value = float(status)
        elif isinstance(status, str):
            status_value = {
                "graveyard": -2.0,
                "wip": -1.0,
                "pending": 0.0,
                "ranked": 1.0,
                "approved": 2.0,
                "qualified": 3.0,
                "loved": 4.0,
            }.get(status.casefold(), 0.0)

        replay_object = {"header": cached.header, "events": cached.events}
        static_features = build_static_features_v2(
            replay_object,
            star=float(metadata["star"]),
            accuracy=accuracy,
            length_seconds=float(metadata["lengthSeconds"]),
            model_mods=cached.model_mods,
            beatmap=parsed_beatmap,
            score_pp=score_pp,
            score_match_quality=score_match_quality,
            map_ranked_status=status_value,
            map_max_combo=(beatmap_payload or {}).get("max_combo"),
            map_drain_seconds=(beatmap_payload or {}).get("hit_length")
            or (beatmap_payload or {}).get("drain"),
        )[None, :].astype(np.float32, copy=False)
        skill, uncertainty, member_predictions = await asyncio.to_thread(
            combine_bundle_predictions,
            bundle,
            static_features=static_features,
            cached=cached,
        )
        static_predictions = [
            prediction
            for prediction, entry in zip(member_predictions, bundle["models"])
            if str(entry.get("type") or "").casefold() == "static"
        ]
        base_skill = float(np.mean(static_predictions)) if static_predictions else float(np.mean(member_predictions))
        replay_correction = skill - base_skill
        replay_gate = 1.0
        ordinal = np.asarray(
            [1.0 / (1.0 + math.exp(-np.clip((skill - threshold) / 0.32, -30.0, 30.0))) for threshold in (1, 2, 3, 4, 5)],
            dtype=np.float64,
        )
        model_version = str(bundle["config"].get("version") or "v2-bundle")
        model_members = len(member_predictions)
    else:
        tabular_core = build_tabular_core(
            float(metadata["star"]),
            accuracy,
            float(metadata["lengthSeconds"]),
            cached.model_mods,
        )
        session = await get_model_session()
        outputs = await asyncio.to_thread(
            session.run,
            None,
            {
                "tabular_core": tabular_core,
                "event_sequence": cached.event_sequence,
                "action_windows": cached.action_windows,
                "replay_summary": cached.replay_summary,
            },
        )
        skill = float(np.asarray(outputs[0]).reshape(-1)[0])
        base_skill = float(np.asarray(outputs[1]).reshape(-1)[0])
        replay_correction = float(np.asarray(outputs[2]).reshape(-1)[0])
        replay_gate = float(np.asarray(outputs[3]).reshape(-1)[0])
        uncertainty = float(np.asarray(outputs[4]).reshape(-1)[0])
        ordinal = np.asarray(outputs[5], dtype=np.float64).reshape(-1)
        model_version = "legacy-replay-ensemble"
        model_members = 5

    if not all(math.isfinite(value) for value in (skill, base_skill, replay_correction, replay_gate, uncertainty)):
        raise RuntimeError("Model produced non-finite output")

    rank_percentile = min(1.0, max(1.0 / OSU_RANK_POPULATION, 10.0 ** (-skill)))
    predicted_rank = max(
        1,
        min(OSU_RANK_POPULATION, int(round(rank_percentile * OSU_RANK_POPULATION))),
    )
    return {
        "skill": skill,
        "baseSkill": base_skill,
        "replayCorrection": replay_correction,
        "replayGate": replay_gate,
        "uncertainty": uncertainty,
        "confidence": confidence_label(uncertainty),
        "rankPercentile": rank_percentile,
        "topPercent": 100.0 * rank_percentile,
        "predictedRank": predicted_rank,
        "accuracy": accuracy,
        "accuracyPercent": 100.0 * accuracy,
        "metadata": metadata,
        "descriptionFormat": parsed["descriptionFormat"],
        "parsedPlayer": parsed["player"],
        "ordinal": ordinal,
        "scorePP": score_pp,
        "scorePPSource": score_pp_source,
        "scoreMatchQuality": score_match_quality,
        "modelVersion": model_version,
        "modelMembers": model_members,
    }

def _cron_authorized(request: Request) -> bool:
    secret = os.getenv("CRON_SECRET")
    if not secret:
        return False
    supplied = request.headers.get("authorization") or ""
    return hmac.compare_digest(supplied, f"Bearer {secret}")


def _ranking_page_for_slot(slot: int, run_date: date, attempt: int) -> int:
    page_ranges = ((1, 20), (21, 200), (201, 1000))
    minimum, maximum = page_ranges[slot % len(page_ranges)]
    return _stable_number(
        f"seed-page:{run_date.isoformat()}:{slot}:{attempt}",
        minimum,
        maximum,
    )


async def find_seed_candidate(
    slot: int,
    run_date: date,
    *,
    selection_key: str | None = None,
) -> dict[str, Any]:
    selection_key = selection_key or f"cron:{run_date.isoformat()}:{slot}"
    for page_attempt in range(8):
        if selection_key.startswith("cron:"):
            page = _ranking_page_for_slot(slot, run_date, page_attempt)
        else:
            minimum, maximum = ((1, 60), (61, 350), (351, 1400))[slot % 3]
            page = _stable_number(f"fresh-page:{selection_key}:{page_attempt}", minimum, maximum)
        ranking = await fetch_rankings_page(page)
        if not ranking:
            continue
        ordered_users = sorted(
            ranking,
            key=lambda row: hashlib.sha256(
                f"candidate-user:{selection_key}:{page_attempt}:{(row.get('user') or {}).get('id')}".encode("utf-8")
            ).digest(),
        )
        for rank_row in ordered_users[:10]:
            user = rank_row.get("user") or {}
            try:
                user_id = int(user.get("id"))
            except (TypeError, ValueError):
                continue
            username = str(user.get("username") or "").strip()
            if not username:
                continue
            try:
                scores = await fetch_user_best_scores(user_id)
            except Exception as exc:
                print(json.dumps({"event": "seed_scores_failed", "userID": user_id, "error": repr(exc)}), flush=True)
                continue
            good_scores = [score for score in scores if _seed_score_is_good(score)]
            good_scores.sort(
                key=lambda score: hashlib.sha256(
                    f"candidate-score:{selection_key}:{_score_id(score)}".encode("utf-8")
                ).digest()
            )
            for score in good_scores[:12]:
                score_id = _score_id(score)
                if score_id is None:
                    continue
                try:
                    replay_bytes = await download_score_replay(score_id)
                    replay_hash = compute_replay_hash(replay_bytes)
                    if await asyncio.to_thread(submission_exists, replay_hash=replay_hash):
                        continue
                    cached = build_cached_replay_from_bytes(replay_bytes, replay_hash, keep_replay_bytes=True)
                except Exception as exc:
                    print(
                        json.dumps(
                            {"event": "seed_replay_rejected", "scoreID": score_id, "error": repr(exc)},
                            separators=(",", ":"),
                        ),
                        flush=True,
                    )
                    continue

                statistics = rank_row.get("statistics") or user.get("statistics") or {}
                actual_rank = rank_row.get("global_rank") or statistics.get("global_rank")
                try:
                    actual_rank = int(actual_rank) if actual_rank else None
                except (TypeError, ValueError):
                    actual_rank = None
                return {
                    "score": score,
                    "scoreID": score_id,
                    "cached": cached,
                    "replayBytes": replay_bytes,
                    "replayHash": replay_hash,
                    "user": {
                        "id": user_id,
                        "username": username,
                        "avatarURL": user.get("avatar_url"),
                        "countryCode": user.get("country_code"),
                        "globalRank": actual_rank,
                    },
                }
    raise RuntimeError("Could not find a fresh public score with a downloadable replay")


async def seed_gallery_once(slot: int) -> dict[str, Any]:
    eligible_before = await asyncio.to_thread(challenge_count)
    if eligible_before >= GALLERY_SEED_TARGET:
        return {
            "ok": True,
            "skipped": True,
            "reason": "seed_target_reached",
            "eligible": eligible_before,
            "target": GALLERY_SEED_TARGET,
        }

    run_date = datetime.now(timezone.utc).date()
    candidate = await find_seed_candidate(slot, run_date)
    cached: CachedReplay = candidate["cached"]
    replay_hash = candidate["replayHash"]
    replay_bytes = candidate["replayBytes"]
    render_id = await submit_ordr_bytes(replay_bytes, cached.player, replay_hash)
    snapshot = await wait_for_ordr_render(render_id)
    video_url = validate_video_url(str(snapshot["videoURL"]))
    render_metadata = dict(snapshot.get("renderMetadata") or {})
    score_pp = _finite_number(candidate.get("score", {}).get("pp"))
    if score_pp is not None:
        render_metadata["scorePP"] = score_pp
        render_metadata["scoreMatchQuality"] = 1.0
    inference = await infer_cached_replay(
        cached,
        render_metadata,
        description=snapshot.get("description"),
    )
    metadata = inference["metadata"]
    score = candidate["score"]
    user = candidate["user"]
    beatmap_payload = score.get("beatmap") or {}
    if score.get("beatmapset"):
        beatmap_payload = dict(beatmap_payload)
        beatmap_payload["beatmapset"] = score.get("beatmapset")
    thumbnail_url = _cover_url_from_beatmap_payload(beatmap_payload)
    if not thumbnail_url:
        osu_beatmap = await fetch_osu_beatmap(metadata.get("id"))
        thumbnail_url = _cover_url_from_beatmap_payload(osu_beatmap)

    public_id = make_public_id(replay_hash)
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
        "video_url": video_url,
        "thumbnail_url": thumbnail_url,
        "source": "cron",
        "published": True,
    }
    saved = await asyncio.to_thread(save_submission, record)
    if not saved:
        raise RuntimeError("Database did not save seeded replay")
    return {
        "ok": True,
        "skipped": False,
        "slot": slot,
        "publicID": public_id,
        "renderID": render_id,
        "scoreID": candidate["scoreID"],
        "player": cached.player,
        "actualRank": user.get("globalRank"),
        "predictedRank": inference["predictedRank"],
        "thumbnailURL": thumbnail_url,
        "eligibleBefore": eligible_before,
        "target": GALLERY_SEED_TARGET,
    }


async def generate_fresh_infinite_replay() -> dict[str, Any]:
    """Create one private challenge from a newly selected public score.

    The replay is never sampled from the gallery and is not published into it.
    It is stored privately only so later guess requests can reveal the answer.
    """
    selection_key = secrets.token_urlsafe(18)
    slot = secrets.randbelow(3)
    run_date = datetime.now(timezone.utc).date()
    candidate = await find_seed_candidate(
        slot,
        run_date,
        selection_key=selection_key,
    )
    cached: CachedReplay = candidate["cached"]
    replay_hash = candidate["replayHash"]
    replay_bytes = candidate["replayBytes"]
    render_id = await submit_ordr_bytes(replay_bytes, cached.player, replay_hash)
    snapshot = await wait_for_ordr_render(render_id)
    video_url = validate_video_url(str(snapshot["videoURL"]))
    render_metadata = dict(snapshot.get("renderMetadata") or {})
    score_pp = _finite_number(candidate.get("score", {}).get("pp"))
    if score_pp is not None:
        render_metadata["scorePP"] = score_pp
        render_metadata["scoreMatchQuality"] = 1.0
    inference = await infer_cached_replay(
        cached,
        render_metadata,
        description=snapshot.get("description"),
    )
    metadata = inference["metadata"]
    score = candidate["score"]
    user = candidate["user"]
    actual_rank = user.get("globalRank")
    if not actual_rank:
        raise RuntimeError("Selected public player no longer has a global rank")

    beatmap_payload = score.get("beatmap") or {}
    if score.get("beatmapset"):
        beatmap_payload = dict(beatmap_payload)
        beatmap_payload["beatmapset"] = score.get("beatmapset")
    thumbnail_url = _cover_url_from_beatmap_payload(beatmap_payload)
    if not thumbnail_url:
        osu_beatmap = await fetch_osu_beatmap(metadata.get("id"))
        thumbnail_url = _cover_url_from_beatmap_payload(osu_beatmap)

    public_id = make_public_id(replay_hash)
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
        "video_url": video_url,
        "thumbnail_url": thumbnail_url,
        "source": "infinite",
        "published": False,
    }
    saved = await asyncio.to_thread(save_submission, record)
    if not saved:
        raise RuntimeError("Database did not save private infinite replay")
    return gallery_item_from_row(saved, reveal_answer=False)


@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse(url="/index.html", status_code=307)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    bundle = await get_model_bundle()
    if bundle is not None:
        model_name = "bundle.json"
        model_version = str(bundle["config"].get("version") or "v2-bundle")
        inputs = {
            entry["file"]: {value.name: value.shape for value in entry["session"].get_inputs()}
            for entry in bundle["models"]
        }
    else:
        session = await get_model_session()
        model_name = MODEL_PATH.name
        model_version = "legacy-replay-ensemble"
        inputs = {value.name: value.shape for value in session.get_inputs()}
    return {
        "ok": True,
        "model": model_name,
        "modelVersion": model_version,
        "bundleConfigured": bundle is not None,
        "version": "4.0.1",
        "inputs": inputs,
        "databaseConfigured": database_configured(),
        "database": database_diagnostics(),
        "rankPopulation": OSU_RANK_POPULATION,
        "ordrApiKeyConfigured": bool(os.getenv("ORDR_API_KEY")),
        "osuApiConfigured": bool(os.getenv("OSU_CLIENT_ID") and os.getenv("OSU_CLIENT_SECRET")),
        "cacheSigningConfigured": bool(
            os.getenv("CACHE_SIGNING_SECRET")
            or os.getenv("ORDR_API_KEY")
            or os.getenv("OSU_CLIENT_SECRET")
        ),
        "cronConfigured": bool(os.getenv("CRON_SECRET")),
        "gallerySeedTarget": GALLERY_SEED_TARGET,
        "seedRenderTimeoutSeconds": SEED_RENDER_TIMEOUT_SECONDS,
    }


@app.get("/api/cron/seed-gallery/{slot}")
async def cron_seed_gallery(slot: int, request: Request) -> JSONResponse:
    if slot not in {0, 1, 2}:
        raise HTTPException(status_code=404, detail={"code": "invalid_seed_slot", "message": "Seed slot must be 0, 1, or 2."})
    if not _cron_authorized(request):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Missing or invalid cron authorization."})
    if not database_configured():
        raise HTTPException(status_code=503, detail={"code": "database_not_configured", "message": "Connect Postgres before seeding the gallery."})
    try:
        result = await seed_gallery_once(slot)
    except Exception as exc:
        print(
            json.dumps(
                {"event": "gallery_seed_failed", "slot": slot, "error": repr(exc)},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        raise HTTPException(
            status_code=502,
            detail={"code": "gallery_seed_failed", "message": str(exc)},
        ) from exc
    print(
        json.dumps(
            {"event": "gallery_seed_complete", **result},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )
    return JSONResponse(result)


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
        events=np.asarray(events, dtype=np.float32),
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
        "generateThumbnail": "true",
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

        render_title = render.get("title")
        render_star = _extract_star(render_title, description)
        render_metadata = {
            "description": description,
            "title": render_title,
            "star": render_star,
            "username": render.get("username"),
            "replayUsername": render.get("replayUsername"),
            "replayMods": render.get("replayMods"),
            "mapTitle": render.get("mapTitle"),
            "mapLength": render.get("mapLength"),
            "drainTime": render.get("drainTime"),
            "replayDifficulty": render.get("replayDifficulty"),
            "mapID": render.get("mapID"),
            "mapLink": render.get("mapLink"),
        }

    metadata_ready = render_metadata.get("star") is not None
    ready = bool(metadata_ready and video_url)
    client_progress = progress
    if metadata_ready and not video_url:
        client_progress = "Finalizing video link"

    print(
        json.dumps(
            {
                "event": "ordr_status",
                "renderID": render_id,
                "progress": progress,
                "ready": ready,
                "description": description,
                "title": render_metadata.get("title"),
                "star": render_metadata.get("star"),
                "mapLength": render_metadata.get("mapLength"),
                "mapTitle": render_metadata.get("mapTitle"),
                "replayDifficulty": render_metadata.get("replayDifficulty"),
                "replayMods": render_metadata.get("replayMods"),
                "videoURL": video_url,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )

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
            "renderMetadata": render_metadata,
        }
    )


@app.get("/api/gallery")
async def gallery(
    limit: int = Query(24, ge=1, le=60),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    if not database_configured():
        return JSONResponse({"ok": True, "configured": False, "items": [], "total": 0})
    try:
        rows, total = await asyncio.to_thread(list_gallery, limit, offset)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "database_error", "message": str(exc)}) from exc
    return JSONResponse({
        "ok": True,
        "configured": True,
        "items": [gallery_item_from_row(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@app.get("/api/gallery/{public_id}/thumbnail", include_in_schema=False)
async def gallery_thumbnail(public_id: str) -> RedirectResponse:
    if not database_configured():
        raise HTTPException(status_code=404)
    row = await asyncio.to_thread(get_submission, public_id)
    if not row:
        raise HTTPException(status_code=404)

    thumbnail_url = row.get("thumbnail_url")
    if not thumbnail_url:
        beatmap_payload = await fetch_osu_beatmap(row.get("map_id"))
        thumbnail_url = _cover_url_from_beatmap_payload(beatmap_payload)
        if thumbnail_url:
            try:
                await asyncio.to_thread(update_submission_thumbnail, public_id, thumbnail_url)
            except Exception as exc:
                print(json.dumps({"event": "thumbnail_backfill_failed", "error": repr(exc)}), flush=True)

    if not thumbnail_url:
        raise HTTPException(status_code=404)
    return RedirectResponse(
        url=str(thumbnail_url),
        status_code=307,
        headers={"Cache-Control": "public, max-age=86400, s-maxage=86400"},
    )


@app.get("/api/challenge/daily")
async def daily_challenge_api() -> JSONResponse:
    challenge_date = datetime.now(timezone.utc).date()
    if not database_configured():
        return JSONResponse({"ok": True, "available": False, "reason": "database_not_configured", "date": challenge_date.isoformat(), "replays": []})
    try:
        rows = await asyncio.to_thread(get_daily_challenge, challenge_date, 3)
        count = await asyncio.to_thread(challenge_count)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "database_error", "message": str(exc)}) from exc
    return JSONResponse({
        "ok": True,
        "available": len(rows) == 3,
        "date": challenge_date.isoformat(),
        "maxAttempts": MAX_CHALLENGE_ATTEMPTS,
        "rankPopulation": OSU_RANK_POPULATION,
        "eligibleReplays": count,
        "replays": [gallery_item_from_row(row, reveal_answer=False) for row in rows],
    })


@app.post("/api/challenge/infinite")
async def infinite_challenge_api() -> JSONResponse:
    if not database_configured():
        return JSONResponse({"ok": True, "available": False, "reason": "database_not_configured"})
    try:
        replay = await generate_fresh_infinite_replay()
    except Exception as exc:
        print(json.dumps({"event": "infinite_generation_failed", "error": repr(exc)}), flush=True)
        raise HTTPException(
            status_code=502,
            detail={"code": "infinite_generation_failed", "message": str(exc)},
        ) from exc
    return JSONResponse({
        "ok": True,
        "available": True,
        "fresh": True,
        "maxAttempts": MAX_CHALLENGE_ATTEMPTS,
        "rankPopulation": OSU_RANK_POPULATION,
        "replay": replay,
    })


def _soft_rank_position(rank: int, population: int = OSU_RANK_POPULATION) -> float:
    softness = 2_500.0
    maximum = max(2, int(population))
    clipped = max(1, min(maximum, int(rank)))
    scale = math.log1p((maximum - 1) / softness)
    return math.log1p((clipped - 1) / softness) / scale


def _soft_rank_from_position(position: float, population: int = OSU_RANK_POPULATION) -> int:
    softness = 2_500.0
    maximum = max(2, int(population))
    unit = max(0.0, min(1.0, float(position)))
    scale = math.log1p((maximum - 1) / softness)
    return max(1, min(maximum, int(round(1 + softness * math.expm1(unit * scale)))))


def rankbot_guess_for_attempt(
    *,
    actual_rank: int,
    predicted_rank: int,
    attempt: int,
    population: int = OSU_RANK_POPULATION,
) -> tuple[int, bool, str, str, float]:
    """Open with the model prediction, then binary-search the visible slider range."""
    maximum = max(2, int(population))
    actual = max(1, min(maximum, int(actual_rank)))
    opening = max(1, min(maximum, int(predicted_rank)))
    lower = 1
    upper = maximum

    guess = opening
    feedback = challenge_feedback(actual, guess)

    for turn in range(1, max(1, int(attempt)) + 1):
        if turn == 1:
            guess = opening
        else:
            left = _soft_rank_position(lower, maximum)
            right = _soft_rank_position(upper, maximum)
            guess = _soft_rank_from_position((left + right) / 2.0, maximum)
            guess = max(lower, min(upper, guess))

        feedback = challenge_feedback(actual, guess)
        correct, direction, _, _ = feedback

        if turn >= attempt or correct:
            return guess, *feedback

        if direction == "better":
            upper = min(upper, max(1, guess - 1))
        elif direction == "worse":
            lower = max(lower, min(maximum, guess + 1))

        if lower > upper:
            lower = upper = actual

    return guess, *feedback


@app.post("/api/challenge/guess")
async def challenge_guess(payload: ChallengeGuessPayload) -> JSONResponse:
    if not database_configured():
        raise HTTPException(status_code=503, detail={"code": "database_not_configured", "message": "Connect a database first."})
    try:
        row = await asyncio.to_thread(get_challenge_submission, payload.replay_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "database_error", "message": str(exc)}) from exc
    if not row or not row.get("actual_rank"):
        raise HTTPException(status_code=404, detail={"code": "challenge_missing", "message": "Challenge replay was not found."})

    mode = payload.mode.strip().lower()
    if mode == "daily":
        challenge_date = payload.challenge_date or datetime.now(timezone.utc).date()
        daily_rows = await asyncio.to_thread(get_daily_challenge, challenge_date, 3)
        if payload.replay_id not in {item["public_id"] for item in daily_rows}:
            raise HTTPException(status_code=400, detail={"code": "not_daily_replay", "message": "Replay is not part of that daily challenge."})
        challenge_key = challenge_date.isoformat()
    elif mode == "infinite":
        challenge_key = payload.replay_id
    else:
        raise HTTPException(status_code=400, detail={"code": "invalid_mode", "message": "Mode must be daily or infinite."})

    try:
        await asyncio.to_thread(
            record_challenge_guess,
            replay_id=payload.replay_id,
            visitor_id=payload.visitor_id,
            mode=mode,
            challenge_key=challenge_key,
            guess_rank=payload.guess_rank,
        )
    except Exception as exc:
        print(json.dumps({"event": "challenge_guess_store_failed", "error": repr(exc)}), flush=True)

    actual_rank = int(row["actual_rank"])
    predicted_rank = max(1, min(OSU_RANK_POPULATION, int(row.get("predicted_rank") or 1)))
    correct, direction, closeness, log_error = challenge_feedback(actual_rank, payload.guess_rank)
    bot_guess, bot_correct, bot_direction, bot_closeness, bot_log_error = rankbot_guess_for_attempt(
        actual_rank=actual_rank,
        predicted_rank=predicted_rank,
        attempt=payload.attempt,
    )

    reveal = correct or bot_correct or payload.attempt >= MAX_CHALLENGE_ATTEMPTS
    if correct and bot_correct:
        if abs(log_error - bot_log_error) < 1e-12:
            turn_winner = "tie"
        else:
            turn_winner = "player" if log_error < bot_log_error else "bot"
    elif correct:
        turn_winner = "player"
    elif bot_correct:
        turn_winner = "bot"
    else:
        turn_winner = "pending"

    response: dict[str, Any] = {
        "ok": True,
        "correct": correct,
        "direction": direction,
        "closeness": closeness,
        "attempt": payload.attempt,
        "maxAttempts": MAX_CHALLENGE_ATTEMPTS,
        "revealed": reveal,
        "logError": log_error,
        "botGuess": bot_guess,
        "botCorrect": bot_correct,
        "botDirection": bot_direction,
        "botCloseness": bot_closeness,
        "botLogError": bot_log_error,
        "turnWinner": turn_winner,
    }
    if reveal:
        distribution = await asyncio.to_thread(
            challenge_guess_distribution,
            replay_id=payload.replay_id,
            mode=mode,
            challenge_key=challenge_key,
            rank_population=OSU_RANK_POPULATION,
        )
        response.update({
            "actualRank": actual_rank,
            "predictedRank": predicted_rank,
            "player": row["player"],
            "avatarURL": row.get("avatar_url"),
            "distribution": distribution,
        })
    return JSONResponse(response)


@app.get("/api/challenge/{replay_id}/distribution")
async def challenge_distribution_api(
    replay_id: str,
    mode: str = Query("infinite", pattern="^(daily|infinite)$"),
    challenge_date: date | None = Query(default=None, alias="challengeDate"),
) -> JSONResponse:
    challenge_key = (
        (challenge_date or datetime.now(timezone.utc).date()).isoformat()
        if mode == "daily"
        else replay_id
    )
    distribution = await asyncio.to_thread(
        challenge_guess_distribution,
        replay_id=replay_id,
        mode=mode,
        challenge_key=challenge_key,
        rank_population=OSU_RANK_POPULATION,
    )
    return JSONResponse({"ok": True, "distribution": distribution})


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

    render_metadata = dict(payload.render_metadata or {})
    description_text = _clean_text(payload.description or render_metadata.get("description"))
    inference = await infer_cached_replay(
        cached,
        render_metadata,
        description=description_text,
    )
    metadata = inference["metadata"]
    skill = float(inference["skill"])
    base_skill = float(inference["baseSkill"])
    replay_correction = float(inference["replayCorrection"])
    replay_gate = float(inference["replayGate"])
    uncertainty = float(inference["uncertainty"])
    ordinal = np.asarray(inference["ordinal"], dtype=np.float64).reshape(-1)
    rank_percentile = float(inference["rankPercentile"])
    top_percent = float(inference["topPercent"])
    predicted_rank = int(inference["predictedRank"])
    confidence = str(inference["confidence"])
    accuracy = float(inference["accuracy"])
    one_in = max(1, int(round(1.0 / max(rank_percentile, 1e-12))))

    print(
        json.dumps(
            {
                "event": "prediction_complete",
                "renderID": payload.render_id,
                "modelVersion": inference["modelVersion"],
                "modelMembers": inference["modelMembers"],
                "star": metadata.get("star"),
                "lengthSeconds": metadata.get("lengthSeconds"),
                "scorePP": inference.get("scorePP"),
                "scorePPSource": inference.get("scorePPSource"),
                "scoreMatchQuality": inference.get("scoreMatchQuality"),
                "predictedRank": predicted_rank,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )

    header = cached.header
    parsed_player = str(inference.get("parsedPlayer") or cached.player)
    player_warning = None
    if parsed_player.casefold() != cached.player.casefold():
        player_warning = f"o!rdr reported {parsed_player}; replay header reported {cached.player}."

    osu_user = await fetch_osu_user(cached.player)
    actual_rank = int(osu_user["globalRank"]) if osu_user and osu_user.get("globalRank") else None
    gallery_saved = False
    gallery_id = None
    thumbnail_url = None
    if payload.publish:
        try:
            beatmap_payload = await fetch_osu_beatmap(metadata.get("id"))
            thumbnail_url = _cover_url_from_beatmap_payload(beatmap_payload)
        except Exception as exc:
            print(json.dumps({"event": "thumbnail_lookup_failed", "error": repr(exc)}), flush=True)

    if payload.publish and database_configured():
        public_id = make_public_id(replay_hash)
        record = {
            "public_id": public_id,
            "replay_hash": replay_hash,
            "render_id": payload.render_id,
            "player": cached.player,
            "osu_user_id": osu_user.get("id") if osu_user else None,
            "avatar_url": osu_user.get("avatarURL") if osu_user else None,
            "country_code": osu_user.get("countryCode") if osu_user else None,
            "actual_rank": actual_rank,
            "predicted_rank": predicted_rank,
            "skill": skill,
            "top_percent": top_percent,
            "confidence": confidence,
            "star": float(metadata["star"]),
            "accuracy_percent": 100.0 * accuracy,
            "mods": ",".join(cached.display_mods or ["NM"]),
            "artist": metadata.get("artist"),
            "title": metadata.get("title"),
            "version": metadata.get("version"),
            "creator": metadata.get("creator"),
            "length_seconds": float(metadata["lengthSeconds"]),
            "map_id": metadata.get("id"),
            "map_link": metadata.get("url"),
            "video_url": video_url,
            "thumbnail_url": thumbnail_url,
            "source": "upload",
            "published": True,
        }
        try:
            saved = await asyncio.to_thread(save_submission, record)
            gallery_saved = saved is not None
            gallery_id = public_id if gallery_saved else None
        except Exception as exc:
            print(json.dumps({"event": "gallery_save_failed", "error": repr(exc)}), flush=True)

    return JSONResponse(
        {
            "skill": skill,
            "rankPercentile": rank_percentile,
            "topPercent": top_percent,
            "oneInPlayers": one_in,
            "estimatedRank": predicted_rank,
            "predictedRank": predicted_rank,
            "rankPopulation": OSU_RANK_POPULATION,
            "actualRank": actual_rank,
            "rankError": (predicted_rank - actual_rank) if actual_rank else None,
            "baseSkill": base_skill,
            "replayCorrection": replay_correction,
            "replayGate": replay_gate,
            "uncertainty": uncertainty,
            "confidence": confidence,
            "ordinalProbabilities": {
                "gt1": float(ordinal[0]),
                "gt2": float(ordinal[1]),
                "gt3": float(ordinal[2]),
                "gt4": float(ordinal[3]),
                "gt5": float(ordinal[4]),
            },
            "modelVersion": inference["modelVersion"],
            "modelMembers": inference["modelMembers"],
            "scorePP": inference.get("scorePP"),
            "scoreMatchQuality": inference.get("scoreMatchQuality"),
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
            "renderDescription": description_text or _clean_text(render_metadata.get("title")),
            "descriptionFormat": inference["descriptionFormat"],
            "videoURL": video_url,
            "gallerySaved": gallery_saved,
            "galleryID": gallery_id,
            "thumbnailURL": thumbnail_url,
        }
    )

