#!/usr/bin/env python3
"""Train the osu!rankguess v2 production ensemble.

This is intentionally a maximum-effort trainer for the small osu3k-style
corpus.  It combines:

* locally calculated stable score PP plus leakage-audited public-score validation/fallback;
* automatic beatmap-id recovery from the canonical renderid or replay checksum;
* complete replay sequence and dense action-window encoders;
* map/replay alignment features, including timing-UR and aim-error proxies;
* repeated stratified group CV by player;
* diverse CatBoost static models;
* a multimodal PyTorch replay network with tail, ordinal and pairwise losses;
* out-of-fold non-negative blending and monotonic calibration;
* untouched test evaluation;
* ONNX export into the bundle format consumed directly by app.py.

Exact hit error is not stored in a standard .osr.  The "UR" and aim-error
features are explicitly proxies derived by aligning key-downs and cursor
positions to beatmap hit-object start times.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import random
import shutil
import sys
import time
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote

import numpy as np
import pandas as pd

try:
    import httpx
    import onnxruntime as ort
    import torch
    from catboost import CatBoostRegressor, Pool
    from scipy.optimize import nnls
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    from sklearn.model_selection import StratifiedGroupKFold
    from torch import nn
    from torch.nn import functional as F
    from torch.utils.data import DataLoader, Dataset
    from tqdm.auto import tqdm
except ImportError as exc:  # pragma: no cover - friendly setup error
    raise SystemExit(
        "Missing training dependencies. Run: pip install -r training/requirements.txt"
    ) from exc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from replay_features import (  # noqa: E402
    ACTION_WINDOW_COUNT,
    ACTION_WINDOW_LENGTH,
    EVENT_CHANNEL_NAMES,
    EVENT_SEQUENCE_LENGTH,
    STATIC_FEATURE_NAMES_V2,
    WINDOW_CHANNEL_NAMES,
    build_action_windows,
    build_event_sequence,
    build_static_features_v2,
    calculate_local_score_pp,
    parse_osr,
    parse_osu_beatmap,
)

warnings.filterwarnings("ignore", message=".*not writable.*")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaticSpec:
    name: str
    loss: str
    depth: int
    learning_rate: float
    l2_leaf_reg: float
    random_strength: float
    bagging_temperature: float


STATIC_SPECS = (
    StaticSpec("cb_rmse", "RMSE", 8, 0.025, 6.0, 0.35, 0.45),
    StaticSpec("cb_huber", "Huber:delta=0.45", 9, 0.022, 9.0, 0.55, 0.80),
    StaticSpec("cb_mae", "MAE", 8, 0.020, 12.0, 0.25, 0.25),
)

ORDINAL_THRESHOLDS = (1.0, 2.0, 3.0, 4.0, 5.0)
PP_FEATURE_NAMES = (
    "score_pp_available",
    "score_pp_log",
    "score_pp_per_star",
    "score_pp_per_hit_log",
    "score_pp_accuracy_interaction",
    "score_match_quality",
)
PP_FEATURE_INDICES = tuple(STATIC_FEATURE_NAMES_V2.index(name) for name in PP_FEATURE_NAMES)


@dataclass
class Preset:
    folds: int
    repeats: int
    cat_iterations: int
    deep_epochs: int
    deep_patience: int
    deep_seeds: tuple[int, ...]
    batch_size: int
    final_static_seeds: tuple[int, ...]
    final_deep_seeds: tuple[int, ...]


PRESETS = {
    "quick": Preset(3, 1, 900, 35, 8, (17,), 64, (101, 202, 303), (404,)),
    "balanced": Preset(5, 1, 1800, 75, 15, (17, 29), 64, (101, 202, 303), (404, 505)),
    "max": Preset(5, 2, 3600, 150, 28, (17, 29), 64, (101, 202, 303), (404, 505)),
}


# ---------------------------------------------------------------------------
# Utilities and metrics
# ---------------------------------------------------------------------------


def seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def read_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported data file: {path}")


def finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def ranked_status_value(value: Any) -> float:
    numeric = finite_float(value)
    if numeric is not None:
        return float(numeric)
    if isinstance(value, str):
        return {
            "graveyard": -2.0,
            "wip": -1.0,
            "pending": 0.0,
            "ranked": 1.0,
            "approved": 2.0,
            "qualified": 3.0,
            "loved": 4.0,
        }.get(value.strip().casefold(), 0.0)
    return 0.0


def rank_from_skill(skill: np.ndarray, population: int) -> np.ndarray:
    skill = np.asarray(skill, dtype=np.float64)
    return np.clip(population * np.power(10.0, -skill), 1.0, float(population))


def regression_metrics(y: np.ndarray, prediction: np.ndarray, population: int) -> dict[str, float]:
    y = np.asarray(y, dtype=np.float64)
    prediction = np.asarray(prediction, dtype=np.float64)
    true_rank = rank_from_skill(y, population)
    predicted_rank = rank_from_skill(prediction, population)
    rank_ratio = np.maximum(true_rank, predicted_rank) / np.maximum(1.0, np.minimum(true_rank, predicted_rank))
    absolute = np.abs(y - prediction)
    return {
        "mae_skill": float(mean_absolute_error(y, prediction)),
        "rmse_skill": float(math.sqrt(mean_squared_error(y, prediction))),
        "r2_skill": float(r2_score(y, prediction)),
        "median_ae_skill": float(np.median(absolute)),
        "median_rank_ratio": float(np.median(rank_ratio)),
        "p75_rank_ratio": float(np.quantile(rank_ratio, 0.75)),
        "within_25_percent": float(np.mean(rank_ratio <= 1.25)),
        "within_50_percent": float(np.mean(rank_ratio <= 1.50)),
        "within_2x": float(np.mean(rank_ratio <= 2.0)),
    }


def bucket_metrics(y: np.ndarray, prediction: np.ndarray, population: int) -> dict[str, Any]:
    rank = rank_from_skill(y, population)
    buckets = (
        ("rank_1_1k", 1, 1_000),
        ("rank_1k_10k", 1_000, 10_000),
        ("rank_10k_100k", 10_000, 100_000),
        ("rank_100k_1m", 100_000, 1_000_000),
        ("rank_1m_plus", 1_000_000, population + 1),
    )
    result: dict[str, Any] = {}
    for name, lower, upper in buckets:
        mask = (rank >= lower) & (rank < upper)
        result[name] = {
            "count": int(mask.sum()),
            **(regression_metrics(y[mask], prediction[mask], population) if mask.any() else {}),
        }
    return result


def selection_score(y: np.ndarray, prediction: np.ndarray, population: int) -> float:
    """CV objective that prevents a good middle from hiding terrible tails."""
    overall = math.sqrt(mean_squared_error(y, prediction))
    rank = rank_from_skill(y, population)
    tail_errors: list[float] = []
    for mask in (rank <= 10_000, rank >= 1_000_000):
        if int(mask.sum()) >= 8:
            tail_errors.append(math.sqrt(mean_squared_error(y[mask], prediction[mask])))
    return float(overall + 0.22 * (np.mean(tail_errors) if tail_errors else 0.0))


def target_bin_weights(y: np.ndarray, bins: int = 10) -> np.ndarray:
    y = np.asarray(y, dtype=np.float64)
    edges = np.unique(np.quantile(y, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return np.ones(len(y), dtype=np.float32)
    labels = np.clip(np.digitize(y, edges[1:-1], right=True), 0, len(edges) - 2)
    counts = np.bincount(labels, minlength=len(edges) - 1).astype(np.float64)
    inverse = 1.0 / np.sqrt(np.maximum(counts, 1.0))
    weights = inverse[labels]
    weights /= weights.mean()
    # Additional but bounded emphasis for truly elite and very low-ranked tails.
    lower, upper = np.quantile(y, [0.10, 0.90])
    weights *= np.where((y <= lower) | (y >= upper), 1.30, 1.0)
    return np.clip(weights / weights.mean(), 0.55, 2.5).astype(np.float32)


# ---------------------------------------------------------------------------
# Dataset schema, leakage audit and optional score-PP enrichment
# ---------------------------------------------------------------------------


def first_existing(columns: Iterable[str], candidates: Sequence[str]) -> str | None:
    available = set(columns)
    return next((candidate for candidate in candidates if candidate in available), None)


def mods_from_mask(mask: int | float | str | None) -> list[str]:
    try:
        value = int(float(mask))
    except (TypeError, ValueError, OverflowError):
        return []
    bits = (
        (1, "NF"), (2, "EZ"), (8, "HD"), (16, "HR"), (32, "SD"),
        (64, "DT"), (128, "RX"), (256, "HT"), (512, "NC"),
        (1024, "FL"), (4096, "SO"), (8192, "AP"), (16384, "PF"),
    )
    result = [token for bit, token in bits if value & bit]
    if "NC" in result and "DT" not in result:
        result.append("DT")
    if "PF" in result and "SD" not in result:
        result.append("SD")
    return result


def parse_mods(value: Any) -> list[str]:
    if isinstance(value, (int, np.integer)):
        return mods_from_mask(value)
    if isinstance(value, float) and np.isfinite(value) and value.is_integer():
        return mods_from_mask(value)
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    elif value is None or (isinstance(value, float) and np.isnan(value)):
        raw = []
    else:
        text = str(value).strip().upper()
        if not text or text in {"NM", "NONE", "[]"}:
            return []
        if text.isdigit():
            return mods_from_mask(text)
        try:
            decoded = json.loads(text)
            raw = decoded if isinstance(decoded, list) else [text]
        except Exception:
            raw = [token for token in text.replace("+", "").replace(" ", ",").split(",") if token]
            if len(raw) == 1 and len(raw[0]) > 2 and raw[0].isalnum():
                token = raw[0]
                known = ("NF", "EZ", "TD", "HD", "HR", "SD", "DT", "RX", "HT", "NC", "FL", "SO", "AP", "PF")
                parsed: list[str] = []
                while token:
                    matched = next((candidate for candidate in known if token.startswith(candidate)), None)
                    if not matched:
                        break
                    parsed.append(matched)
                    token = token[len(matched):]
                raw = parsed if not token else raw
    result: list[str] = []
    for item in raw:
        token = str(item.get("acronym") if isinstance(item, dict) else item).upper().strip()
        if token and token not in result:
            result.append(token)
    if "NC" in result and "DT" not in result:
        result.append("DT")
    if "PF" in result and "SD" not in result:
        result.append("SD")
    return result


def infer_target(df: pd.DataFrame, population: int) -> tuple[np.ndarray, str]:
    target_column = first_existing(df.columns, ("skill_target", "skill", "target"))
    if target_column:
        target = pd.to_numeric(df[target_column], errors="coerce").to_numpy(np.float64)
        return target, target_column
    percentile_column = first_existing(df.columns, ("global_rank_percent", "rank_percentile"))
    if percentile_column:
        percentile = pd.to_numeric(df[percentile_column], errors="coerce").to_numpy(np.float64)
        if np.nanmax(percentile) > 1.0:
            percentile /= 100.0
        return -np.log10(np.clip(percentile, 1.0 / population, 1.0)), percentile_column
    rank_column = first_existing(df.columns, ("global_rank", "rank"))
    if rank_column:
        rank = pd.to_numeric(df[rank_column], errors="coerce").to_numpy(np.float64)
        return -np.log10(np.clip(rank / population, 1.0 / population, 1.0)), rank_column
    raise ValueError("Need skill_target, global_rank_percent, or global_rank")


def select_pp_column(df: pd.DataFrame, requested: str, target: np.ndarray, group_column: str) -> str | None:
    preferred = ("score_pp", "play_pp", "performance_pp", "score_performance_pp", "pp_score")
    if requested != "auto":
        if requested.lower() in {"none", "off", ""}:
            return None
        if requested not in df.columns:
            raise ValueError(f"Requested PP column does not exist: {requested}")
        candidate = requested
    else:
        candidate = first_existing(df.columns, preferred)
        if candidate is None and "pp" in df.columns:
            candidate = "pp"

    if candidate is None:
        return None

    pp = pd.to_numeric(df[candidate], errors="coerce")
    valid = pp.notna() & np.isfinite(target)
    if valid.sum() < max(30, len(df) // 20):
        print(f"PP column {candidate!r} is too sparse; training with missing PP indicators.")
        return None

    if candidate == "pp":
        frame = pd.DataFrame({"group": df[group_column], "pp": pp})[valid]
        grouped = frame.groupby("group", dropna=False)["pp"].agg(["size", "nunique"])
        repeated = grouped[grouped["size"] >= 2]
        # One-row players cannot distinguish profile PP from score PP, so only
        # repeated players participate in this leakage diagnostic.
        profile_like_fraction = float(np.mean(repeated["nunique"] <= 1)) if len(repeated) else 0.0
        correlation = float(np.corrcoef(np.log1p(pp[valid]), target[valid])[0, 1])
        if profile_like_fraction >= 0.80 or abs(correlation) >= 0.965:
            raise ValueError(
                "Generic column 'pp' looks like player profile PP and would leak rank. "
                "Rename/provide per-score PP as score_pp or pass --pp-column none."
            )
    print(f"Using per-score PP column: {candidate}")
    return candidate


class ScorePPResolver:
    """Resolve per-play PP either by score ID or exact replay/beatmap matching.

    Profile total PP is never returned. Replay matching mirrors production:
    player + beatmap candidates are ranked by mods, accuracy, hit counts,
    combo, and legacy score. Low-confidence matches remain missing.
    """

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.cache: dict[str, Any] = {}
        if cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.cache = {}
        self.client_id = os.getenv("OSU_CLIENT_ID")
        self.client_secret = os.getenv("OSU_CLIENT_SECRET")
        self.token: str | None = None
        self.expires_at = 0.0
        self.client = httpx.Client(timeout=25.0, follow_redirects=True)
        self.lock = threading.RLock()
        self.api_slots = threading.BoundedSemaphore(3)
        self.user_cache: dict[str, int | None] = {}
        self.pending_writes = 0

    def _access_token(self) -> str:
        if not self.client_id or not self.client_secret:
            raise RuntimeError("OSU_CLIENT_ID and OSU_CLIENT_SECRET are required for PP enrichment")
        with self.lock:
            if self.token and time.time() < self.expires_at - 60:
                return self.token
            response = self.client.post(
                "https://osu.ppy.sh/oauth/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                    "scope": "public",
                },
            )
            response.raise_for_status()
            payload = response.json()
            self.token = str(payload["access_token"])
            self.expires_at = time.time() + float(payload.get("expires_in", 3600))
            return self.token

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _cached(self, key: str) -> Any:
        with self.lock:
            return copy.deepcopy(self.cache.get(key))

    def _store(self, key: str, value: Any) -> None:
        with self.lock:
            self.cache[key] = value
            self.pending_writes += 1
            if self.pending_writes >= 25:
                json_dump(self.cache_path, self.cache)
                self.pending_writes = 0

    def _user_id(self, username: str) -> int | None:
        normalized = username.strip().casefold()
        with self.lock:
            if normalized in self.user_cache:
                return self.user_cache[normalized]
        token = self._access_token()
        encoded = quote("@" + username.strip(), safe="")
        try:
            with self.api_slots:
                response = self.client.get(
                    f"https://osu.ppy.sh/api/v2/users/{encoded}/osu",
                    headers=self._headers(token),
                )
                time.sleep(0.05)
            user_id = int(response.json().get("id")) if response.status_code == 200 else None
        except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError):
            user_id = None
        with self.lock:
            self.user_cache[normalized] = user_id
        return user_id

    @staticmethod
    def _score_mods(score: dict[str, Any]) -> set[str]:
        raw = score.get("mods") or []
        result: set[str] = set()
        for item in raw:
            token = item if isinstance(item, str) else (item.get("acronym") if isinstance(item, dict) else None)
            if token:
                result.add(str(token).upper())
        if "NC" in result:
            result.add("DT")
        if "PF" in result:
            result.add("SD")
        return result - {"CL"}

    @staticmethod
    def _score_accuracy(score: dict[str, Any]) -> float:
        value = finite_float(score.get("accuracy"))
        if value is not None:
            return value / 100.0 if value > 1.0 else value
        stats = score.get("statistics") or {}
        n300 = int(stats.get("count_300", stats.get("great", 0)) or 0)
        n100 = int(stats.get("count_100", stats.get("ok", 0)) or 0)
        n50 = int(stats.get("count_50", stats.get("meh", 0)) or 0)
        miss = int(stats.get("count_miss", stats.get("miss", 0)) or 0)
        total = n300 + n100 + n50 + miss
        return (300 * n300 + 100 * n100 + 50 * n50) / (300 * total) if total else 0.0

    @staticmethod
    def _statistics(score: dict[str, Any]) -> dict[str, int]:
        stats = score.get("statistics") or {}
        return {
            "count_300": int(stats.get("count_300", stats.get("great", 0)) or 0),
            "count_100": int(stats.get("count_100", stats.get("ok", 0)) or 0),
            "count_50": int(stats.get("count_50", stats.get("meh", 0)) or 0),
            "count_miss": int(stats.get("count_miss", stats.get("miss", 0)) or 0),
        }

    @staticmethod
    def _replay_accuracy(header: dict[str, Any]) -> float:
        n300 = int(header.get("count_300", 0) or 0)
        n100 = int(header.get("count_100", 0) or 0)
        n50 = int(header.get("count_50", 0) or 0)
        miss = int(header.get("count_miss", 0) or 0)
        total = n300 + n100 + n50 + miss
        return (300 * n300 + 100 * n100 + 50 * n50) / (300 * total) if total else 0.0

    def _match_quality(self, score: dict[str, Any], parsed: dict, mods: list[str]) -> float:
        header = parsed["header"]
        replay_mods = set(parse_mods(mods)) - {"CL"}
        if "NC" in replay_mods:
            replay_mods.add("DT")
        if "PF" in replay_mods:
            replay_mods.add("SD")
        score_mods = self._score_mods(score)
        mod_score = 1.0 if replay_mods == score_mods else 0.0
        accuracy_score = math.exp(-abs(self._score_accuracy(score) - self._replay_accuracy(header)) / 0.0008)
        score_stats = self._statistics(score)
        stat_difference = sum(
            abs(int(header.get(name, 0) or 0) - score_stats[name])
            for name in ("count_300", "count_100", "count_50", "count_miss")
        )
        stats_score = math.exp(-stat_difference / 3.0)
        replay_combo = int(header.get("max_combo", 0) or 0)
        score_combo = int(score.get("max_combo", 0) or 0)
        combo_score = math.exp(-abs(score_combo - replay_combo) / max(2.0, replay_combo * 0.02))
        exact_score = float(int(score.get("score", 0) or 0) == int(header.get("score", -1) or -1))
        quality = 0.30 * mod_score + 0.25 * accuracy_score + 0.25 * stats_score + 0.10 * combo_score + 0.10 * exact_score
        if mod_score == 0.0:
            quality *= 0.35
        return float(np.clip(quality, 0.0, 1.0))

    def get(self, score_id: int) -> float | None:
        key = f"score:{int(score_id)}"
        cached = self._cached(key)
        if cached is not None:
            return float(cached) if finite_float(cached) is not None else None
        token = self._access_token()
        value: float | None = None
        for endpoint in (
            f"https://osu.ppy.sh/api/v2/scores/osu/{int(score_id)}",
            f"https://osu.ppy.sh/api/v2/scores/{int(score_id)}",
        ):
            try:
                with self.api_slots:
                    response = self.client.get(endpoint, headers=self._headers(token))
                    time.sleep(0.05)
            except httpx.HTTPError:
                continue
            if response.status_code == 200:
                value = finite_float(response.json().get("pp"))
                break
            if response.status_code not in {404, 422}:
                break
        self._store(key, value)
        return value

    @staticmethod
    def _extract_beatmap_id(payload: Any) -> int | None:
        """Extract a beatmap id from osu!api or o!rdr response variants."""
        if not isinstance(payload, dict):
            return None
        for key in ("id", "mapID", "mapId", "beatmapID", "beatmapId", "beatmap_id"):
            value = finite_float(payload.get(key))
            if value is not None and value > 0:
                return int(value)
        nested = payload.get("beatmap")
        if isinstance(nested, dict):
            value = finite_float(nested.get("id"))
            if value is not None and value > 0:
                return int(value)
        return None

    def resolve_beatmap_id(self, parsed: dict, render_id: int | None = None) -> int | None:
        """Resolve a map id from o!rdr render metadata or replay checksum.

        The user's canonical osu3k CSV only guarantees ``renderid`` and a
        replay path, so requiring a pre-existing beatmap_id column would make
        PP/map enrichment silently unusable.  o!rdr is tried first because it
        is public and already knows the rendered map.  The replay checksum is
        then resolved through osu!api as a robust fallback.
        """
        checksum = str((parsed.get("header") or {}).get("beatmap_hash") or "").strip()
        cache_key = f"beatmap-resolve:{int(render_id) if render_id else 0}:{checksum.casefold()}"
        checksum_key = f"beatmap-checksum:{checksum.casefold()}" if checksum else None
        for candidate_key in (checksum_key, cache_key):
            if not candidate_key:
                continue
            cached = self._cached(candidate_key)
            if isinstance(cached, dict):
                value = finite_float(cached.get("id"))
                if value is not None and value > 0:
                    return int(value)

        beatmap_id: int | None = None
        source = "none"
        if render_id:
            try:
                with self.api_slots:
                    response = self.client.get(
                        "https://apis.issou.best/ordr/renders",
                        params={"renderID": int(render_id)},
                    )
                    time.sleep(0.03)
                if response.status_code == 200:
                    payload = response.json()
                    rows = []
                    if isinstance(payload, dict):
                        data = payload.get("data")
                        if isinstance(data, dict):
                            rows = list(data.get("renders") or [])
                        elif isinstance(data, list):
                            rows = list(data)
                        if not rows:
                            rows = list(payload.get("renders") or [])
                    if rows:
                        beatmap_id = self._extract_beatmap_id(rows[0])
                        source = "ordr" if beatmap_id else source
            except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError):
                beatmap_id = None

        if beatmap_id is None and checksum:
            try:
                token = self._access_token()
                with self.api_slots:
                    response = self.client.get(
                        "https://osu.ppy.sh/api/v2/beatmaps/lookup",
                        params={"checksum": checksum},
                        headers=self._headers(token),
                    )
                    time.sleep(0.05)
                if response.status_code == 200:
                    beatmap_id = self._extract_beatmap_id(response.json())
                    source = "checksum" if beatmap_id else source
            except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError, RuntimeError):
                beatmap_id = None

        resolved_payload = {"id": beatmap_id, "source": source}
        self._store(cache_key, resolved_payload)
        if checksum_key and beatmap_id:
            self._store(checksum_key, resolved_payload)
        return beatmap_id

    def get_beatmap_metadata(self, beatmap_id: int) -> dict[str, Any] | None:
        key = f"beatmap-metadata:{int(beatmap_id)}"
        cached = self._cached(key)
        if isinstance(cached, dict):
            return cached
        try:
            token = self._access_token()
            with self.api_slots:
                response = self.client.get(
                    f"https://osu.ppy.sh/api/v2/beatmaps/{int(beatmap_id)}",
                    headers=self._headers(token),
                )
                time.sleep(0.05)
            payload = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, json.JSONDecodeError, RuntimeError):
            payload = None
        self._store(key, payload)
        return payload if isinstance(payload, dict) else None

    def get_replay(self, parsed: dict, beatmap_id: int, mods: list[str]) -> tuple[float | None, float]:
        header = parsed["header"]
        marker = json.dumps(
            {
                "player": str(header.get("player_name") or "").casefold(),
                "map": int(beatmap_id),
                "hash": str(header.get("replay_hash") or ""),
                "score": int(header.get("score", 0) or 0),
                "combo": int(header.get("max_combo", 0) or 0),
                "hits": [int(header.get(name, 0) or 0) for name in ("count_300", "count_100", "count_50", "count_miss")],
                "mods": sorted(parse_mods(mods)),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        key = "replay:" + hashlib.sha256(marker.encode()).hexdigest()
        cached = self._cached(key)
        if isinstance(cached, dict):
            return finite_float(cached.get("pp")), float(cached.get("quality") or 0.0)

        username = str(header.get("player_name") or "").strip()
        user_id = self._user_id(username) if username else None
        if not user_id:
            self._store(key, {"pp": None, "quality": 0.0})
            return None, 0.0
        token = self._access_token()
        try:
            with self.api_slots:
                response = self.client.get(
                    f"https://osu.ppy.sh/api/v2/beatmaps/{int(beatmap_id)}/scores/users/{int(user_id)}/all",
                    params={"mode": "osu", "legacy_only": 0},
                    headers=self._headers(token),
                )
                time.sleep(0.05)
        except httpx.HTTPError:
            self._store(key, {"pp": None, "quality": 0.0})
            return None, 0.0
        if response.status_code != 200:
            self._store(key, {"pp": None, "quality": 0.0})
            return None, 0.0
        payload = response.json()
        raw_scores = payload.get("scores") if isinstance(payload, dict) else payload
        scores = list(raw_scores or [])
        if not scores:
            self._store(key, {"pp": None, "quality": 0.0})
            return None, 0.0
        ranked = sorted(((self._match_quality(score, parsed, mods), score) for score in scores), key=lambda item: item[0], reverse=True)
        quality, score = ranked[0]
        pp = finite_float(score.get("pp"))
        if pp is None or pp < 0 or quality < 0.72:
            pp = None
        self._store(key, {"pp": pp, "quality": float(quality)})
        return pp, float(quality)

    def close(self) -> None:
        with self.lock:
            json_dump(self.cache_path, self.cache)
            self.pending_writes = 0
        self.client.close()


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


@dataclass
class Schema:
    replay_path: str
    star: str
    accuracy: str
    length: str
    mods: str | None
    group: str
    split: str | None
    beatmap_path: str | None
    beatmap_id: str | None
    score_id: str | None
    render_id: str | None


def resolve_schema(df: pd.DataFrame) -> Schema:
    required = {
        "replay_path": first_existing(df.columns, ("replay_path", "osr_path", "replay_file")),
        "star": first_existing(df.columns, ("star", "stars", "difficulty_rating")),
        "accuracy": first_existing(df.columns, ("map_acc", "accuracy", "acc")),
        "length": first_existing(df.columns, ("length_seconds", "map_length", "total_length")),
        "group": first_existing(df.columns, ("userid", "user_id", "osu_user_id", "player", "username")),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ValueError(f"Missing required data fields: {missing}")
    return Schema(
        replay_path=str(required["replay_path"]),
        star=str(required["star"]),
        accuracy=str(required["accuracy"]),
        length=str(required["length"]),
        mods=first_existing(df.columns, ("mods", "mod_list", "replay_mods")),
        group=str(required["group"]),
        split=first_existing(df.columns, ("split", "dataset_split")),
        beatmap_path=first_existing(df.columns, ("beatmap_path", "osu_path", "map_path")),
        beatmap_id=first_existing(df.columns, ("beatmap_id", "map_id", "beatmapID")),
        score_id=first_existing(df.columns, ("score_id", "legacy_score_id", "osu_score_id")),
        render_id=first_existing(df.columns, ("renderid", "render_id", "ordr_render_id")),
    )


def resolve_path(value: Any, data_root: Path) -> Path | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = data_root / path
    return path


def beatmap_cache_path(cache_root: Path, beatmap_id: int) -> Path:
    return cache_root / "beatmaps" / f"{beatmap_id}.osu"


def download_beatmap(beatmap_id: int, destination: Path) -> Path | None:
    if destination.exists() and destination.stat().st_size > 200:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        response = httpx.get(f"https://osu.ppy.sh/osu/{int(beatmap_id)}", timeout=20.0, follow_redirects=True)
        if response.status_code == 200 and "[HitObjects]" in response.text:
            destination.write_text(response.text, encoding="utf-8")
            return destination
    except httpx.HTTPError:
        pass
    return None


def row_fingerprint(
    row: pd.Series,
    schema: Schema,
    pp_value: float | None,
    enrich_score_pp: bool,
) -> str:
    replay_path = str(row[schema.replay_path])
    try:
        stat = Path(replay_path).stat()
        replay_marker = f"{replay_path}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        replay_marker = replay_path
    payload = {
        "version": 5,
        "replay": replay_marker,
        "star": finite_float(row[schema.star]),
        "accuracy": finite_float(row[schema.accuracy]),
        "length": finite_float(row[schema.length]),
        "mods": parse_mods(row[schema.mods]) if schema.mods else [],
        "beatmap_path": str(row[schema.beatmap_path]) if schema.beatmap_path else None,
        "beatmap_id": finite_float(row[schema.beatmap_id]) if schema.beatmap_id else None,
        "render_id": finite_float(row[schema.render_id]) if schema.render_id else None,
        "score_pp": pp_value,
        "enrich_score_pp": bool(enrich_score_pp),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def extract_one(
    index: int,
    row: pd.Series,
    schema: Schema,
    data_root: Path,
    cache_root: Path,
    pp_value: float | None,
    download_beatmaps: bool,
    pp_resolver: ScorePPResolver | None,
) -> tuple[int, np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    replay_path = resolve_path(row[schema.replay_path], data_root)
    if replay_path is None or not replay_path.exists():
        raise FileNotFoundError(f"Replay missing: {replay_path}")

    fingerprint = row_fingerprint(row, schema, pp_value, pp_resolver is not None)
    item_path = cache_root / "features" / f"{fingerprint}.npz"
    if item_path.exists():
        archive = np.load(item_path, allow_pickle=False)
        return (
            index,
            archive["static"].astype(np.float32),
            archive["event"].astype(np.float32),
            archive["windows"].astype(np.float32),
            json.loads(bytes(archive["metadata"].tolist()).decode("utf-8")),
        )

    parsed = parse_osr(replay_path)
    if int(parsed["header"]["mode"]) != 0:
        raise ValueError("Not an osu!standard replay")
    if len(parsed["events"]) < 2:
        raise ValueError("Replay contains fewer than two events")

    row_accuracy = float(row[schema.accuracy])
    if row_accuracy > 1.0:
        row_accuracy /= 100.0
    header = parsed["header"]
    total_hits = (
        int(header.get("count_300", 0))
        + int(header.get("count_100", 0))
        + int(header.get("count_50", 0))
        + int(header.get("count_miss", 0))
    )
    if total_hits > 0:
        accuracy = (
            300.0 * int(header.get("count_300", 0))
            + 100.0 * int(header.get("count_100", 0))
            + 50.0 * int(header.get("count_50", 0))
        ) / (300.0 * total_hits)
    else:
        accuracy = row_accuracy
    star = float(row[schema.star])
    length = float(row[schema.length])
    mods = parse_mods(row[schema.mods]) if schema.mods else []
    if not mods:
        mods = mods_from_mask(header.get("mods_mask", 0))
    if "RX" in mods or "AP" in mods:
        raise ValueError("Relax/Autopilot replay excluded")

    beatmap_path = resolve_path(row[schema.beatmap_path], data_root) if schema.beatmap_path else None
    beatmap_id: int | None = None
    if schema.beatmap_id:
        beatmap_number = finite_float(row[schema.beatmap_id])
        beatmap_id = int(beatmap_number) if beatmap_number and beatmap_number > 0 else None
    render_id_number = finite_float(row[schema.render_id]) if schema.render_id else None
    render_id = int(render_id_number) if render_id_number and render_id_number > 0 else None
    if beatmap_id is None and pp_resolver is not None:
        beatmap_id = pp_resolver.resolve_beatmap_id(parsed, render_id)

    map_metadata: dict[str, Any] | None = None
    if beatmap_id is not None and pp_resolver is not None:
        map_metadata = pp_resolver.get_beatmap_metadata(beatmap_id)
    if (beatmap_path is None or not beatmap_path.exists()) and beatmap_id and download_beatmaps:
        beatmap_path = download_beatmap(beatmap_id, beatmap_cache_path(cache_root, beatmap_id))

    beatmap = None
    beatmap_text: str | None = None
    if beatmap_path and beatmap_path.exists():
        beatmap_text = beatmap_path.read_text(encoding="utf-8", errors="replace")
        beatmap = parse_osu_beatmap(beatmap_text)

    score_match_quality = 1.0 if pp_value is not None and pp_value > 0 else 0.0
    score_pp_source = "column" if pp_value is not None and pp_value > 0 else "missing"
    if (pp_value is None or pp_value <= 0) and beatmap_text:
        local_pp = calculate_local_score_pp(beatmap_text, header, mods)
        if local_pp is not None:
            pp_value = local_pp
            score_match_quality = 1.0
            score_pp_source = "local_rosu"
    if (pp_value is None or pp_value <= 0) and pp_resolver is not None:
        score_id = finite_float(row[schema.score_id]) if schema.score_id else None
        try:
            if score_id and score_id > 0:
                pp_value = pp_resolver.get(int(score_id))
                score_match_quality = 1.0 if pp_value is not None and pp_value > 0 else 0.0
                if pp_value is not None and pp_value > 0:
                    score_pp_source = "osu_api_score_id"
            elif beatmap_id:
                pp_value, score_match_quality = pp_resolver.get_replay(parsed, beatmap_id, mods)
                if pp_value is not None and pp_value > 0:
                    score_pp_source = "osu_api_match"
        except Exception as exc:
            # PP is valuable but optional. A transient API failure must not
            # discard an otherwise valid replay from model training.
            score_match_quality = 0.0
            print(f"PP enrichment failed for row {index}: {exc!r}")

    static = build_static_features_v2(
        parsed,
        star=star,
        accuracy=accuracy,
        length_seconds=length,
        model_mods=mods,
        beatmap=beatmap,
        score_pp=pp_value,
        score_match_quality=score_match_quality,
        map_ranked_status=ranked_status_value(
            row.get("map_ranked_status")
            if row.get("map_ranked_status") is not None
            else (map_metadata or {}).get("status")
        ),
        map_max_combo=(
            finite_float(row.get("map_max_combo"))
            if finite_float(row.get("map_max_combo")) is not None
            else finite_float((map_metadata or {}).get("max_combo"))
        ),
        map_drain_seconds=(
            finite_float(row.get("map_drain_seconds"))
            if finite_float(row.get("map_drain_seconds")) is not None
            else finite_float((map_metadata or {}).get("hit_length"))
        ),
    )
    event = build_event_sequence(parsed["events"])
    windows = build_action_windows(parsed["events"])
    metadata = {
        "fingerprint": fingerprint,
        "replay_path": str(replay_path),
        "beatmap_path": str(beatmap_path) if beatmap_path else None,
        "beatmap_id": beatmap_id,
        "render_id": render_id,
        "beatmap_available": bool(beatmap and beatmap.get("available")),
        "score_pp_available": bool(pp_value is not None and pp_value > 0),
        "score_pp": float(pp_value) if pp_value is not None and pp_value > 0 else None,
        "score_pp_source": score_pp_source,
        "score_match_quality": float(score_match_quality),
        "event_count": int(len(parsed["events"])),
    }
    item_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    np.savez_compressed(
        item_path,
        static=static,
        event=event,
        windows=windows,
        metadata=np.frombuffer(metadata_bytes, dtype=np.uint8),
    )
    return index, static, event, windows, metadata


def build_feature_matrix(
    df: pd.DataFrame,
    schema: Schema,
    data_root: Path,
    cache_root: Path,
    pp_values: np.ndarray,
    download_beatmaps: bool,
    workers: int,
    pp_resolver: ScorePPResolver | None = None,
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray, list[dict[str, Any]]]:
    results: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]] = {}
    failures: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                extract_one,
                int(index),
                row,
                schema,
                data_root,
                cache_root,
                float(pp_values[index]) if np.isfinite(pp_values[index]) else None,
                download_beatmaps,
                pp_resolver,
            ): int(index)
            for index, row in df.iterrows()
        }
        for future in tqdm(as_completed(futures), total=len(futures), desc="replay features"):
            index = futures[future]
            try:
                _, static, event, windows, metadata = future.result()
                results[index] = (static, event, windows, metadata)
            except Exception as exc:
                failures.append({"index": index, "error": repr(exc), "path": str(df.loc[index, schema.replay_path])})

    if failures:
        json_dump(cache_root / "feature_failures.json", failures)
        print(f"Excluded {len(failures)} rows that could not be featurized.")
    valid_indices = sorted(results)
    clean = df.loc[valid_indices].reset_index(drop=True)
    static = np.stack([results[index][0] for index in valid_indices]).astype(np.float32)
    event = np.stack([results[index][1] for index in valid_indices]).astype(np.float32)
    windows = np.stack([results[index][2] for index in valid_indices]).astype(np.float32)
    metadata = [results[index][3] for index in valid_indices]
    return clean, static, event, windows, metadata


# ---------------------------------------------------------------------------
# Cross-validation
# ---------------------------------------------------------------------------


def stratification_labels(y: np.ndarray, bins: int = 10) -> np.ndarray:
    series = pd.Series(y)
    try:
        labels = pd.qcut(series.rank(method="first"), q=min(bins, len(series)), labels=False, duplicates="drop")
        return labels.to_numpy(np.int64)
    except ValueError:
        return np.zeros(len(y), dtype=np.int64)


def repeated_group_folds(
    y: np.ndarray,
    groups: np.ndarray,
    folds: int,
    repeats: int,
    seed: int,
) -> Iterable[tuple[int, int, np.ndarray, np.ndarray]]:
    labels = stratification_labels(y)
    for repeat in range(repeats):
        splitter = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed + 1009 * repeat)
        for fold, (train, valid) in enumerate(splitter.split(np.zeros(len(y)), labels, groups)):
            yield repeat, fold, train.astype(np.int64), valid.astype(np.int64)


# ---------------------------------------------------------------------------
# CatBoost family
# ---------------------------------------------------------------------------


def fit_catboost(
    spec: StaticSpec,
    x_train: np.ndarray,
    y_train: np.ndarray,
    w_train: np.ndarray,
    x_valid: np.ndarray,
    y_valid: np.ndarray,
    w_valid: np.ndarray,
    iterations: int,
    seed: int,
    use_gpu: bool,
) -> CatBoostRegressor:
    parameters: dict[str, Any] = {
        "loss_function": spec.loss,
        "eval_metric": "RMSE",
        "iterations": iterations,
        "depth": spec.depth,
        "learning_rate": spec.learning_rate,
        "l2_leaf_reg": spec.l2_leaf_reg,
        "random_strength": spec.random_strength,
        "bagging_temperature": spec.bagging_temperature,
        "bootstrap_type": "Bayesian",
        "random_seed": seed,
        "verbose": False,
        "allow_writing_files": False,
        "od_type": "Iter",
        "od_wait": max(120, iterations // 12),
    }
    if use_gpu:
        parameters.update({"task_type": "GPU", "devices": "0", "bootstrap_type": "Bayesian"})
    model = CatBoostRegressor(**parameters)
    model.fit(
        Pool(x_train, y_train, weight=w_train),
        eval_set=Pool(x_valid, y_valid, weight=w_valid),
        use_best_model=True,
        verbose=False,
    )
    return model


# ---------------------------------------------------------------------------
# Multimodal replay network
# ---------------------------------------------------------------------------


class ResidualMLPBlock(nn.Module):
    def __init__(self, width: int, dropout: float):
        super().__init__()
        self.norm = nn.LayerNorm(width)
        self.net = nn.Sequential(
            nn.Linear(width, width * 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(width * 2, width),
            nn.Dropout(dropout),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.net(self.norm(x))


class ResidualTCNBlock(nn.Module):
    def __init__(self, width: int, dilation: int, dropout: float):
        super().__init__()
        padding = dilation
        self.norm1 = nn.GroupNorm(8, width)
        self.conv1 = nn.Conv1d(width, width * 2, 3, padding=padding, dilation=dilation)
        self.norm2 = nn.GroupNorm(8, width)
        self.conv2 = nn.Conv1d(width, width, 1)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        residual = x
        value = self.conv1(F.gelu(self.norm1(x)))
        left, gate = value.chunk(2, dim=1)
        value = left * torch.sigmoid(gate)
        value = self.conv2(F.gelu(self.norm2(value)))
        return residual + self.dropout(value)


class SequenceEncoder(nn.Module):
    def __init__(self, channels: int, width: int, dilations: Sequence[int], dropout: float):
        super().__init__()
        self.input = nn.Conv1d(channels, width, 1)
        self.blocks = nn.Sequential(*(ResidualTCNBlock(width, dilation, dropout) for dilation in dilations))
        self.attention = nn.Sequential(
            nn.Conv1d(width, width // 2, 1),
            nn.GELU(),
            nn.Conv1d(width // 2, 1, 1),
        )
        self.output = nn.Sequential(
            nn.Linear(width * 3, width * 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(width * 2, width),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.blocks(self.input(x))
        weights = torch.softmax(self.attention(x), dim=-1)
        attentive = torch.sum(x * weights, dim=-1)
        mean = x.mean(dim=-1)
        maximum = x.amax(dim=-1)
        return self.output(torch.cat((attentive, mean, maximum), dim=-1))


class WindowSetEncoder(nn.Module):
    def __init__(self, channels: int, width: int, dropout: float):
        super().__init__()
        self.sequence = SequenceEncoder(channels, width, (1, 2, 4, 8), dropout)
        self.score = nn.Sequential(nn.Linear(width, width // 2), nn.Tanh(), nn.Linear(width // 2, 1))
        self.output = nn.Sequential(
            nn.Linear(width * 3, width * 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(width * 2, width),
        )

    def forward(self, windows: torch.Tensor) -> torch.Tensor:
        batch, count, channels, length = windows.shape
        encoded = self.sequence(windows.reshape(batch * count, channels, length)).reshape(batch, count, -1)
        weights = torch.softmax(self.score(encoded), dim=1)
        attentive = torch.sum(encoded * weights, dim=1)
        mean = encoded.mean(dim=1)
        maximum = encoded.amax(dim=1)
        return self.output(torch.cat((attentive, mean, maximum), dim=-1))


class RankGuessV2(nn.Module):
    def __init__(
        self,
        static_mean: np.ndarray,
        static_std: np.ndarray,
        event_mean: np.ndarray,
        event_std: np.ndarray,
        window_mean: np.ndarray,
        window_std: np.ndarray,
        width: int = 192,
        dropout: float = 0.14,
    ):
        super().__init__()
        self.register_buffer("static_mean", torch.tensor(static_mean, dtype=torch.float32))
        self.register_buffer("static_std", torch.tensor(static_std, dtype=torch.float32))
        self.register_buffer("event_mean", torch.tensor(event_mean, dtype=torch.float32).view(1, -1, 1))
        self.register_buffer("event_std", torch.tensor(event_std, dtype=torch.float32).view(1, -1, 1))
        self.register_buffer("window_mean", torch.tensor(window_mean, dtype=torch.float32).view(1, 1, -1, 1))
        self.register_buffer("window_std", torch.tensor(window_std, dtype=torch.float32).view(1, 1, -1, 1))

        self.static_input = nn.Sequential(
            nn.Linear(len(STATIC_FEATURE_NAMES_V2), width),
            nn.LayerNorm(width),
            nn.GELU(),
            nn.Dropout(dropout),
        )
        self.static_blocks = nn.Sequential(*(ResidualMLPBlock(width, dropout) for _ in range(4)))
        self.event_encoder = SequenceEncoder(len(EVENT_CHANNEL_NAMES), width, (1, 2, 4, 8, 16, 32), dropout)
        self.window_encoder = WindowSetEncoder(len(WINDOW_CHANNEL_NAMES), width, dropout)

        self.fusion = nn.Sequential(
            nn.Linear(width * 3, width * 2),
            nn.LayerNorm(width * 2),
            nn.GELU(),
            nn.Dropout(dropout),
            ResidualMLPBlock(width * 2, dropout),
            ResidualMLPBlock(width * 2, dropout),
        )
        self.gate = nn.Sequential(nn.Linear(width * 3, width), nn.GELU(), nn.Linear(width, 3))
        self.prediction = nn.Sequential(
            nn.Linear(width * 2, width),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(width, 1),
        )
        self.ordinal = nn.Linear(width * 2, len(ORDINAL_THRESHOLDS))

    def normalize(self, static: torch.Tensor, event: torch.Tensor, windows: torch.Tensor):
        static = torch.clamp((static - self.static_mean) / self.static_std, -10.0, 10.0)
        event = torch.clamp((event - self.event_mean) / self.event_std, -10.0, 10.0)
        windows = torch.clamp((windows - self.window_mean) / self.window_std, -10.0, 10.0)
        return static, event, windows

    def forward(self, static_features: torch.Tensor, event_sequence: torch.Tensor, action_windows: torch.Tensor):
        static_features, event_sequence, action_windows = self.normalize(static_features, event_sequence, action_windows)
        static_embedding = self.static_blocks(self.static_input(static_features))
        event_embedding = self.event_encoder(event_sequence)
        window_embedding = self.window_encoder(action_windows)
        components = torch.stack((static_embedding, event_embedding, window_embedding), dim=1)
        weights = torch.softmax(self.gate(torch.cat((static_embedding, event_embedding, window_embedding), dim=-1)), dim=-1)
        weighted = torch.sum(components * weights.unsqueeze(-1), dim=1)
        fused = self.fusion(torch.cat((weighted, event_embedding - window_embedding, static_embedding), dim=-1))
        prediction = self.prediction(fused).squeeze(-1)
        ordinal = self.ordinal(fused)
        return prediction, ordinal


class ReplayDataset(Dataset):
    def __init__(self, static: np.ndarray, event: np.ndarray, windows: np.ndarray, target: np.ndarray, weights: np.ndarray):
        self.static = static
        self.event = event
        self.windows = windows
        self.target = target
        self.weights = weights

    def __len__(self) -> int:
        return len(self.target)

    def __getitem__(self, index: int):
        return (
            torch.from_numpy(self.static[index]),
            torch.from_numpy(self.event[index]),
            torch.from_numpy(self.windows[index]),
            torch.tensor(self.target[index], dtype=torch.float32),
            torch.tensor(self.weights[index], dtype=torch.float32),
        )


class EMA:
    def __init__(self, model: nn.Module, decay: float = 0.995):
        self.decay = decay
        self.shadow = {name: value.detach().clone() for name, value in model.state_dict().items()}

    @torch.no_grad()
    def update(self, model: nn.Module) -> None:
        for name, value in model.state_dict().items():
            if value.dtype.is_floating_point:
                self.shadow[name].lerp_(value.detach(), 1.0 - self.decay)
            else:
                self.shadow[name].copy_(value)

    def state_dict(self) -> dict[str, torch.Tensor]:
        return {name: value.detach().clone() for name, value in self.shadow.items()}


def normalization_stats(static: np.ndarray, event: np.ndarray, windows: np.ndarray, indices: np.ndarray):
    static_mean = static[indices].mean(axis=0).astype(np.float32)
    static_std = static[indices].std(axis=0).astype(np.float32)
    event_mean = event[indices].mean(axis=(0, 2)).astype(np.float32)
    event_std = event[indices].std(axis=(0, 2)).astype(np.float32)
    window_mean = windows[indices].mean(axis=(0, 1, 3)).astype(np.float32)
    window_std = windows[indices].std(axis=(0, 1, 3)).astype(np.float32)
    return (
        static_mean,
        np.where(static_std < 1e-5, 1.0, static_std).astype(np.float32),
        event_mean,
        np.where(event_std < 1e-5, 1.0, event_std).astype(np.float32),
        window_mean,
        np.where(window_std < 1e-5, 1.0, window_std).astype(np.float32),
    )


def apply_pp_dropout(static: torch.Tensor, probability: float) -> torch.Tensor:
    if probability <= 0:
        return static
    mask = torch.rand(static.shape[0], device=static.device) < probability
    if not mask.any():
        return static
    static = static.clone()
    for feature_index in PP_FEATURE_INDICES:
        static[mask, feature_index] = 0.0
    return static


def pairwise_rank_loss(prediction: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    if len(target) < 4:
        return prediction.new_zeros(())
    permutation = torch.randperm(len(target), device=target.device)
    target_difference = target - target[permutation]
    prediction_difference = prediction - prediction[permutation]
    mask = target_difference.abs() >= 0.12
    if not mask.any():
        return prediction.new_zeros(())
    sign = target_difference[mask].sign()
    return F.softplus(-sign * prediction_difference[mask] / 0.25).mean()


def training_loss(prediction, ordinal_logits, target, weights):
    squared = (prediction - target).square()
    weighted_mse = (squared * weights).mean()
    huber = (F.smooth_l1_loss(prediction, target, beta=0.30, reduction="none") * weights).mean()
    ordinal_target = torch.stack([(target > threshold).float() for threshold in ORDINAL_THRESHOLDS], dim=-1)
    ordinal = F.binary_cross_entropy_with_logits(ordinal_logits, ordinal_target)
    pairwise = pairwise_rank_loss(prediction, target)
    return 0.68 * weighted_mse + 0.17 * huber + 0.09 * ordinal + 0.06 * pairwise


@torch.inference_mode()
def predict_deep(model: RankGuessV2, static, event, windows, indices, batch_size, device) -> np.ndarray:
    model.eval()
    predictions: list[np.ndarray] = []
    for start in range(0, len(indices), batch_size):
        batch = indices[start:start + batch_size]
        prediction, _ = model(
            torch.from_numpy(static[batch]).to(device),
            torch.from_numpy(event[batch]).to(device),
            torch.from_numpy(windows[batch]).to(device),
        )
        predictions.append(prediction.float().cpu().numpy())
    return np.concatenate(predictions).astype(np.float64)


def train_deep_fold(
    static: np.ndarray,
    event: np.ndarray,
    windows: np.ndarray,
    y: np.ndarray,
    weights: np.ndarray,
    train_indices: np.ndarray,
    valid_indices: np.ndarray,
    preset: Preset,
    seed: int,
    population: int,
    device: torch.device,
) -> tuple[dict[str, torch.Tensor], tuple[np.ndarray, ...], np.ndarray, int]:
    seed_everything(seed)
    stats = normalization_stats(static, event, windows, train_indices)
    model = RankGuessV2(*stats).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=7e-4, weight_decay=3e-4, betas=(0.9, 0.98))
    steps_per_epoch = max(1, math.ceil(len(train_indices) / preset.batch_size))
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer,
        max_lr=1.8e-3,
        epochs=preset.deep_epochs,
        steps_per_epoch=steps_per_epoch,
        pct_start=0.12,
        div_factor=12.0,
        final_div_factor=150.0,
    )
    scaler = torch.amp.GradScaler("cuda", enabled=device.type == "cuda")
    dataset = ReplayDataset(static[train_indices], event[train_indices], windows[train_indices], y[train_indices], weights[train_indices])
    generator = torch.Generator().manual_seed(seed)
    loader = DataLoader(
        dataset,
        batch_size=preset.batch_size,
        shuffle=True,
        generator=generator,
        num_workers=0,
        pin_memory=device.type == "cuda",
        drop_last=False,
    )
    ema = EMA(model)
    best_state = copy.deepcopy(model.state_dict())
    best_score = float("inf")
    best_epoch = 0
    stale = 0

    for epoch in range(preset.deep_epochs):
        model.train()
        for static_batch, event_batch, window_batch, target_batch, weight_batch in loader:
            static_batch = static_batch.to(device, non_blocking=True)
            event_batch = event_batch.to(device, non_blocking=True)
            window_batch = window_batch.to(device, non_blocking=True)
            target_batch = target_batch.to(device, non_blocking=True)
            weight_batch = weight_batch.to(device, non_blocking=True)
            # The model must remain useful if the public API cannot match PP.
            if random.random() < 0.55:
                mask = torch.rand(len(static_batch), device=device) < 0.25
                if mask.any():
                    static_batch = static_batch.clone()
                    for feature_index in PP_FEATURE_INDICES:
                        static_batch[mask, feature_index] = 0.0
            optimizer.zero_grad(set_to_none=True)
            with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=device.type == "cuda"):
                prediction, ordinal = model(static_batch, event_batch, window_batch)
                loss = training_loss(prediction, ordinal, target_batch, weight_batch)
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 4.0)
            scaler.step(optimizer)
            scaler.update()
            scheduler.step()
            ema.update(model)

        evaluation_model = copy.deepcopy(model)
        evaluation_model.load_state_dict(ema.state_dict())
        validation_prediction = predict_deep(
            evaluation_model, static, event, windows, valid_indices, 256, device
        )
        score = selection_score(y[valid_indices], validation_prediction, population)
        if score < best_score - 1e-4:
            best_score = score
            best_epoch = epoch + 1
            best_state = ema.state_dict()
            stale = 0
        else:
            stale += 1
        del evaluation_model
        if stale >= preset.deep_patience:
            break

    model.load_state_dict(best_state)
    validation_prediction = predict_deep(model, static, event, windows, valid_indices, 256, device)
    return best_state, stats, validation_prediction, best_epoch


# ---------------------------------------------------------------------------
# Blending, calibration and exports
# ---------------------------------------------------------------------------


def fit_blend(y: np.ndarray, matrix: np.ndarray) -> tuple[np.ndarray, dict[str, Any], np.ndarray]:
    # Centering lets NNLS fit useful relative weights while calibration handles
    # the final intercept/slope and monotonic tail correction.
    weights, _ = nnls(matrix, y)
    if weights.sum() <= 1e-12:
        weights = np.ones(matrix.shape[1], dtype=np.float64)
    weights /= weights.sum()
    raw = matrix @ weights
    design = np.column_stack((np.ones(len(raw)), raw))
    intercept, slope = np.linalg.lstsq(design, y, rcond=None)[0]
    slope = float(np.clip(slope, 0.65, 1.35))
    linear = float(intercept) + slope * raw

    isotonic = IsotonicRegression(increasing=True, out_of_bounds="clip", y_min=float(np.min(y)), y_max=float(np.max(y)))
    calibrated = isotonic.fit_transform(linear, y)
    calibration = {
        "intercept": float(intercept),
        "slope": slope,
        "xThresholds": [float(value) for value in isotonic.X_thresholds_],
        "yThresholds": [float(value) for value in isotonic.y_thresholds_],
    }
    return weights, calibration, np.asarray(calibrated, dtype=np.float64)


def apply_calibration(raw: np.ndarray, calibration: dict[str, Any]) -> np.ndarray:
    value = calibration["intercept"] + calibration["slope"] * np.asarray(raw, dtype=np.float64)
    return np.interp(value, calibration["xThresholds"], calibration["yThresholds"])


def crossfit_meta_blend(
    y: np.ndarray,
    matrix: np.ndarray,
    groups: np.ndarray,
    seed: int,
) -> np.ndarray:
    """Cross-fit the level-2 blend so CV reporting includes no meta leakage."""
    labels = stratification_labels(y)
    splitter = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=seed)
    prediction = np.full(len(y), np.nan, dtype=np.float64)
    for train, valid in splitter.split(np.zeros(len(y)), labels, groups):
        fold_weights, fold_calibration, _ = fit_blend(y[train], matrix[train])
        raw = matrix[valid] @ fold_weights
        prediction[valid] = apply_calibration(raw, fold_calibration)
    if not np.isfinite(prediction).all():
        raise RuntimeError("Cross-fitted meta predictions are incomplete")
    return prediction


def export_catboost_onnx(model: CatBoostRegressor, path: Path) -> None:
    model.save_model(
        str(path),
        format="onnx",
        export_parameters={
            "onnx_domain": "ai.osu.rankguess",
            "onnx_model_version": 2,
            "onnx_doc_string": "osu!rankguess v2 static regressor",
        },
    )


def export_deep_onnx(model: RankGuessV2, path: Path) -> None:
    model.eval().cpu()
    static = torch.zeros(1, len(STATIC_FEATURE_NAMES_V2), dtype=torch.float32)
    event = torch.zeros(1, len(EVENT_CHANNEL_NAMES), EVENT_SEQUENCE_LENGTH, dtype=torch.float32)
    windows = torch.zeros(1, ACTION_WINDOW_COUNT, len(WINDOW_CHANNEL_NAMES), ACTION_WINDOW_LENGTH, dtype=torch.float32)
    torch.onnx.export(
        model,
        (static, event, windows),
        str(path),
        input_names=("static_features", "event_sequence", "action_windows"),
        output_names=("skill", "ordinal_logits"),
        opset_version=18,
        do_constant_folding=True,
        external_data=False,
        dynamo=False,
        dynamic_axes={
            "static_features": {0: "batch"},
            "event_sequence": {0: "batch"},
            "action_windows": {0: "batch"},
            "skill": {0: "batch"},
            "ordinal_logits": {0: "batch"},
        },
    )


def verify_onnx(path: Path, feeds: dict[str, np.ndarray]) -> float:
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    mapped: dict[str, np.ndarray] = {}
    for model_input in session.get_inputs():
        lower = model_input.name.casefold()
        if "static" in lower or lower == "features":
            mapped[model_input.name] = feeds["static"]
        elif "event" in lower:
            mapped[model_input.name] = feeds["event"]
        elif "window" in lower or "action" in lower:
            mapped[model_input.name] = feeds["windows"]
        else:
            # CatBoost may choose a generated input name.
            mapped[model_input.name] = feeds["static"]
    value = float(np.asarray(session.run(None, mapped)[0]).reshape(-1)[0])
    if not math.isfinite(value):
        raise RuntimeError(f"Non-finite ONNX result from {path}")
    return value


# ---------------------------------------------------------------------------
# Main training pipeline
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--preset", choices=PRESETS, default="max")
    parser.add_argument("--population", type=int, default=5_500_000)
    parser.add_argument("--pp-column", default="auto")
    parser.add_argument("--enrich-score-pp", action="store_true")
    parser.add_argument("--download-beatmaps", action="store_true")
    parser.add_argument("--workers", type=int, default=min(12, os.cpu_count() or 4))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--gpu-catboost", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    seed_everything(args.seed)
    preset = PRESETS[args.preset]
    data_path = args.data.resolve()
    data_root = (args.data_root or data_path.parent).resolve()
    output = args.output.resolve()
    cache_root = (args.cache or (output / "cache")).resolve()
    model_dir = output / "model"
    if output.exists() and args.overwrite:
        shutil.rmtree(output)
    model_dir.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)

    print("=" * 90)
    print("osu!rankguess v2 maximum-accuracy trainer")
    print("=" * 90)
    print("data:", data_path)
    print("output:", output)
    print("preset:", args.preset, asdict(preset))
    print("device:", "cuda" if torch.cuda.is_available() else "cpu")

    raw_df = read_table(data_path).reset_index(drop=True)
    schema = resolve_schema(raw_df)
    target, target_source = infer_target(raw_df, args.population)
    raw_df["__target"] = target
    raw_df = raw_df[np.isfinite(raw_df["__target"].to_numpy(np.float64))].reset_index(drop=True)
    target = raw_df["__target"].to_numpy(np.float64)

    pp_column = select_pp_column(raw_df, args.pp_column, target, schema.group)
    pp_values = (
        pd.to_numeric(raw_df[pp_column], errors="coerce").to_numpy(np.float64)
        if pp_column
        else np.full(len(raw_df), np.nan, dtype=np.float64)
    )

    pp_resolver: ScorePPResolver | None = None
    if args.enrich_score_pp or args.download_beatmaps:
        # beatmap_id is optional: the resolver can recover it from the canonical
        # renderid column or the replay's beatmap checksum.
        pp_resolver = ScorePPResolver(cache_root / "score_pp_api_cache.json")

    try:
        clean_df, static, event, windows, feature_metadata = build_feature_matrix(
            raw_df,
            schema,
            data_root,
            cache_root,
            pp_values,
            args.download_beatmaps,
            args.workers,
            pp_resolver,
        )
    finally:
        if pp_resolver is not None:
            pp_resolver.close()
    # build_feature_matrix resets the row index after filtering parse failures;
    # the copied hidden target column remains aligned with the feature tensors.
    y = clean_df["__target"].to_numpy(np.float64)
    groups = clean_df[schema.group].astype(str).fillna("unknown").to_numpy()
    weights = target_bin_weights(y)

    if schema.split:
        split = clean_df[schema.split].astype(str).str.lower()
        source_test_mask = split.eq("test").to_numpy()
        if not np.any(source_test_mask):
            raise ValueError("The source split column exists but contains no test rows")
        # A random row split can put the same player in train and test, which
        # substantially overstates generalization for a rank-prediction task.
        # Preserve every source-test player, but move all of that player's rows
        # into the locked test set so the final benchmark is player-disjoint.
        locked_test_groups = np.unique(groups[source_test_mask])
        test_mask = np.isin(groups, locked_test_groups)
        moved = int(np.sum(test_mask & ~source_test_mask))
        print(
            "source test rows:", int(source_test_mask.sum()),
            "player-disjoint locked test rows:", int(test_mask.sum()),
            "rows moved out of training:", moved,
        )
    else:
        # Group-safe ~14% holdout when the source did not already define one.
        labels = stratification_labels(y)
        splitter = StratifiedGroupKFold(n_splits=7, shuffle=True, random_state=args.seed + 9901)
        _, test_indices = next(splitter.split(np.zeros(len(y)), labels, groups))
        test_mask = np.zeros(len(y), dtype=bool)
        test_mask[test_indices] = True
    train_pool = np.flatnonzero(~test_mask)
    test_indices = np.flatnonzero(test_mask)
    if len(test_indices) == 0:
        raise ValueError("The test split is empty")

    print("rows:", len(clean_df), "train/CV:", len(train_pool), "test:", len(test_indices))
    print("players:", len(np.unique(groups)), "PP coverage:", float(np.mean(static[:, STATIC_FEATURE_NAMES_V2.index("score_pp_available")] > 0)))
    print("beatmap coverage:", float(np.mean(static[:, STATIC_FEATURE_NAMES_V2.index("beatmap_available")] > 0)))

    pool_y = y[train_pool]
    pool_groups = groups[train_pool]
    model_names = [spec.name for spec in STATIC_SPECS] + [f"deep_seed_{seed}" for seed in preset.deep_seeds]
    oof_sum = np.zeros((len(train_pool), len(model_names)), dtype=np.float64)
    oof_count = np.zeros_like(oof_sum)
    best_cat_iterations: dict[str, list[int]] = {spec.name: [] for spec in STATIC_SPECS}
    best_deep_epochs: dict[str, list[int]] = {f"deep_seed_{seed}": [] for seed in preset.deep_seeds}
    fold_reports: list[dict[str, Any]] = []
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    for repeat, fold, local_train, local_valid in repeated_group_folds(
        pool_y, pool_groups, preset.folds, preset.repeats, args.seed
    ):
        train_indices = train_pool[local_train]
        valid_indices = train_pool[local_valid]
        print(f"\nCV repeat {repeat + 1}/{preset.repeats} fold {fold + 1}/{preset.folds}")
        fold_predictions: dict[str, np.ndarray] = {}

        for spec_index, spec in enumerate(STATIC_SPECS):
            model = fit_catboost(
                spec,
                static[train_indices],
                y[train_indices],
                weights[train_indices],
                static[valid_indices],
                y[valid_indices],
                weights[valid_indices],
                preset.cat_iterations,
                args.seed + repeat * 10_000 + fold * 100 + spec_index,
                args.gpu_catboost,
            )
            prediction = np.asarray(model.predict(static[valid_indices]), dtype=np.float64)
            column = model_names.index(spec.name)
            oof_sum[local_valid, column] += prediction
            oof_count[local_valid, column] += 1
            fold_predictions[spec.name] = prediction
            best_cat_iterations[spec.name].append(max(50, int(model.get_best_iteration() or preset.cat_iterations) + 1))
            del model

        for deep_seed in preset.deep_seeds:
            name = f"deep_seed_{deep_seed}"
            state, stats, prediction, best_epoch = train_deep_fold(
                static,
                event,
                windows,
                y,
                weights,
                train_indices,
                valid_indices,
                preset,
                args.seed + deep_seed + repeat * 10_000 + fold * 101,
                args.population,
                device,
            )
            column = model_names.index(name)
            oof_sum[local_valid, column] += prediction
            oof_count[local_valid, column] += 1
            fold_predictions[name] = prediction
            best_deep_epochs[name].append(best_epoch)
            del state, stats
            if device.type == "cuda":
                torch.cuda.empty_cache()

        fold_matrix = np.column_stack([fold_predictions[name] for name in model_names])
        equal_prediction = fold_matrix.mean(axis=1)
        fold_reports.append({
            "repeat": repeat,
            "fold": fold,
            "trainRows": len(train_indices),
            "validRows": len(valid_indices),
            "equalBlend": regression_metrics(y[valid_indices], equal_prediction, args.population),
            "tail": bucket_metrics(y[valid_indices], equal_prediction, args.population),
        })
        print(fold_reports[-1]["equalBlend"])

    if np.any(oof_count == 0):
        raise RuntimeError("Some OOF predictions were never populated")
    oof_matrix = oof_sum / oof_count
    blend_weights, calibration, deployment_fit_oof = fit_blend(pool_y, oof_matrix)
    meta_oof = crossfit_meta_blend(pool_y, oof_matrix, pool_groups, args.seed + 7717)
    oof_report = {
        "models": model_names,
        "weights": {name: float(weight) for name, weight in zip(model_names, blend_weights)},
        "metrics": regression_metrics(pool_y, meta_oof, args.population),
        "tail": bucket_metrics(pool_y, meta_oof, args.population),
        "deploymentFitMetrics": regression_metrics(pool_y, deployment_fit_oof, args.population),
        "calibration": calibration,
        "metaBlendCrossFitted": True,
    }
    print("\nOOF cross-fitted meta blend")
    print(json.dumps(oof_report["metrics"], indent=2))
    json_dump(output / "cv_report.json", {"oof": oof_report, "folds": fold_reports})
    pd.DataFrame({
        "row": train_pool,
        "target": pool_y,
        **{name: oof_matrix[:, index] for index, name in enumerate(model_names)},
        "blend_crossfit": meta_oof,
        "blend_deployment_fit": deployment_fit_oof,
    }).to_csv(output / "oof_predictions.csv", index=False)

    # Final static models: one per diverse loss/spec.
    bundle_models: list[dict[str, Any]] = []
    final_test_predictions: list[np.ndarray] = []
    for spec_index, spec in enumerate(STATIC_SPECS):
        iterations = int(np.median(best_cat_iterations[spec.name]))
        seed = preset.final_static_seeds[spec_index % len(preset.final_static_seeds)]
        parameters: dict[str, Any] = {
            "loss_function": spec.loss,
            "iterations": iterations,
            "depth": spec.depth,
            "learning_rate": spec.learning_rate,
            "l2_leaf_reg": spec.l2_leaf_reg,
            "random_strength": spec.random_strength,
            "bagging_temperature": spec.bagging_temperature,
            "bootstrap_type": "Bayesian",
            "random_seed": seed,
            "verbose": False,
            "allow_writing_files": False,
        }
        if args.gpu_catboost:
            parameters.update({"task_type": "GPU", "devices": "0"})
        model = CatBoostRegressor(**parameters)
        model.fit(Pool(static[train_pool], y[train_pool], weight=weights[train_pool]), verbose=False)
        filename = f"{spec.name}.onnx"
        export_catboost_onnx(model, model_dir / filename)
        final_test_predictions.append(np.asarray(model.predict(static[test_indices]), dtype=np.float64))
        bundle_models.append({
            "file": filename,
            "type": "static",
            "weight": float(blend_weights[model_names.index(spec.name)]),
            "cvBestIterations": iterations,
        })

    # Final deep models, trained on all CV data for median selected epochs.
    all_stats = normalization_stats(static, event, windows, train_pool)
    for deep_seed in preset.final_deep_seeds:
        # Average the OOF weights of the deep family across final deep seeds.
        deep_weight = float(sum(blend_weights[model_names.index(f"deep_seed_{seed}")] for seed in preset.deep_seeds) / len(preset.final_deep_seeds))
        epochs = max(10, int(np.median([epoch for values in best_deep_epochs.values() for epoch in values])))
        seed_everything(args.seed + deep_seed)
        model = RankGuessV2(*all_stats).to(device)
        optimizer = torch.optim.AdamW(model.parameters(), lr=7e-4, weight_decay=3e-4, betas=(0.9, 0.98))
        loader = DataLoader(
            ReplayDataset(static[train_pool], event[train_pool], windows[train_pool], y[train_pool], weights[train_pool]),
            batch_size=preset.batch_size,
            shuffle=True,
            generator=torch.Generator().manual_seed(args.seed + deep_seed),
            num_workers=0,
            pin_memory=device.type == "cuda",
        )
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, epochs * len(loader)), eta_min=2e-6)
        scaler = torch.amp.GradScaler("cuda", enabled=device.type == "cuda")
        ema = EMA(model)
        for _ in tqdm(range(epochs), desc=f"final deep {deep_seed}"):
            model.train()
            for static_batch, event_batch, window_batch, target_batch, weight_batch in loader:
                static_batch = static_batch.to(device)
                event_batch = event_batch.to(device)
                window_batch = window_batch.to(device)
                target_batch = target_batch.to(device)
                weight_batch = weight_batch.to(device)
                pp_mask = torch.rand(len(static_batch), device=device) < 0.20
                if pp_mask.any():
                    static_batch = static_batch.clone()
                    for index in PP_FEATURE_INDICES:
                        static_batch[pp_mask, index] = 0.0
                optimizer.zero_grad(set_to_none=True)
                with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=device.type == "cuda"):
                    prediction, ordinal = model(static_batch, event_batch, window_batch)
                    loss = training_loss(prediction, ordinal, target_batch, weight_batch)
                scaler.scale(loss).backward()
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), 4.0)
                scaler.step(optimizer)
                scaler.update()
                scheduler.step()
                ema.update(model)
        model.load_state_dict(ema.state_dict())
        test_prediction = predict_deep(model, static, event, windows, test_indices, 256, device)
        final_test_predictions.append(test_prediction)
        filename = f"deep_{deep_seed}.onnx"
        export_deep_onnx(model, model_dir / filename)
        bundle_models.append({
            "file": filename,
            "type": "deep",
            "weight": deep_weight,
            "epochs": epochs,
        })
        del model
        if device.type == "cuda":
            torch.cuda.empty_cache()

    # Match final model ordering to bundle ordering.  Static predictions were
    # appended first; deep predictions follow final_deep_seeds.  Deep family
    # OOF weights are shared equally among final deep members.
    final_weights = np.asarray([entry["weight"] for entry in bundle_models], dtype=np.float64)
    if final_weights.sum() <= 0:
        final_weights[:] = 1.0
    final_weights /= final_weights.sum()
    for entry, weight in zip(bundle_models, final_weights):
        entry["weight"] = float(weight)
    raw_test = np.column_stack(final_test_predictions) @ final_weights
    calibrated_test = apply_calibration(raw_test, calibration)
    test_report = {
        "metrics": regression_metrics(y[test_indices], calibrated_test, args.population),
        "tail": bucket_metrics(y[test_indices], calibrated_test, args.population),
        "count": int(len(test_indices)),
    }
    print("\nUNTOUCHED TEST")
    print(json.dumps(test_report, indent=2))

    residual_floor = float(max(0.035, np.quantile(np.abs(pool_y - meta_oof), 0.50)))
    bundle = {
        "version": "rankguess-v2-max",
        "target": "skill=-log10(global_rank/population)",
        "rankPopulation": int(args.population),
        "staticFeatureNames": list(STATIC_FEATURE_NAMES_V2),
        "models": bundle_models,
        "calibration": calibration,
        "uncertaintyFloor": residual_floor,
        "training": {
            "preset": args.preset,
            "targetSource": target_source,
            "ppColumn": pp_column,
            "trainRows": int(len(train_pool)),
            "testRows": int(len(test_indices)),
            "groupColumn": schema.group,
            "testSplitUntouched": True,
            "timingAndAimFeaturesAreProxies": True,
        },
        "oofMetrics": oof_report["metrics"],
        "testMetrics": test_report["metrics"],
    }
    json_dump(model_dir / "bundle.json", bundle)
    json_dump(output / "test_report.json", test_report)
    pd.DataFrame({
        "row": test_indices,
        "target": y[test_indices],
        "prediction": calibrated_test,
        "true_rank": rank_from_skill(y[test_indices], args.population),
        "predicted_rank": rank_from_skill(calibrated_test, args.population),
    }).to_csv(output / "test_predictions.csv", index=False)

    # Verify every exported model with one real test row.
    sample = test_indices[:1]
    verification = {}
    for entry in bundle_models:
        verification[entry["file"]] = verify_onnx(
            model_dir / entry["file"],
            {
                "static": static[sample].astype(np.float32),
                "event": event[sample].astype(np.float32),
                "windows": windows[sample].astype(np.float32),
            },
        )
    json_dump(output / "onnx_verification.json", verification)

    print("\nDone.")
    print("Copy every file from", model_dir, "into the website's model/ directory, preserving bundle.json.")
    print("Test metrics:", test_report["metrics"])


if __name__ == "__main__":
    main()
