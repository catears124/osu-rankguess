from __future__ import annotations

import html
import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Query
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from runtime import replay_page as _replay_page
from runtime.oauth import *
from runtime.oauth import register_routes as _register_oauth_routes


_INDEX_PATH = Path(__file__).resolve().parent / "public" / "index.html"
_SOCIAL_META = re.compile(
    r"\s*(?:"
    r"<link\s+rel=[\"']canonical[\"'][^>]*>"
    r"|<meta\s+(?:name|property)=[\"'](?:description|og:[^\"']+|twitter:[^\"']+)[\"'][^>]*>"
    r")",
    re.IGNORECASE,
)
_TITLE = re.compile(r"<title>[\s\S]*?</title>", re.IGNORECASE)


def _gallery_replay_url(origin: str, public_id: str) -> str:
    return f"{origin}/gallery?replay={quote(public_id, safe='')}"


def _gallery_video_url(origin: str, public_id: str) -> str:
    # Keep Discord's media request on an explicit function route. Paths ending in
    # .mp4 can be treated as static assets by deployment routing before FastAPI
    # sees them.
    return f"{origin}/api/replay-video?public_id={quote(public_id, safe='')}"


def _clean_text(value: Any, fallback: str = "") -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or fallback


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _social_copy(row: dict[str, Any]) -> tuple[str, str]:
    player = _clean_text(row.get("player"), "Unknown player")
    artist = _clean_text(row.get("artist"))
    map_title = _clean_text(row.get("title"), "Unknown map")
    version = _clean_text(row.get("version"))
    map_name = " – ".join(part for part in (artist, map_title) if part) or "Unknown map"
    if version:
        map_name = f"{map_name} [{version}]"

    details: list[str] = [map_name]
    star = _finite_float(row.get("star"))
    if star is not None:
        details.append(f"{star:.2f}★")

    mods = _clean_text(row.get("mods"), "NM").replace(",", "")
    if mods:
        details.append(mods)

    accuracy = _finite_float(row.get("accuracy_percent"))
    if accuracy is not None:
        details.append(f"{accuracy:.2f}% accuracy")

    description = " · ".join(details)
    description += " · Can you guess their rank?"
    return player[:120], description[:300]


def _replay_social_meta(
    row: dict[str, Any],
    *,
    origin: str,
    public_id: str,
) -> tuple[str, str, str, str, str, str]:
    canonical = _gallery_replay_url(origin, public_id)
    video = _gallery_video_url(origin, public_id)
    thumbnail = _replay_page._absolute(  # noqa: SLF001
        origin,
        row.get("thumbnail_url") or f"/api/gallery/{public_id}/thumbnail",
    )
    title, description = _social_copy(row)
    esc = lambda value: html.escape(str(value or ""), quote=True)
    replay_meta = f"""
  <link rel="canonical" href="{esc(canonical)}" />
  <link rel="alternate" type="video/mp4" href="{esc(video)}" />
  <meta name="description" content="{esc(description)}" />
  <meta property="og:type" content="video.other" />
  <meta property="og:site_name" content="osu!rankguess" />
  <meta property="og:title" content="{esc(title)}" />
  <meta property="og:description" content="{esc(description)}" />
  <meta property="og:url" content="{esc(canonical)}" />
  <meta property="og:image" content="{esc(thumbnail)}" />
  <meta property="og:image:secure_url" content="{esc(thumbnail)}" />
  <meta property="og:image:alt" content="Thumbnail for {esc(title)}" />
  <meta property="og:video" content="{esc(video)}" />
  <meta property="og:video:url" content="{esc(video)}" />
  <meta property="og:video:secure_url" content="{esc(video)}" />
  <meta property="og:video:type" content="video/mp4" />
  <meta property="og:video:width" content="960" />
  <meta property="og:video:height" content="540" />
  <meta name="twitter:card" content="player" />
  <meta name="twitter:title" content="{esc(title)}" />
  <meta name="twitter:description" content="{esc(description)}" />
  <meta name="twitter:image" content="{esc(thumbnail)}" />
  <meta name="twitter:player" content="{esc(canonical)}" />
  <meta name="twitter:player:width" content="960" />
  <meta name="twitter:player:height" content="540" />
  <meta name="twitter:player:stream" content="{esc(video)}" />
  <meta name="twitter:player:stream:content_type" content="video/mp4" />"""
    return title, description, canonical, video, thumbnail, replay_meta


def _index_candidates() -> list[Path]:
    module_path = Path(__file__).resolve()
    cwd = Path.cwd().resolve()
    roots = [module_path.parent, *module_path.parents, cwd, *cwd.parents]
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        candidate = root / "public" / "index.html"
        if candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    if _INDEX_PATH not in seen:
        candidates.insert(0, _INDEX_PATH)
    return candidates


def _read_gallery_app_document() -> str:
    failures: list[str] = []
    for candidate in _index_candidates():
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError as error:
            failures.append(f"{candidate}: {error}")
    raise FileNotFoundError("Could not locate public/index.html; " + " | ".join(failures))


def _gallery_handoff_document(
    *,
    title: str,
    description: str,
    replay_meta: str,
    public_id: str | None,
) -> str:
    # Social crawlers do not run JavaScript and keep the metadata above. Browsers
    # immediately enter the real SPA, which then canonicalizes the URL back to
    # /gallery and opens the requested replay in the normal gallery dialog.
    target = (
        f"/daily?replay={quote(public_id, safe='')}"
        if public_id
        else "/daily#gallery"
    )
    esc = lambda value: html.escape(str(value or ""), quote=True)
    target_json = json.dumps(target, ensure_ascii=False).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="theme-color" content="#050506" />
  <title>{esc(title)}</title>{replay_meta}
  <script>location.replace({target_json});</script>
  <style>
    html,body{{margin:0;min-height:100%;background:#050506;color:#fff;font-family:Inter,ui-sans-serif,system-ui,sans-serif}}
    body{{display:grid;place-items:center}}a{{color:#ff8abb}}
  </style>
</head>
<body>
  <noscript><p><a href="{esc(target)}">Open this replay in the gallery</a></p></noscript>
  <p aria-hidden="true">Opening replay…</p>
</body>
</html>"""


def _gallery_document(
    row: dict[str, Any] | None,
    *,
    origin: str,
    public_id: str | None,
) -> str:
    title = "osu! Replay Rank Prediction Gallery | osu!rankguess"
    description = "Browse osu! replay clips and compare actual ranks with model predictions."
    replay_meta = ""
    if row is not None and public_id is not None:
        title, description, _canonical, _video, _thumbnail, replay_meta = _replay_social_meta(
            row,
            origin=origin,
            public_id=public_id,
        )

    try:
        document = _read_gallery_app_document()
    except OSError:
        return _gallery_handoff_document(
            title=title,
            description=description,
            replay_meta=replay_meta,
            public_id=public_id,
        )

    if row is None or public_id is None:
        return document

    esc = lambda value: html.escape(str(value or ""), quote=True)
    document = _SOCIAL_META.sub("", document)
    document = _TITLE.sub(f"<title>{esc(title)}</title>", document, count=1)
    return document.replace("</head>", f"{replay_meta}\n</head>", 1)


async def _replay_page_response(public_id: str, request: Request) -> HTMLResponse:
    row = await _replay_page._published_submission(public_id)  # noqa: SLF001
    body = _replay_page._render_replay_html(  # noqa: SLF001
        row,
        origin=_replay_page._origin(request),  # noqa: SLF001
        public_id=public_id,
    )
    return HTMLResponse(
        body,
        headers={
            "Cache-Control": "public, max-age=60, s-maxage=300, stale-while-revalidate=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _replay_video_response(public_id: str, request: Request) -> Response:
    row = await _replay_page._published_submission(public_id)  # noqa: SLF001
    source_url = _replay_page._source_video_url(row)  # noqa: SLF001
    if request.method == "HEAD":
        return await _replay_page._head_video(source_url, public_id, request)  # noqa: SLF001
    return await _replay_page._stream_video(source_url, public_id, request)  # noqa: SLF001


def register_routes(app: Any) -> None:
    """Register OAuth and canonical gallery replay routes on the FastAPI app."""
    _register_oauth_routes(app)
    if getattr(app.state, "rankguess_explicit_replay_routes", False):
        return
    app.state.rankguess_explicit_replay_routes = True

    @app.get(
        "/gallery",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def gallery_page(
        request: Request,
        replay: str | None = Query(default=None, min_length=6, max_length=80),
    ) -> HTMLResponse:
        row = None
        if replay:
            row = await _replay_page._published_submission(replay)  # noqa: SLF001
        body = _gallery_document(
            row,
            origin=_replay_page._origin(request),  # noqa: SLF001
            public_id=replay,
        )
        return HTMLResponse(
            body,
            headers={
                "Cache-Control": (
                    "public, max-age=60, s-maxage=300, stale-while-revalidate=3600"
                    if replay
                    else "no-store, max-age=0"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.api_route(
        "/gallery/video.mp4",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def gallery_replay_video(
        request: Request,
        replay: str = Query(..., min_length=6, max_length=80),
    ) -> Response:
        return await _replay_video_response(replay, request)

    # Stable API aliases remain available for diagnostics and social crawlers.
    @app.get(
        "/api/replay-page",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def replay_page_api(
        request: Request,
        public_id: str = Query(..., min_length=6, max_length=80),
    ) -> HTMLResponse:
        return await _replay_page_response(public_id, request)

    @app.api_route(
        "/api/replay-video",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def replay_video_api(
        request: Request,
        public_id: str = Query(..., min_length=6, max_length=80),
    ) -> Response:
        return await _replay_video_response(public_id, request)

    paths = {getattr(route, "path", None) for route in app.routes}
    required = {
        "/gallery",
        "/gallery/video.mp4",
        "/api/replay-page",
        "/api/replay-video",
    }
    missing = required - paths
    if missing:
        raise RuntimeError(f"Replay routes failed to register: {sorted(missing)}")
