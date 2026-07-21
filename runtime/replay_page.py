from __future__ import annotations

import asyncio
import html
import json
import re
from string import Template
from typing import Any
from urllib.parse import urljoin

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from backend import database as _database

_INSTALLED = False
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
_PAGE = Template("""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="theme-color" content="#050506" />
  <meta name="color-scheme" content="dark" />
  <meta name="robots" content="index, follow, max-video-preview:-1" />
  <title>$title</title>
  <link rel="canonical" href="$canonical" />
  <link rel="stylesheet" href="/replay-page.css?v=1" />
  <meta name="description" content="$description" />
  <meta property="og:type" content="video.other" />
  <meta property="og:site_name" content="osu!rankguess" />
  <meta property="og:title" content="$title" />
  <meta property="og:description" content="$description" />
  <meta property="og:url" content="$canonical" />
  <meta property="og:image" content="$thumbnail" />
  <meta property="og:image:secure_url" content="$thumbnail" />
  <meta property="og:video" content="$video" />
  <meta property="og:video:url" content="$video" />
  <meta property="og:video:secure_url" content="$video" />
  <meta property="og:video:type" content="video/mp4" />
  <meta property="og:video:width" content="960" />
  <meta property="og:video:height" content="540" />
  <meta name="twitter:card" content="player" />
  <meta name="twitter:title" content="$title" />
  <meta name="twitter:description" content="$description" />
  <meta name="twitter:image" content="$thumbnail" />
  <meta name="twitter:player" content="$canonical" />
  <meta name="twitter:player:width" content="960" />
  <meta name="twitter:player:height" content="540" />
  <meta name="twitter:player:stream" content="$video" />
  <meta name="twitter:player:stream:content_type" content="video/mp4" />
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <a class="brand" href="/gallery"><span>osu!</span>rankguess</a>
      <div class="top-actions">
        <a class="button" href="/gallery">back to gallery</a>
        <button class="button" id="copyLink" type="button">copy link</button>
      </div>
    </header>
    <section class="stage">
      <video id="replayVideo" src="$video" poster="$thumbnail" controls autoplay playsinline preload="auto"></video>
      <button class="sound-gate" id="soundGate" type="button" hidden><span>play with sound</span></button>
      <aside class="spoiler" id="spoilerPanel">
        <div id="hiddenState">
          <p class="kicker">spoiler mode</p>
          <h1>mystery replay</h1>
          <p>Watch first. The player, map, actual rank, and model rank stay hidden until you reveal them.</p>
          <button class="reveal-card" id="reveal" type="button">
            <span>actual + model ranks hidden<small>click to reveal</small></span>
            <strong>-</strong>
          </button>
        </div>
        <div class="result" id="resultState">
          <p class="kicker">replay result</p>
          <h1 id="player"></h1>
          <p class="map" id="map"></p>
          <div class="ranks">
            <div><span>actual</span><strong id="actual"></strong></div>
            <div><span>model</span><strong id="model"></strong></div>
            <div><span>ratio</span><strong id="ratio"></strong></div>
          </div>
        </div>
      </aside>
    </section>
  </main>
  <script id="replayData" type="application/json">$data</script>
  <script src="/replay-page.js?v=1"></script>
</body>
</html>""")


def _origin(request: Request) -> str:
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    host = forwarded_host or (request.headers.get("host") or request.url.netloc)
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
    scheme = forwarded_proto or request.url.scheme or "https"
    return f"{scheme}://{host}".rstrip("/")


def _absolute(origin: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return f"https:{text}"
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return urljoin(f"{origin}/", text.lstrip("/"))


def _safe_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _render_replay_html(row: dict[str, Any], *, origin: str, public_id: str) -> str:
    canonical = f"{origin}/replay/{public_id}"
    video_url = _absolute(origin, row.get("video_url"))
    thumbnail_url = _absolute(
        origin,
        row.get("thumbnail_url") or f"/api/gallery/{public_id}/thumbnail",
    )
    if not video_url:
        raise HTTPException(status_code=404, detail="Replay video is unavailable")

    replay = {
        "id": public_id,
        "player": row.get("player") or "Unknown player",
        "actualRank": row.get("actual_rank"),
        "predictedRank": row.get("predicted_rank"),
        "star": row.get("star"),
        "mods": [token for token in str(row.get("mods") or "NM").split(",") if token],
        "beatmap": {
            "artist": row.get("artist") or "",
            "title": row.get("title") or "Unknown map",
            "version": row.get("version") or "",
        },
        "videoURL": video_url,
        "thumbnailURL": thumbnail_url,
        "canonicalURL": canonical,
    }
    esc = lambda value: html.escape(str(value or ""), quote=True)
    return _PAGE.substitute(
        title=esc("osu!rankguess replay"),
        description=esc("Can you guess this osu! player's rank?"),
        canonical=esc(canonical),
        thumbnail=esc(thumbnail_url),
        video=esc(video_url),
        data=_safe_json(replay),
    )


def _install_route() -> None:
    if getattr(FastAPI, "_rankguess_replay_page_patch", False):
        return
    original_init = FastAPI.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        title = kwargs.get("title") or getattr(self, "title", "")
        if title != "osu!rankguess":
            return

        @self.get(
            "/api/replay-page/{public_id}",
            response_class=HTMLResponse,
            include_in_schema=False,
        )
        async def replay_page(public_id: str, request: Request) -> HTMLResponse:
            if not _PUBLIC_ID.fullmatch(public_id):
                raise HTTPException(status_code=404)
            row = await asyncio.to_thread(_database.get_submission, public_id)
            if not row or not bool(row.get("published")):
                raise HTTPException(status_code=404)
            body = _render_replay_html(row, origin=_origin(request), public_id=public_id)
            return HTMLResponse(
                body,
                headers={
                    "Cache-Control": "public, max-age=60, s-maxage=300, stale-while-revalidate=3600",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    FastAPI.__init__ = patched_init
    FastAPI._rankguess_replay_page_patch = True


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_route()
    _INSTALLED = True
