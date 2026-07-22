from __future__ import annotations

import html
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
    return f"{origin}/gallery/video.mp4?replay={quote(public_id, safe='')}"


def _clean_text(value: Any, fallback: str = "") -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or fallback


def _social_copy(row: dict[str, Any]) -> tuple[str, str]:
    player = _clean_text(row.get("player"), "Unknown player")
    artist = _clean_text(row.get("artist"))
    map_title = _clean_text(row.get("title"), "Unknown map")
    version = _clean_text(row.get("version"))
    map_name = " – ".join(part for part in (artist, map_title) if part)
    if version:
        map_name = f"{map_name} [{version}]"

    details: list[str] = []
    try:
        star = float(row.get("star"))
        details.append(f"{star:.2f}★")
    except (TypeError, ValueError):
        pass

    mods = _clean_text(row.get("mods"), "NM").replace(",", "")
    if mods:
        details.append(mods)

    try:
        accuracy = float(row.get("accuracy_percent"))
        details.append(f"{accuracy:.2f}% accuracy")
    except (TypeError, ValueError):
        pass

    title = f"{player} on {map_name} | osu!rankguess"
    description = f"Watch {player} play {map_name}"
    if details:
        description += f" · {' · '.join(details)}"
    description += ". Can you guess their rank?"
    return title[:180], description[:300]


def _gallery_document(
    row: dict[str, Any] | None,
    *,
    origin: str,
    public_id: str | None,
) -> str:
    document = _INDEX_PATH.read_text(encoding="utf-8")
    if row is None or public_id is None:
        return document

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

    # Stable API aliases remain available for diagnostics.
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
