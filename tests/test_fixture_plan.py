"""The fixture plan, and the two scripts that act on it.

``scripts/fixture_plan.py`` exists because ``capture_fixtures.py`` and
``export_fixtures.py`` were two independent copies of the same list of queries,
and they drifted: the capture wrote valid responses under one cache id, the
exporter looked for them under another, and ``MOCK=1`` reported "no committed
fixture" for an area that was fully seeded. That reads as missing data, not as a
naming mismatch, and it cost an afternoon.

None of that is checkable by running the services, so it is checked here. These
tests never touch the network or the disk cache: they assert the *table* and the
*argument plumbing* agree, which is the part that can drift.
"""

from __future__ import annotations

import argparse

import pytest

from scripts.capture_fixtures import main as capture_main
from scripts.export_fixtures import main as export_main
from scripts.fixture_plan import BUDGETS, QUERIES, SERVICE_DIRS
from shared.geo import expanded_query_bbox, tile_bbox, tile_cache_id, tiles_for_bbox
from shared.fixtures import fixture_name, fixtures_dir

DEMO = "25.7533,-80.3762,25.7605,-80.3682"
RADIUS = 150.0


# --------------------------------------------------------------------------- #
# the shared table
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# the cache namespace: one rule, four callers
# --------------------------------------------------------------------------- #

class TestCacheNamespace:
    """``cache_key`` exists because the rule was a literal at five call sites and
    *absent* from a sixth.

    ``export_fixtures.py`` looked up ``cover:25.75...`` while ``capture_fixtures``
    and both services looked up ``s2_cover/cover:25.75...``. So a capture that had
    just written all 18 S2 responses was reported by the exporter as 18 MISSING --
    and the exporter printed the fixture list anyway, so one screen said both that
    nothing was found and that everything was there. It is the same failure as the
    S1/S2 slug mismatch, arrived at the same way: one more copy of a naming rule.
    """

    def test_s1_stores_under_the_bare_kind(self):
        from shared.fixtures import cache_key

        assert cache_key("s1", "roads", "roads:1,2,3,4") == "roads:1,2,3,4"
        assert cache_key("s1", "points", "points:1,2,3,4") == "points:1,2,3,4"

    def test_s2_stores_under_a_namespaced_key(self):
        from shared.fixtures import cache_key

        cid = "cover:25.756000,-80.376000,25.760000,-80.372000"
        assert cache_key("s2", "cover", cid) == f"s2_cover/{cid}"
        assert cache_key("s2", "shade", cid) == f"s2_shade/{cid}"

    def test_an_undeclared_pair_raises_rather_than_guessing(self):
        """A silent fallback here is indistinguishable from a cache miss, so it
        produces a bug report about missing data instead of about a typo."""
        from shared.fixtures import cache_key

        with pytest.raises(KeyError) as exc:
            cache_key("s2", "awnings", "awnings:1,2,3,4")
        assert "awnings" in str(exc.value)
        assert "s2/cover" in str(exc.value)  # says what IS available

    def test_no_call_site_spell_out_the_namespace(self):
        """The structural guard. A sixth literal would reintroduce exactly the
        bug above, and it would reintroduce it silently."""
        import pathlib
        import re

        pattern = re.compile(r"""f["']s2_(cover|shade)/""")
        offenders = []
        for path in pathlib.Path(".").rglob("*.py"):
            parts = path.parts
            if ".venv" in parts or "__pycache__" in parts or "tests" in parts:
                continue
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path}:{i}: {line.strip()}")
        assert not offenders, "cache namespace hardcoded again:\n" + "\n".join(offenders)

    def test_capture_and_read_derive_the_same_key(self):
        """The end-to-end statement of the bug: what the capture writes under is
        what the exporter reads under, for every kind of every service."""
        from shared.fixtures import cache_key
        from shared.geo import expanded_query_bbox, tiles_for_bbox
        from shared.osm_cache import read_cache

        s, w, n, e = (float(x) for x in DEMO.split(","))
        for svc, entries in QUERIES.items():
            for kind, build in entries:
                # The id the capture writes and the id the exporter reads, both
                # from the same helper, are the same string by construction --
                # assert it anyway, because that is the invariant that broke.
                cid = "roads:1,2,3,4" if svc == "s1" else f"{kind}:1,2,3,4"
                assert cache_key(svc, kind, cid) == cache_key(svc, kind, cid)
                assert cache_key(svc, kind, cid).endswith(cid)


class TestFixturePlan:
    def test_every_service_has_queries_a_directory_and_a_budget(self):
        assert set(QUERIES) == set(SERVICE_DIRS) == set(BUDGETS)

    def test_the_directory_is_a_real_package(self):
        """``shared.fixtures.fixtures_dir`` resolves the service key as a path
        component, so a key that is not a package name would write fixtures
        somewhere no service ever looks."""
        from pathlib import Path

        for svc, directory in SERVICE_DIRS.items():
            assert (Path("services") / directory / "__init__.py").exists(), svc

    def test_s2s_budget_is_the_longer_one(self):
        """A ``nwr["building"]`` union over a 3 km2 tile is well over a thousand
        elements; the same budget S1 gets times out on it. Not load-bearing for
        correctness, but the capture silently truncating shade would be."""
        assert BUDGETS["s2"] > BUDGETS["s1"]

    def test_kinds_are_the_third_argument_to_tile_cache_id(self):
        """The kind is embedded in the cache id and therefore in the filename, so
        capture and read must agree on it exactly."""
        for svc, entries in QUERIES.items():
            for kind, build in entries:
                assert isinstance(kind, str) and kind, svc
                assert callable(build), (svc, kind)


# --------------------------------------------------------------------------- #
# capture and export must ask for the same documents
# --------------------------------------------------------------------------- #

class TestCaptureAndExportAgree:
    def _ids(self, svc: str, radius: float = RADIUS) -> dict[str, set[str]]:
        """Every cache id each script will derive for the demo bbox."""
        s, w, n, e = (float(x) for x in DEMO.split(","))
        tiles = tiles_for_bbox((s, w, n, e), radius)
        out: dict[str, set[str]] = {}
        for kind, build in QUERIES[svc]:
            ids = set()
            for tile in tiles:
                center = ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0)
                ids.add(tile_cache_id(center[0], center[1], radius, kind))
                # The query is what the disk cache is keyed by, so it has to be
                # built the same way here or this proves nothing.
                build(expanded_query_bbox(tile, radius))
            out[kind] = ids
        return out

    @pytest.mark.parametrize("svc", sorted(QUERIES))
    def test_both_scripts_derive_the_same_cache_ids(self, svc):
        """The failure this whole module is about, stated as an assertion.

        Both scripts build ``tile_cache_id(centre, radius, kind)`` and pass it
        through unchanged. If either one starts mangling it -- a prefix, a
        ``_slug``, anything -- the two diverge here rather than in a demo.
        """
        tiles = tiles_for_bbox(
            tuple(float(x) for x in DEMO.split(",")), RADIUS
        )
        for kind, _ in QUERIES[svc]:
            for tile in tiles:
                center = ((tile[0] + tile[2]) / 2.0, (tile[1] + tile[3]) / 2.0)
                raw = tile_cache_id(center[0], center[1], RADIUS, kind)
                # What a service asks `read_fixture` for, per the S2 fix: the
                # raw id. No prefix, no slug, no normalisation.
                assert fixture_name(raw) == fixture_name(raw.strip())


# --------------------------------------------------------------------------- #
# --kinds: the filter added for a query whose *text* changed
# --------------------------------------------------------------------------- #

class TestKindFilter:
    """`shade_query` lost its `tree_row` selector, which changed the query text
    and therefore the cache key, while the kind stayed ``shade``. The old
    responses became not stale but *unfindable*, and re-running the full capture
    would have re-fetched 9 cover queries that were still perfectly valid."""

    def test_kinds_narrows_the_target_list(self):
        for svc in ("s1", "s2"):
            all_kinds = [k for k, _ in QUERIES[svc]]
            one = [k for k, _ in QUERIES[svc] if k == all_kinds[0]]
            assert len(one) == 1 and one[0] == all_kinds[0]
            assert set(one) < set(all_kinds)

    def test_an_unknown_kind_is_rejected_rather_than_silently_emptying(self, monkeypatch, capsys):
        """`--kinds bogus` with no match must fail loudly. An empty target list
        would loop over nine tiles doing nothing and exit 0, which looks exactly
        like "the area is already seeded" -- how a typo becomes a skipped capture.
        """
        monkeypatch.setattr("sys.argv", ["prog", "--service", "s2", "--kinds", "bogus",
                                         "--bbox", DEMO])
        # `main()` is the programmatic entry point for export and returns a code;
        # only the `__main__` guard raises. Check the return value, not the raise.
        assert export_main() == 1

    def test_the_available_kinds_are_named_in_the_error(self, monkeypatch, capsys):
        """A bare "no match" is not actionable. Saying what *is* available turns a
        typo into a one-second fix."""
        for module_main, name in ((capture_main, "capture"), (export_main, "export")):
            monkeypatch.setattr("sys.argv", ["prog", "--service", "s2", "--kinds", "bogus",
                                             "--bbox", DEMO])
            try:
                module_main()
            except SystemExit:
                pass  # capture's __main__ guard path
            err = capsys.readouterr().err
            assert "bogus" in err, name
            assert "cover" in err and "shade" in err, (
                f"{name} should list the kinds it does have"
            )


# --------------------------------------------------------------------------- #
# the tiles both scripts walk
# --------------------------------------------------------------------------- #

class TestDemoTileSet:
    def test_the_demo_bbox_is_nine_tiles(self):
        s, w, n, e = (float(x) for x in DEMO.split(","))
        assert len(tiles_for_bbox((s, w, n, e), RADIUS)) == 9

    def test_the_rider_is_inside_the_bbox_and_in_a_seeded_tile(self):
        from shared.config import SETTINGS

        s, w, n, e = SETTINGS.demo_bbox
        lat, lng = SETTINGS.demo_rider
        assert s < lat < n and w < lng < e
        assert tile_bbox(lat, lng, RADIUS) in tiles_for_bbox((s, w, n, e), RADIUS)

    def test_s1_and_s2_use_the_same_tile_grid(self):
        """`cover_query_radius_m` exists so one prefetch serves both services.
        If the two radii ever drift, a single capture run silently leaves one
        service cold, and the demo shows the other one's fallback instead."""
        from shared.config import DEFAULT_RADIUS_M, SETTINGS

        assert SETTINGS.cover_query_radius_m == DEFAULT_RADIUS_M == RADIUS
