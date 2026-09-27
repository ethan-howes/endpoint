"""Which Overpass query each service reads, and under what key.

One table, shared by ``capture_fixtures.py`` (which fetches) and
``export_fixtures.py`` (which commits what was fetched). They were two copies
of the same list, and the failure mode of a drift between them is nasty: the
capture writes a perfectly good response under one cache id and the exporter
looks for it under another, so ``MOCK=1`` finds nothing and reports "no committed
fixture" for an area that is in fact fully seeded. That reads as missing data, not
as a naming mismatch, and costs an afternoon.

``kind`` is the third argument to ``tile_cache_id``, and hence part of both the
cache id and the fixture filename, so it has to be the same string the service
passes. The service passes that raw id straight to ``shared.fixtures``; nothing
here or there should pre-mangle it, because ``fixture_name`` already owns the
filename convention and a second mangling on top produces a name nothing reads.
"""

from __future__ import annotations

from services.s1_legal_spots.overpass import build_point_query, build_road_query
from services.s2_weather_cover.cover import build_cover_query, build_shade_query

#: service key -> ((kind, query builder), ...)
QUERIES: dict[str, tuple] = {
    "s1": (("roads", build_road_query), ("points", build_point_query)),
    "s2": (("cover", build_cover_query), ("shade", build_shade_query)),
}

#: Cache-id namespace per (service, kind) lives in ``shared.fixtures.cache_key``,
#: because the services read the cache as well as these two scripts and a rule
#: that only the scripts know is a rule the services can get wrong. It was a
#: literal at five call sites and *absent* from the exporter, which then reported
#: all 18 S2 fixtures MISSING straight after a capture that had written all 18.
#: Re-exported from here so ``fixture_plan`` remains the single place a reader
#: looks for "what does this service fetch and under what name".

#: service key -> the directory name under ``services/`` that holds its fixtures.
#: Not the same as the key: the directory is the package name, because
#: ``shared.fixtures.fixtures_dir`` resolves it as a path.
SERVICE_DIRS = {
    "s1": "s1_legal_spots",
    "s2": "s2_weather_cover",
}

#: S2's queries are heavier -- a ``nwr["building"]`` union over a 3 km2 tile is
#: well over a thousand elements -- so it gets a longer per-request budget than
#: S1's. These are for the capture script, which is allowed to be slow because it
#: runs once, offline, before a demo.
BUDGETS = {"s1": 150, "s2": 600}
