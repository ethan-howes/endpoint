"""Committed demo fixtures: replaying the live Overpass bytes without the network.

ENDPOINT.md section 8 calls for ``fixtures/`` directories next to each service, and
``MOCK=1`` in ``.env.example``. The important property is *where* the mock sits:
it replaces the HTTP call and nothing else. The same ``parse_roads``,
``parse_restrictions``, ``generate_candidates``, ``judge_candidate`` and ``rank``
code runs in mock and live mode, so a mock run exercises the real pipeline rather
than a parallel implementation that can drift from it.

That is also why the fixtures are raw Overpass JSON instead of pre-parsed objects.
A committed fixture that has already been through our parser can only prove the
parser round-trips; replaying the original bytes also proves the parser handles
what the API actually returns, nulls in geometry and all.

Fixture filenames are derived from the cache id (which is already tile-derived and
stable), so ``prefetch`` and ``MOCK`` agree without a lookup table. In a repo they
are committed, because "the demo data" is part of the deliverable -- it is what
makes a recorded run reproducible, and it is the fallback when the public Overpass
instance is down on demo day.

``MOCK=1`` reads by *cache id*, not by filename: the service passes the id it
computed for its tile and the same :func:`read_fixture` finds the file. Nothing may
pre-mangle that id. It happened twice. S2 passed ``f"cover__{_slug(cid)}"`` while
``export_fixtures`` wrote the raw id, so every S2 fixture was committed and
permanently unreadable, and the reported symptom -- "no committed fixture" for a
fully seeded area -- looks like missing data rather than a naming mismatch.
``TestMockReplay`` in ``tests/test_s2_api.py`` writes a fixture and asks the service
to find it, which is the only kind of test that could have caught it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import REPO_ROOT


#: Cache-id namespace per (service, kind), for the raw Overpass cache.
#:
#: Not the same as the fixture filename: the cache file is named by a hash of the
#: query, and the namespace is only recorded in the ``.meta`` sidecar, so it cannot
#: be recovered from the cache directory and has to be declared. S1 stores under the
#: bare kind, S2 under ``s2_cover/`` and ``s2_shade/``.
#:
#: This was a literal at five call sites across ``cover.py``, ``service.py`` and
#: ``capture_fixtures.py``, and ``export_fixtures.py`` had no copy at all -- so it
#: used a third rule and reported all 18 S2 fixtures MISSING immediately after a
#: capture that had written all 18. One function, called by every reader and every
#: writer, is the only way this stops recurring; the same reason ``fixture_name``
#: owns the filename convention rather than leaving it to the writer.
CACHE_NAMESPACES: dict[tuple[str, str], str] = {
    ("s1", "roads"): "",
    ("s1", "points"): "",
    ("s2", "cover"): "s2_cover/",
    ("s2", "shade"): "s2_shade/",
    ("s2", "paths"): "s2_paths/",
}


def cache_key(service: str, kind: str, tile_cache_id: str) -> str:
    """The disk-cache key for a tile's response, with its namespace.

    ``cache_key("s2", "cover", "cover:25.75...")`` is
    ``"s2_cover/cover:25.75..."``. Unknown (service, kind) pairs raise rather than
    falling back to a guess, because a silent fallback here is indistinguishable
    from a cache miss and produces exactly the wrong kind of bug report.
    """
    try:
        namespace = CACHE_NAMESPACES[(service, kind)]
    except KeyError:
        known = sorted(f"{s}/{k}" for s, k in CACHE_NAMESPACES)
        raise KeyError(
            f"no cache namespace declared for ({service!r}, {kind!r}); known: {known}"
        ) from None
    return f"{namespace}{tile_cache_id}"


def fixtures_dir(service_package: str) -> Path:
    """``services/s1_legal_spots/fixtures`` for ``'s1_legal_spots'``."""
    return REPO_ROOT / "services" / service_package / "fixtures"


def fixture_name(cache_id: str) -> str:
    """Turn a cache id into a readable, stable filename.

    Cache ids look like ``roads:25.756000,-80.376000,25.760000,-80.372000``; the
    colon and the bare minus signs are legal on Linux but hostile to shells and to
    people reading a directory listing, so both are replaced.
    """
    return cache_id.replace(":", "__").replace("-", "m").replace(",", "_") + ".json"


def fixture_path(directory: Path, cache_id: str) -> Path:
    return directory / fixture_name(cache_id)


def read_fixture(directory: Path, cache_id: str) -> dict[str, Any] | None:
    """Return the committed fixture for ``cache_id``, or None if absent."""
    path = fixture_path(directory, cache_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) and "elements" in payload else None


def write_fixture(directory: Path, cache_id: str, payload: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = fixture_path(directory, cache_id)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def list_fixtures(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.glob("*.json"))
