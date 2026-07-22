from __future__ import annotations

import asyncio
import html
import json
import re
from string import Template
from typing import Any, AsyncIterator
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from backend import database as _database

_INSTALLED = False
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
_CONTENT_RANGE = re.compile(r"^bytes\s+\d+-\d+/(\d+|\*)$", re.IGNORECASE)
_VIDEO_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
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
  <link rel="alternate" type="video/mp4" href="$embed_video" />
  <link rel="stylesheet" href="/replay-page.css?v=1" />
  <meta name="description" content="$description" />
  <meta property="og:type" content="video.other" />
  <meta property="og:site_name" content="osu!rankguess" />
  <meta property="og:title" content="$title" />
  <meta property="og:description" content="$description" />
  <meta property="og:url" content="$canonical" />
  <meta property="og:image" content="$thumbnail" />
  <meta property="og:image:secure_url" content="$thumbnail" />
  <meta property="og:video" content="$embed_video" />
  <meta property="og:video:url" content="$embed_video" />
  <meta property="og:video:secure_url" content="$embed_video" />
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
  <meta name="twitter:player:stream" content="$embed_video" />
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
      <video id="replayVideo" src="$player_video" poster="$thumbnail" controls autoplay playsinline preload="auto"></video>
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


def _source_video_url(row: dict[str, Any]) -> str:
    source = str(row.get("video_url") or "").strip()
    parsed = urlparse(source)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=404, detail="Replay video is unavailable")
    return source


def _embed_video_url(origin: str, public_id: str) -> str:
    return f"{origin}/replay/{public_id}/video.mp4"


def _render_replay_html(row: dict[str, Any], *, origin: str, public_id: str) -> str:
    canonical = f"{origin}/replay/{public_id}"
    player_video_url = _source_video_url(row)
    embed_video_url = _embed_video_url(origin, public_id)
    thumbnail_url = _absolute(
        origin,
        row.get("thumbnail_url") or f"/api/gallery/{public_id}/thumbnail",
    )

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
        "videoURL": player_video_url,
        "embedVideoURL": embed_video_url,
        "thumbnailURL": thumbnail_url,
        "canonicalURL": canonical,
    }
    esc = lambda value: html.escape(str(value or ""), quote=True)
    return _PAGE.substitute(
        title=esc("osu!rankguess replay"),
        description=esc("Can you guess this osu! player's rank?"),
        canonical=esc(canonical),
        thumbnail=esc(thumbnail_url),
        player_video=esc(player_video_url),
        embed_video=esc(embed_video_url),
        data=_safe_json(replay),
    )


async def _published_submission(public_id: str) -> dict[str, Any]:
    if not _PUBLIC_ID.fullmatch(public_id):
        raise HTTPException(status_code=404)
    row = await asyncio.to_thread(_database.get_submission, public_id)
    if not row or not bool(row.get("published")):
        raise HTTPException(status_code=404)
    return row


def _upstream_request_headers(request: Request) -> dict[str, str]:
    headers = {
        "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.1",
        "Accept-Encoding": "identity",
        "User-Agent": "osu-rankguess-replay-embed/1.0",
    }
    range_header = (request.headers.get("range") or "").strip()
    if range_header:
        headers["Range"] = range_header
    if_range = (request.headers.get("if-range") or "").strip()
    if if_range:
        headers["If-Range"] = if_range
    return headers


def _video_response_headers(upstream: httpx.Response, public_id: str) -> dict[str, str]:
    headers = {
        "Accept-Ranges": upstream.headers.get("accept-ranges") or "bytes",
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=3600, s-maxage=86400, stale-while-revalidate=604800",
        "Content-Disposition": f'inline; filename="{public_id}.mp4"',
        "Cross-Origin-Resource-Policy": "cross-origin",
        "X-Content-Type-Options": "nosniff",
    }
    for source, destination in (
        ("content-length", "Content-Length"),
        ("content-range", "Content-Range"),
        ("etag", "ETag"),
        ("last-modified", "Last-Modified"),
    ):
        value = upstream.headers.get(source)
        if value:
            headers[destination] = value
    return headers


def _content_range_total(value: str | None) -> int | None:
    match = _CONTENT_RANGE.fullmatch(str(value or "").strip())
    if not match or match.group(1) == "*":
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


async def _head_video(source_url: str, public_id: str, request: Request) -> Response:
    request_headers = _upstream_request_headers(request)
    async with httpx.AsyncClient(timeout=_VIDEO_TIMEOUT, follow_redirects=True) as client:
        upstream = await client.head(source_url, headers=request_headers)
        used_range_probe = False
        if upstream.status_code not in {200, 206}:
            probe_headers = dict(request_headers)
            probe_headers["Range"] = "bytes=0-0"
            upstream = await client.get(source_url, headers=probe_headers)
            used_range_probe = True
        if upstream.status_code not in {200, 206}:
            raise HTTPException(status_code=502, detail="Replay video host is unavailable")
        headers = _video_response_headers(upstream, public_id)
        if used_range_probe:
            total = _content_range_total(upstream.headers.get("content-range"))
            if total is not None:
                headers["Content-Length"] = str(total)
            headers.pop("Content-Range", None)
    return Response(status_code=200, headers=headers, media_type="video/mp4")


async def _stream_video(source_url: str, public_id: str, request: Request) -> StreamingResponse:
    client = httpx.AsyncClient(timeout=_VIDEO_TIMEOUT, follow_redirects=True)
    try:
        upstream_request = client.build_request(
            "GET",
            source_url,
            headers=_upstream_request_headers(request),
        )
        upstream = await client.send(upstream_request, stream=True)
    except Exception:
        await client.aclose()
        raise

    if upstream.status_code not in {200, 206}:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="Replay video host is unavailable")

    headers = _video_response_headers(upstream, public_id)

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=headers,
        media_type="video/mp4",
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
            row = await _published_submission(public_id)
            body = _render_replay_html(row, origin=_origin(request), public_id=public_id)
            return HTMLResponse(
                body,
                headers={
                    "Cache-Control": "public, max-age=60, s-maxage=300, stale-while-revalidate=3600",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        @self.api_route(
            "/api/replay-video/{public_id}",
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
        async def replay_video(public_id: str, request: Request) -> Response:
            row = await _published_submission(public_id)
            source_url = _source_video_url(row)
            if request.method == "HEAD":
                return await _head_video(source_url, public_id, request)
            return await _stream_video(source_url, public_id, request)

    FastAPI.__init__ = patched_init
    FastAPI._rankguess_replay_page_patch = True


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_route()
    _INSTALLED = True
