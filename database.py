from __future__ import annotations

import sys

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
_community.install()

from runtime import cron as _cron
sys.modules.setdefault("cron_runtime", _cron)
_cron.install()
