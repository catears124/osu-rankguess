from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urljoin

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from backend import database as _database

_INSTALLED = False
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9_-]{6,80}$")


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
        .replace("<", "\u003c")
        .replace(">", "\u003e")
        .replace("&", "\u0026")
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
    data = _safe_json(replay)

    esc = lambda value: html.escape(str(value or ""), quote=True)
    meta_title = "osu!rankguess replay"
    meta_description = "Can you guess this osu! player's rank?"

    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="theme-color" content="#050506" />
  <meta name="color-scheme" content="dark" />
  <meta name="robots" content="index, follow, max-video-preview:-1" />
  <title>{esc(meta_title)}</title>
  <link rel="canonical" href="{esc(canonical)}" />
  <meta name="description" content="{esc(meta_description)}" />
  <meta property="og:type" content="video.other" />
  <meta property="og:site_name" content="osu!rankguess" />
  <meta property="og:title" content="{esc(meta_title)}" />
  <meta property="og:description" content="{esc(meta_description)}" />
  <meta property="og:url" content="{esc(canonical)}" />
  <meta property="og:image" content="{esc(thumbnail_url)}" />
  <meta property="og:image:secure_url" content="{esc(thumbnail_url)}" />
  <meta property="og:video" content="{esc(video_url)}" />
  <meta property="og:video:url" content="{esc(video_url)}" />
  <meta property="og:video:secure_url" content="{esc(video_url)}" />
  <meta property="og:video:type" content="video/mp4" />
  <meta property="og:video:width" content="960" />
  <meta property="og:video:height" content="540" />
  <meta name="twitter:card" content="player" />
  <meta name="twitter:title" content="{esc(meta_title)}" />
  <meta name="twitter:description" content="{esc(meta_description)}" />
  <meta name="twitter:image" content="{esc(thumbnail_url)}" />
  <meta name="twitter:player" content="{esc(canonical)}" />
  <meta name="twitter:player:width" content="960" />
  <meta name="twitter:player:height" content="540" />
  <meta name="twitter:player:stream" content="{esc(video_url)}" />
  <meta name="twitter:player:stream:content_type" content="video/mp4" />
  <style>
    :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: #050506; color: #f5f5f5; }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; background: #050506; }
    body { min-height: 100vh; overflow: hidden; }
    button, a { font: inherit; }
    .shell { position: fixed; inset: 0; display: grid; grid-template-rows: auto 1fr; background: #050506; }
    .topbar { position: relative; z-index: 5; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px max(18px, env(safe-area-inset-right)) 14px max(18px, env(safe-area-inset-left)); border-bottom: 1px solid rgba(255,255,255,.1); background: rgba(5,5,6,.88); backdrop-filter: blur(16px); }
    .brand { color: #fff; text-decoration: none; font-weight: 800; letter-spacing: -.04em; }
    .brand span { color: #ff66aa; }
    .top-actions { display: flex; gap: 10px; }
    .button { border: 1px solid rgba(255,255,255,.16); border-radius: 999px; padding: 9px 14px; color: #fff; background: rgba(255,255,255,.07); cursor: pointer; text-decoration: none; transition: .15s ease; }
    .button:hover, .button:focus-visible { border-color: rgba(255,255,255,.36); background: rgba(255,255,255,.12); outline: none; }
    .stage { position: relative; min-height: 0; display: grid; place-items: center; overflow: hidden; background: #000; }
    video { width: 100%; height: 100%; object-fit: contain; background: #000; }
    .sound-gate { position: absolute; inset: 0; z-index: 3; display: grid; place-items: center; border: 0; color: #fff; background: rgba(0,0,0,.42); cursor: pointer; }
    .sound-gate[hidden] { display: none; }
    .sound-gate span { padding: 12px 18px; border: 1px solid rgba(255,255,255,.22); border-radius: 999px; background: rgba(10,10,12,.8); font-weight: 700; }
    .spoiler { position: absolute; z-index: 4; right: max(20px, env(safe-area-inset-right)); bottom: max(20px, env(safe-area-inset-bottom)); width: min(390px, calc(100vw - 40px)); padding: 22px; border: 1px solid rgba(255,255,255,.14); border-radius: 20px; background: rgba(8,8,10,.9); box-shadow: 0 24px 80px rgba(0,0,0,.55); backdrop-filter: blur(18px); }
    .kicker { margin: 0 0 7px; color: #ff8fc3; font-size: 12px; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; }
    h1 { margin: 0; font-size: clamp(27px, 4vw, 42px); letter-spacing: -.05em; }
    .spoiler > p:not(.kicker) { margin: 10px 0 18px; color: #b9b9c0; line-height: 1.5; }
    .reveal-card { width: 100%; display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 14px; padding: 15px 16px; border: 1px solid rgba(255,255,255,.13); border-radius: 14px; color: #fff; background: rgba(255,255,255,.05); cursor: pointer; text-align: left; }
    .reveal-card span { color: #aaaab3; font-size: 13px; }
    .reveal-card small { display: block; margin-top: 3px; color: #777781; font-size: 11px; }
    .reveal-card strong { font-size: 30px; line-height: 1; }
    .result { display: none; }
    .result.visible { display: block; }
    .map { margin: 8px 0 17px; color: #b9b9c0; line-height: 1.45; }
    .ranks { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
    .ranks div { padding: 12px; border-radius: 12px; background: rgba(255,255,255,.055); }
    .ranks span { display: block; margin-bottom: 4px; color: #8f8f98; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
    .ranks strong { font-size: 17px; }
    @media (max-width: 720px) {
      .topbar { padding-top: max(12px, env(safe-area-inset-top)); }
      .spoiler { right: 12px; bottom: max(12px, env(safe-area-inset-bottom)); width: calc(100vw - 24px); padding: 18px; }
      .button { padding: 8px 12px; }
      .ranks { grid-template-columns: 1fr 1fr; }
      .ranks div:last-child { grid-column: 1 / -1; }
    }
  </style>
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
      <video id="replayVideo" src="{esc(video_url)}" poster="{esc(thumbnail_url)}" controls autoplay playsinline preload="auto"></video>
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
  <script id="replayData" type="application/json">{data}</script>
  <script>
    (() => {
      const data = JSON.parse(document.querySelector("#replayData").textContent);
      const video = document.querySelector("#replayVideo");
      const gate = document.querySelector("#soundGate");
      const formatRank = (value) => Number(value) > 0 ? `#${Math.round(Number(value)).toLocaleString()}` : "-";
      const trySound = async () => {
        video.muted = false;
        video.volume = 1;
        try { await video.play(); gate.hidden = true; }
        catch { gate.hidden = false; }
      };
      gate.addEventListener("click", trySound);
      window.addEventListener("load", trySound, { once: true });
      document.querySelector("#reveal").addEventListener("click", () => {
        const actual = Number(data.actualRank);
        const predicted = Number(data.predictedRank);
        const ratio = actual > 0 && predicted > 0 ? Math.max(actual, predicted) / Math.max(1, Math.min(actual, predicted)) : NaN;
        document.querySelector("#player").textContent = data.player || "Unknown player";
        const map = data.beatmap || {};
        document.querySelector("#map").textContent = `${map.artist ? `${map.artist} - ` : ""}${map.title || "Unknown map"}${map.version ? ` [${map.version}]` : ""}`;
        document.querySelector("#actual").textContent = formatRank(actual);
        document.querySelector("#model").textContent = formatRank(predicted);
        document.querySelector("#ratio").textContent = Number.isFinite(ratio) ? `${ratio.toFixed(2)}x` : "-";
        document.querySelector("#hiddenState").hidden = true;
        document.querySelector("#resultState").classList.add("visible");
      });
      document.querySelector("#copyLink").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        try {
          await navigator.clipboard.writeText(data.canonicalURL);
        } catch {
          const input = document.createElement("textarea");
          input.value = data.canonicalURL;
          input.style.position = "fixed";
          input.style.opacity = "0";
          document.body.appendChild(input);
          input.select();
          document.execCommand("copy");
          input.remove();
        }
        button.textContent = "copied";
        setTimeout(() => { if (button.isConnected) button.textContent = "copy link"; }, 1400);
      });
    })();
  </script>
</body>
</html>'''


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
            row = await __import__("asyncio").to_thread(_database.get_submission, public_id)
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
