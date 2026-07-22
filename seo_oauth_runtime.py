from __future__ import annotations

from typing import Any

from fastapi import Query
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from runtime import replay_page as _replay_page
from runtime.oauth import *
from runtime.oauth import register_routes as _register_oauth_routes


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
    """Register OAuth and canonical replay routes on the actual FastAPI app."""
    _register_oauth_routes(app)
    if getattr(app.state, "rankguess_explicit_replay_routes", False):
        return
    app.state.rankguess_explicit_replay_routes = True

    @app.get(
        "/replay/{public_id}",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def canonical_replay_page(public_id: str, request: Request) -> HTMLResponse:
        return await _replay_page_response(public_id, request)

    @app.api_route(
        "/replay/{public_id}/video.mp4",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def canonical_replay_video(public_id: str, request: Request) -> Response:
        return await _replay_video_response(public_id, request)

    # Stable API aliases remain available for compatibility and direct diagnostics.
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
        "/replay/{public_id}",
        "/replay/{public_id}/video.mp4",
        "/api/replay-page",
        "/api/replay-video",
    }
    missing = required - paths
    if missing:
        raise RuntimeError(f"Replay routes failed to register: {sorted(missing)}")
