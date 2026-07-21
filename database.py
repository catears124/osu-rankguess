from __future__ import annotations

import sys

from fastapi import FastAPI as _FastAPI
from starlette.requests import Request as _Request
from starlette.responses import JSONResponse as _JSONResponse

from backend.database import *
from backend.database import _connect
from backend import replay_features as _replay_features

sys.modules.setdefault("replay_features", _replay_features)

from backend import render_jobs as _render_jobs
sys.modules.setdefault("render_jobs", _render_jobs)

from runtime import bootstrap as _bootstrap
_bootstrap.install()

from runtime import ordr as _ordr
sys.modules.setdefault("ordr_recovery", _ordr)
_ordr.install()

from runtime import community as _community
sys.modules.setdefault("community_runtime", _community)
# These runtime modules define FastAPI routes inside installer functions while
# using postponed annotations. FastAPI resolves those annotation strings from
# the module globals, not the installer's local import scope. Publish the
# concrete Starlette classes before any FastAPI application is constructed so
# `request: Request` is injected rather than exposed as a required query field.
_community.Request = _Request
_community.JSONResponse = _JSONResponse
_community.install()

from runtime import cron as _cron
sys.modules.setdefault("cron_runtime", _cron)
_cron.Request = _Request
_cron.JSONResponse = _JSONResponse
_cron.install()

from runtime import daily_fresh as _daily_fresh
sys.modules.setdefault("daily_fresh_runtime", _daily_fresh)
_daily_fresh.install()

from runtime import daily_diversity as _daily_diversity
sys.modules.setdefault("daily_diversity_runtime", _daily_diversity)
_daily_diversity.install()

from runtime import daily_randomness as _daily_randomness
sys.modules.setdefault("daily_randomness_runtime", _daily_randomness)
_daily_randomness.install()

from runtime import cron_oidc as _cron_oidc
sys.modules.setdefault("cron_oidc_runtime", _cron_oidc)
_cron_oidc.install()


def _install_cron_route_contract_check() -> None:
    """Fail startup if FastAPI turns a Request object into a query parameter."""
    if getattr(_FastAPI, "_rankguess_cron_route_contract_check", False):
        return

    original_init = _FastAPI.__init__
    cron_paths = {"/api/cron/seed-gallery", "/api/cron/tick"}

    def checked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        title = kwargs.get("title") or getattr(self, "title", "")
        if title != "osu!rankguess":
            return
        for route in self.routes:
            if getattr(route, "path", None) not in cron_paths:
                continue
            dependant = getattr(route, "dependant", None)
            query_names = {
                field.name
                for field in (getattr(dependant, "query_params", None) or [])
            }
            if "request" in query_names:
                raise RuntimeError(
                    f"{route.path} registered Request as a query parameter"
                )

    _FastAPI.__init__ = checked_init
    _FastAPI._rankguess_cron_route_contract_check = True


_install_cron_route_contract_check()
