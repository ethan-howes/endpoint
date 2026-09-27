"""Config-from-environment tests.

`shared/config.py` reads the environment at import time and then rebinds the
module-level ``SETTINGS``. That makes it awkward to test in the obvious way and
easy to leave untested, which is how `S1_URL` / `S2_URL` / `S3_URL` stayed
documented in ENDPOINT.md section 8 and `.env.example` while being read from
nothing: `orchestrator/clients.py` read them off `SETTINGS`, and the only thing
that ever populated those three fields was the dataclass default.

The consequence was invisible on a laptop and severe everywhere else. On a host,
`localhost:8001` is the correct address, so nothing appeared broken. In a
container it is not: the orchestrator has nothing listening on 8001 in its own
network namespace, and `orchestrator/clients.py` -- correctly, per section 4.5 --
turns every failed call into a logged fallback instead of an error. The stack
would boot, pass every healthcheck, report itself ready, and quietly degrade
every ride.

So these tests are about the *wiring* rather than any threshold. They reload the
module under a patched environment, which is the only way to exercise
import-time configuration, and they assert the addresses the orchestrator will
actually dial.
"""

from __future__ import annotations

import importlib
import os
from contextlib import contextmanager

import pytest

import shared.config as config_module


@contextmanager
def _reload_with_env(monkeypatch: pytest.MonkeyPatch, **env: str):
    """Re-import shared.config with ``env`` applied, and restore it afterwards.

    ``monkeypatch`` undoes the environment, but it cannot undo the rebinding of
    ``SETTINGS`` inside an already-imported module -- and ``orchestrator`` and both
    services hold a reference to the object, not the module. So the module is
    restored explicitly on the way out. Without that, a test that set ``S1_URL``
    would leak a compose-style address into every later test in the session, which
    is a genuinely confusing failure to debug.

    ``monkeypatch`` is accepted for symmetry with the rest of the suite, but the
    environment is managed by hand here: the cleanup has to happen *before* the
    restoring reload, and monkeypatch's teardown runs after this context exits, so
    relying on it would reload the module with the patched values still in place.
    """
    previous = {key: os.environ.get(key) for key in env}
    for key, value in env.items():
        os.environ[key] = value
    try:
        yield importlib.reload(config_module)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(config_module)


class TestServiceUrls:
    def test_defaults_are_localhost(self, monkeypatch: pytest.MonkeyPatch):
        """With nothing set, the host-development defaults must survive.

        These are what `./scripts/run_all.sh` and a bare `uvicorn` depend on, so
        they are the one thing that must not change.
        """
        for key in ("S1_URL", "S2_URL", "S3_URL"):
            monkeypatch.delenv(key, raising=False)
        reloaded = importlib.reload(config_module)
        try:
            assert reloaded.SETTINGS.s1_url == "http://localhost:8001"
            assert reloaded.SETTINGS.s2_url == "http://localhost:8002"
            assert reloaded.SETTINGS.s3_url == "http://localhost:8003"
        finally:
            importlib.reload(config_module)

    def test_env_overrides_the_defaults(self, monkeypatch: pytest.MonkeyPatch):
        """The regression test. Compose sets service names, not localhost.

        This is the exact configuration docker-compose.yml uses, and it is the one
        that has to reach `orchestrator/clients.py::base_url`. Before the override
        was wired up, this assertion failed while every other test in the suite
        stayed green -- which is the whole reason it is worth pinning.
        """
        with _reload_with_env(
            monkeypatch,
            S1_URL="http://s1:8001",
            S2_URL="http://s2:8002",
            S3_URL="http://s3:8003",
        ) as reloaded:
            assert reloaded.SETTINGS.s1_url == "http://s1:8001"
            assert reloaded.SETTINGS.s2_url == "http://s2:8002"
            assert reloaded.SETTINGS.s3_url == "http://s3:8003"

    def test_reaches_the_orchestrator_client(self, monkeypatch: pytest.MonkeyPatch):
        """The value has to arrive where the call is actually made.

        Asserting on `SETTINGS` alone would pass even if some other layer rebuilt
        the dataclass. `base_url` is what `call_service` dials, so this is the
        assertion that would have caught the original bug.
        """
        import orchestrator.clients as clients

        with _reload_with_env(
            monkeypatch, S1_URL="http://s1:8001", S2_URL="http://s2:8002"
        ) as reloaded:
            # clients.py holds its own `from shared.config import SETTINGS` binding,
            # captured at ITS import time, so reloading shared.config is not enough --
            # the orchestrator module has to be reloaded too. That indirection is the
            # reason this is worth a test rather than a glance.
            reloaded_clients = importlib.reload(clients)
            try:
                assert reloaded_clients.base_url("S1") == "http://s1:8001"
                assert reloaded_clients.base_url("S2") == "http://s2:8002"
                # S3 stays at its default here: nothing sets it, and an unset var
                # must not blank the field.
                assert reloaded_clients.base_url("S3") == reloaded.SETTINGS.s3_url
            finally:
                importlib.reload(clients)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_does_not_erase_the_default(
        self, monkeypatch: pytest.MonkeyPatch, blank: str
    ):
        """An empty or whitespace value must fall back, not blank the address.

        A blanked URL would reach httpx as ``http://:8001/spots/legal`` and fail
        as a malformed request rather than as "S1 unreachable", which sends you
        looking at the wrong thing. Docker Compose substitutes an empty string for
        an unset variable in some invocation styles, so this is reachable.
        """
        with _reload_with_env(monkeypatch, S1_URL=blank) as reloaded:
            assert reloaded.SETTINGS.s1_url == "http://localhost:8001"

    def test_surrounding_whitespace_is_stripped(self, monkeypatch: pytest.MonkeyPatch):
        """A trailing newline from a `.env` file must not become part of the URL.

        `S1_URL=http://s1:8001\n` is what a hand-edited env file or a
        `docker inspect` copy-paste produces. httpx would treat it as a different
        host and fail with a name-resolution error, which is indistinguishable
        from "service not running".
        """
        with _reload_with_env(monkeypatch, S1_URL="  http://s1:8001  ") as reloaded:
            assert reloaded.SETTINGS.s1_url == "http://s1:8001"


class TestOtherEnvOverrides:
    """The pre-existing overrides, checked for the interaction with the new ones.

    These already worked. They are here because the service URLs are now applied
    in the same block, and a change that fixed the URLs must not have disturbed
    the values that were already honoured.
    """

    def test_mock(self, monkeypatch: pytest.MonkeyPatch):
        with _reload_with_env(monkeypatch, MOCK="1") as reloaded:
            assert reloaded.SETTINGS.mock is True

    def test_demo_bbox(self, monkeypatch: pytest.MonkeyPatch):
        with _reload_with_env(monkeypatch, DEMO_BBOX="25.1,-80.2,25.3,-80.0") as reloaded:
            assert reloaded.SETTINGS.demo_bbox == (25.1, -80.2, 25.3, -80.0)

    def test_demo_rider(self, monkeypatch: pytest.MonkeyPatch):
        with _reload_with_env(monkeypatch, DEMO_RIDER="25.5,-80.5") as reloaded:
            assert reloaded.SETTINGS.demo_rider == (25.5, -80.5)

    def test_traffic_side(self, monkeypatch: pytest.MonkeyPatch):
        with _reload_with_env(monkeypatch, TRAFFIC_SIDE="left") as reloaded:
            assert reloaded.SETTINGS.traffic_side == "left"

    def test_service_urls_and_other_overrides_coexist(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Both blocks applied in one pass, which is how compose invokes it.

        Compose sets every one of these at once. A bug where one block overwrote
        or reset the other would only appear in the container, never in a test
        that set a single variable.
        """
        with _reload_with_env(
            monkeypatch,
            MOCK="1",
            TRAFFIC_SIDE="right",
            DEMO_TZ="America/New_York",
            S1_URL="http://s1:8001",
            S2_URL="http://s2:8002",
            S3_URL="http://s3:8003",
        ) as reloaded:
            s = reloaded.SETTINGS
            assert s.s1_url == "http://s1:8001"
            assert s.s2_url == "http://s2:8002"
            assert s.s3_url == "http://s3:8003"
            assert s.mock is True
            assert s.traffic_side == "right"
            assert s.demo_tz == "America/New_York"
            # Untouched fields must keep their dataclass defaults.
            assert s.timeout_s1_s == 3.0
            assert s.timeout_s2_s == 6.0


class TestCachePaths:
    """The Docker volume mount point depends on these being derived, not configured.

    `docker-compose.yml` mounts a named volume at `/app/data/cache` and expects
    the services to write their Overpass cache there. That works only because
    `REPO_ROOT` is derived from `__file__` and the image puts `shared/` at
    `/app/shared`, so `DATA_DIR` lands on `/app/data` with no environment
    variable involved. If someone "tidies" this into an env-backed path, the
    volume silently stops being where the data is, and every request starts
    missing cache with no error anywhere -- the same class of failure the
    prefetch script was written to prevent.
    """

    def test_data_dir_is_derived_from_the_package_location(self):
        assert config_module.REPO_ROOT == config_module.Path(
            config_module.__file__
        ).resolve().parent.parent
        assert config_module.DATA_DIR == config_module.REPO_ROOT / "data"
        assert (
            config_module.OVERPASS_CACHE_DIR
            == config_module.REPO_ROOT / "data" / "cache" / "overpass"
        )

    def test_cache_dir_is_where_compose_mounts_the_volume(self):
        """The mount point and the cache path have to be the same directory.

        Written as a path comparison rather than a hardcoded `/app/data/cache`, so
        it keeps testing the *relationship* when run on the host, where the paths
        are not under /app. The /app prefix is checked separately below, and only
        when the image is what is under test.
        """
        assert config_module.OVERPASS_CACHE_DIR.parent.name == "cache"
        assert config_module.OVERPASS_CACHE_DIR.parent.parent.name == "data"

    def test_overpass_cache_dir_matches_the_image_layout(self):
        """Under the image, this is exactly where compose mounts the volume.

        Skipped on a host checkout, where REPO_ROOT is the repo directory. The
        assertion is the contract between docker/Dockerfile and
        docker-compose.yml: `shared/` goes to `/app/shared`, so `DATA_DIR` has to
        be `/app/data`.
        """
        if config_module.REPO_ROOT.parent != config_module.Path("/app"):
            pytest.skip("not running from the image; /app layout not in play")
        assert config_module.OVERPASS_CACHE_DIR == config_module.Path(
            "/app/data/cache/overpass"
        )
