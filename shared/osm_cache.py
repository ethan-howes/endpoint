"""Shared, disk-cached Overpass client.

Why this exists in ``shared/`` rather than inside S1: S1 (roads + stopping
restrictions) and S2 (cover features) both read Overpass, and the public
instances return 504 under load often enough that it happened on the first
request of a real session and repeatedly while capturing the demo data. If each
service owned its own HTTP client and cache we would double the load on an
already-flaky endpoint. Sharing the transport and cache keeps the services
independent in *what* they ask for while halving the traffic, and gives
``scripts/prefetch_demo_area.py`` one mechanism to warm both.

**The cache key and the query bbox are deliberately separate.** The query is
built over the containment tile *expanded by the search radius*, so a rider
standing near a tile edge still has their whole search circle covered. The key is
the unexpanded tile, so every rider in that tile shares one entry and a prefetch
can predict it exactly. Conflating the two was the original design and it is
wrong twice over: keying on the expanded box would make the key depend on the
radius in a way prefetch could not reproduce, and keying on the tile alone would
silently under-cover the radius.

Cache layout::

    data/cache/overpass/<sha256(query + cache_id)>.json   raw Overpass response
    data/cache/overpass/<sha256(query + cache_id)>.meta   fetched_at, endpoint

The raw response is cached verbatim, so ``MOCK=1`` replays exactly the bytes the
live API produced and the whole parse/geometry path runs unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import OVERPASS_CACHE_DIR, SETTINGS

USER_AGENT = "endpoint-demo/0.1 (robotaxi pickup spot research; OSM/Overpass)"

#: How long a cached response counts as fresh. OSM road geometry does not change
#: fast, and for a demo we would far rather run on week-old data than risk a 504.
#: ``prefetch_demo_area.py --force`` ignores it.
DEFAULT_TTL_S = 7 * 24 * 3600


@dataclass(frozen=True)
class CacheEntry:
    key: str
    path: Path
    meta_path: Path
    fetched_at: float
    age_s: float
    endpoint: str

    @property
    def fresh(self) -> bool:
        return self.age_s <= DEFAULT_TTL_S


def cache_key(query: str, cache_id: str) -> str:
    """Hash a query plus its cache id into a filesystem-safe key."""
    return hashlib.sha256(f"{query.strip()}|{cache_id}".encode("utf-8")).hexdigest()[:32]


def _paths(key: str) -> tuple[Path, Path]:
    OVERPASS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return OVERPASS_CACHE_DIR / f"{key}.json", OVERPASS_CACHE_DIR / f"{key}.meta"


def read_cache(query: str, cache_id: str) -> dict[str, Any] | None:
    """Return the cached Overpass response for this query+tile, or None."""
    path, _ = _paths(cache_key(query, cache_id))
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def cache_info(query: str, cache_id: str) -> CacheEntry | None:
    key = cache_key(query, cache_id)
    path, meta_path = _paths(key)
    if not path.exists():
        return None
    fetched_at, endpoint = 0.0, "unknown"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched_at = float(meta.get("fetched_at", 0.0))
            endpoint = str(meta.get("endpoint", "unknown"))
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    return CacheEntry(
        key=key,
        path=path,
        meta_path=meta_path,
        fetched_at=fetched_at,
        age_s=time.time() - fetched_at if fetched_at else float("inf"),
        endpoint=endpoint,
    )


def write_cache(
    query: str,
    cache_id: str,
    payload: dict[str, Any],
    endpoint: str = "unknown",
) -> Path:
    key = cache_key(query, cache_id)
    path, meta_path = _paths(key)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)  # atomic: a killed process cannot leave a truncated file
    meta_path.write_text(
        json.dumps(
            {
                "fetched_at": time.time(),
                "endpoint": endpoint,
                "elements": len(payload.get("elements", [])),
                "cache_id": cache_id,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


class OverpassError(RuntimeError):
    """Every mirror failed. Callers degrade; this never reaches a rider."""


async def afetch_overpass(
    query: str,
    cache_id: str,
    *,
    use_cache: bool = True,
    store: bool = True,
    timeout_s: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch an Overpass response, preferring cache and failing over mirrors.

    Order of attempts:

      1. disk cache, when ``use_cache`` (skipped to force a refetch)
      2. each mirror in ``SETTINGS.overpass_mirrors``, in order

    ``use_cache`` and ``store`` are separate on purpose. A forced refetch
    (``use_cache=False``) must still persist, otherwise "prefetch with --force"
    would download the whole demo area and throw it away. Conflating the two is
    how the first version of the prefetch script reported every fetch as
    successful while writing nothing to disk.

    Raises ``OverpassError`` only if every attempt fails.
    """
    if use_cache:
        cached = read_cache(query, cache_id)
        if cached is not None:
            return cached

    timeout = timeout_s or SETTINGS.overpass_timeout_s
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)

    errors: list[str] = []
    try:
        for endpoint in SETTINGS.overpass_mirrors:
            try:
                resp = await client.post(
                    endpoint,
                    data={"data": query},
                    headers={"User-Agent": USER_AGENT},
                    timeout=timeout,
                )
                if resp.status_code != 200:
                    errors.append(f"{endpoint}: HTTP {resp.status_code}")
                    continue
                payload = resp.json()
                if "elements" not in payload:
                    errors.append(f"{endpoint}: response had no 'elements' key")
                    continue
                if store:
                    write_cache(query, cache_id, payload, endpoint=endpoint)
                return payload
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                errors.append(f"{endpoint}: {exc!r}")
    finally:
        if owns_client:
            await client.aclose()

    raise OverpassError("all Overpass mirrors failed -> " + "; ".join(errors))


def fetch_overpass(
    query: str,
    cache_id: str,
    *,
    use_cache: bool = True,
    store: bool = True,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Synchronous wrapper, for scripts like ``prefetch_demo_area.py``."""
    return asyncio.run(
        afetch_overpass(
            query, cache_id, use_cache=use_cache, store=store, timeout_s=timeout_s
        )
    )


def clear_cache() -> int:
    """Remove every cached Overpass response. Returns the count removed."""
    if not OVERPASS_CACHE_DIR.exists():
        return 0
    removed = 0
    for pattern in ("*.json", "*.meta", "*.json.tmp"):
        for p in OVERPASS_CACHE_DIR.glob(pattern):
            p.unlink()
            removed += 1
    return removed
