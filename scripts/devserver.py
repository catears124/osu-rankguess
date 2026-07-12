#!/usr/bin/env python3
"""Serve public/ with mock osu!rankguess API responses for UI development."""
from __future__ import annotations

import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PUBLIC = Path(__file__).resolve().parent.parent / "public"
VIDEO = "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4"
POPULATION = 5_500_000


def replay(replay_id: str, artist: str, title: str, version: str, star: float, accuracy: float, mods: list[str]):
    return {
        "id": replay_id,
        "beatmap": {"artist": artist, "title": title, "version": version},
        "star": star,
        "accuracyPercent": accuracy,
        "mods": mods,
        "videoURL": VIDEO,
    }


ACTUAL = {"r1": 12_345, "r2": 850, "r3": 233_000, "inf1": 47_000}
DAILY = {
    "available": True,
    "rankPopulation": POPULATION,
    "date": "2026-07-11",
    "replays": [
        replay("r1", "Camellia", "FLYING OUT TO THE SKY", "EXTRA", 6.68, 88.41, ["HD", "DT"]),
        replay("r2", "xi", "Blue Zenith", "FOUR DIMENSIONS", 7.89, 97.02, ["NM"]),
        replay("r3", "Silentroom", "Nhelv", "Astral", 6.02, 99.10, ["HR"]),
    ],
}
GALLERY = {
    "configured": True,
    "total": 6,
    "items": [
        {
            "id": f"g{index}",
            "player": ["cookiezi", "WhiteCat", "mrekk", "Vaxei", "idke", "Aricin"][index],
            "source": "cron" if index % 2 else "upload",
            "beatmap": {"artist": "Artist", "title": f"Sample Map {index + 1}", "version": "Extra"},
            "star": round(5 + index * 0.4, 2),
            "mods": ["DT"] if index % 2 else ["NM"],
            "actualRank": (index + 1) * 137,
            "predictedRank": (index + 1) * 151,
            "videoURL": VIDEO,
            "thumbnailURL": "",
        }
        for index in range(6)
    ],
}


def distribution():
    counts = [0, 1, 2, 4, 9, 13, 8, 4, 1, 0, 0, 0]
    edges = [1, 4, 14, 48, 169, 594, 2089, 7348, 25846, 90925, 319801, 1_125_650, POPULATION]
    return {
        "count": sum(counts),
        "medianRank": 30_000,
        "q25Rank": 12_000,
        "q75Rank": 91_000,
        "geometricMeanRank": 32_500,
        "bins": [
            {"lower": edges[index], "upper": edges[index + 1], "count": count}
            for index, count in enumerate(counts)
        ],
    }


def guess_result(body):
    guess = int(body.get("guessRank", 1) or 1)
    attempt = int(body.get("attempt", 1) or 1)
    actual = ACTUAL.get(body.get("replayID"), 50_000)
    ratio = max(actual, guess) / max(1, min(actual, guess))
    correct = ratio <= 1.10
    revealed = correct or attempt >= 5
    result = {
        "correct": correct,
        "direction": "correct" if correct else "better" if actual < guess else "worse",
        "closeness": "exact" if correct else "very_close" if ratio <= 1.35 else "close" if ratio <= 2 else "far",
        "attempt": attempt,
        "maxAttempts": 5,
        "revealed": revealed,
    }
    if revealed:
        result.update(
            actualRank=actual,
            predictedRank=round(actual * 1.35),
            player="SamplePlayer",
            distribution=distribution(),
        )
    return result


def prediction():
    return {
        "player": "SamplePlayer",
        "predictedRank": 18_240,
        "actualRank": 15_990,
        "rankPopulation": POPULATION,
        "topPercent": 0.33,
        "accuracyPercent": 98.12,
        "mods": ["HD", "DT"],
        "confidence": "medium",
        "scorePP": 412.6,
        "modelVersion": "dev-mock",
        "beatmap": {"artist": "Camellia", "title": "FLYING OUT TO THE SKY", "version": "EXTRA", "star": 6.68},
        "videoURL": VIDEO,
        "gallerySaved": True,
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except (ValueError, TypeError):
            return {}

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        if path == "/api/challenge/daily":
            return self.send_json(DAILY)
        if path.startswith("/api/challenge/") and path.endswith("/distribution"):
            return self.send_json({"ok": True, "distribution": distribution()})
        if path == "/api/gallery":
            return self.send_json(GALLERY)
        if path == "/api/health":
            return self.send_json({"ok": True, "version": "dev-mock", "modelVersion": "dev-mock", "rankPopulation": POPULATION})
        if path == "/api/ordr/status":
            return self.send_json({"ready": True, "failed": False, "progress": "Done", "videoURL": VIDEO, "description": "mock", "renderMetadata": {}})
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        body = self.read_json()
        if path == "/api/challenge/guess":
            return self.send_json(guess_result(body))
        if path == "/api/challenge/infinite":
            return self.send_json({"available": True, "rankPopulation": POPULATION, "replay": replay("inf1", "Halozy", "Genryuu Kaiko", "Higan Torrent", 6.54, 96.4, ["NM"])})
        if path == "/api/replay/cache":
            return self.send_json({"replayHash": "mock-hash", "player": "SamplePlayer", "eventCount": 45_231, "cacheToken": "mock-token"})
        if path == "/api/ordr/render":
            return self.send_json({"renderID": 4337})
        if path == "/api/predict":
            return self.send_json(prediction())
        return self.send_json({})

    def log_message(self, *_args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock osu!rankguess UI server: http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
