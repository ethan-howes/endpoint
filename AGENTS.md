you are an expert engineer with extensive experience in av's and microservice architecture. Read the ENDPOINT.md file and tell me at a high level what you understand about the project and I will direct you from there

ok perfect, I want you to focus on the backend of the project, specifically the services excluding the computer vision service. Focus on the data models in the shared directory, and the orchcestrator. Ignore the web app and its frontend and backend. I want you to start on the services first specifically s1_legal_spots. You can have free reign on tradeoffs and archtecture of this part of the project but I want you to always give me a proposed plan with the improvements and why you think its better than the original version. If you need a point of reference on how parts of the project should function refer to ENDPOINTS.md first before referring to me. if you have any questions always ask first before and dont assume anything

# AGENTS.md

Robotaxi pickup-spot selection. `ENDPOINT.md` is the specification and the
source of truth for behaviour; this file is the map of the code. Read
`ENDPOINT.md` before changing anything that has a rule attached to it, and
`README.md` for how to run and verify.

## Layout

```
shared/            the Pydantic contracts, geometry, config, Overpass cache
orchestrator/      FastAPI; owns the ride, degrades rather than fails
services/
  s1_legal_spots/  legal kerbside stopping places, ranked
  s2_weather_cover/rain/sun exposure of a walk, cover from OSM
  vision/          batch Mask2Former road segmentation. NOT a service.
frontend/          Vite SPA, served by nginx, talks only to the orchestrator
scripts/           prefetch, smoke test, fixture capture, verifiers
tests/             571 tests, hermetic, no network
docker/            Dockerfiles and the nginx config
docker-compose.yml the one-command deploy: 4 containers
Makefile           thin wrapper over docker compose
```

- `.venv/` at the **repo root** is the only environment (Python 3.14).
- No `pyproject.toml`, no lockfile, no CI, no pre-commit. Packages are plain
  directories on `sys.path`; there is nothing to install the project itself.
- `services/vision/` is a batch pipeline with no `main:app` and it needs a GPU.
  It is excluded from compose, from the smoke test, and from `shared/`.

## Commands

```bash
.venv/bin/python -m pytest                    # 571 tests, ~4s, no network
make up                                        # build + run all four containers
make smoke                                     # drive a real ride end to end
make ready                                     # per-dependency readiness
make logs                                      # tail all four
make prefetch                                  # warm the Overpass cache (network)
make test                                      # pytest in the venv
```

`ENDPOINT_MOCK=1` (the compose default) replays committed fixtures with no
network at all. `make smoke` reads the service addresses from
`shared/config.py`, so it needs `-e S1_URL=... -e S2_URL=...` to reach
containers rather than localhost.

## Traps

**`S1_URL`/`S2_URL`/`S3_URL` were documented but never read.** They are in
`ENDPOINT.md` section 8 and in `.env.example`, and `orchestrator/clients.py`
reads them off `SETTINGS`, but until recently only the dataclass defaults
populated those three fields. Invisible on a laptop, where `localhost:8001` is
the right answer; fatal in compose, where the orchestrator has nothing on 8001
in its own network namespace. The compounding part is that `clients.py`
(correctly, per section 4.5) turns a failed call into a logged fallback, so the
stack boots clean, passes every healthcheck, and quietly degrades every ride.
`shared/config.py` now honours them, and
`tests/test_config_env.py::test_reaches_the_orchestrator_client` pins it by
asserting through the real client rather than through `SETTINGS`.

**Config is read at import time and rebinds the module-level `SETTINGS`.**
Anything that holds `from shared.config import SETTINGS` keeps its own
binding, so reloading `shared.config` is not enough to change what
`orchestrator/clients.py` dials. Both modules have to be reloaded. That is why
the config tests reload explicitly and restore on the way out.

**Cache paths are derived, not configured.** `shared/config.py` derives
`REPO_ROOT` from `__file__`, so `DATA_DIR` follows wherever `shared/` sits. The
compose volume at `/app/data/cache` works only because the image puts
`shared/` at `/app/shared`. Tidying this into an env-backed path silently moves
the data out from under the volume, with no error anywhere.

**Two BuildKit approaches were tried and do not work here.** Don't retry them:
- A separate `endpoint/base` image with `FROM endpoint/base:latest` fails on a
  clean clone with `pull access denied`. Compose does not order builds by
  `depends_on`, verified including `--with-dependencies`.
- `additional_contexts: {base: ./docker/base}` with `FROM base` produces an
  image containing only the Dockerfile, because named contexts are rootfs, not
  build targets. It then fails on `stat /bin/sh: no such file or directory`.
  (This compose version also requires a string, not `{context: ...}`.)

Use the `target:` stages in `docker/Dockerfile` instead. The frontend build
context must be the **repo root**, not `./frontend`, or 113 MB of
`node_modules` ships and `docker/frontend/nginx.conf` is unresolvable.

**`.env` is read twice, for two different things.** Compose reads the repo-root
`.env` for interpolation, and `shared/config.py` reads it for the services. That
file sets `S1_URL=http://localhost:8001` for host development, which is wrong
inside compose — so compose-level knobs are prefixed `ENDPOINT_*` and the three
service URLs are hardcoded and deliberately not overridable.

## History

Commits `0c6ae5a` and `89e90c3` ("refactor: nuked repo") deleted a previous
adverse-weather perception project (ACDC/CADC datasets, detection, tracking,
LiDAR, its `CLAUDE.md`). Nothing from it survives — do not go looking for that
code or restore it.

## Git

- Commit style `type: short imperative` (`feat:`, `fix:`, `docs:`, `chore:`,
  `refactor:`).
- Long jobs (model downloads, multi-minute inference runs) go in `tmux`; the
  box is reached over SSH and a disconnect kills foreground work.
- Prefer `wget -c` over browser downloads for any single file over ~1 GB, and
  delete large archives after extraction.
- `.gitignore` is anchored wrong for the vision service: root patterns
  `results/videos/*.mp4` and `assets/samples/*.jpg` resolve against the repo
  root, but the files live under `services/vision/`, so **nothing is ignored**
  and ~19 MB of videos and PNGs are tracked (largest:
  `results/videos/selftest_compare.png`, 14 MB). Do not commit pipeline output
  and do not assume "outputs are gitignored". Fix by prefixing those patterns
  with `services/vision/` and untracking the blobs.
- `data/cache/overpass` is gitignored but deliberately **baked into the images**
  so the stack works with no network. It is ~8.2 MB, 94 files. Regenerate with
  `make prefetch` rather than committing it.
