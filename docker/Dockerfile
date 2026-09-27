# Endpoint -- one Dockerfile, four images.
#
# One file with named stages rather than four files, which is not the usual
# preference and is here for a concrete reason. Each service wants its own image,
# and the natural way to share a base layer across images is a separate base
# image that the others say `FROM base-image:latest`. That approach needs
# something to build the base first, and Docker Compose does not order builds by
# `depends_on` -- verified, including with --with-dependencies. So a fresh clone
# fails with "pull access denied for base-image:latest", and it only works on a
# machine where somebody already built the base by hand. Failing on clean and
# working on the author's box is the worst possible property for a deploy.
#
# (BuildKit's `additional_contexts` does not rescue it either: a named context is
# used as a rootfs, and the Dockerfile inside it is never built. So `FROM base`
# yields an image containing only that Dockerfile, and the first RUN fails with
# "stat /bin/sh: no such file or directory".)
#
# Stages fix it: `target: s1` on the compose service builds this file up to the
# s1 stage. The `base` stage is built once and cached across all three targets, so
# the dependency layer is still shared, and `docker compose up --build` works on
# a clean machine with no ordering assumptions and nothing to prime first.
#
# The frontend is deliberately NOT in this file. It is a different language, a
# different base, and a different kind of artifact (a static bundle served by
# nginx); it shares no layer with the Python services, so folding it in would
# couple two things that have nothing to do with each other. It keeps its own
# docker/frontend/Dockerfile.
#
# Stage map:
#   base          shared Python runtime, no entrypoint, never run
#   s1            + services/s1_legal_spots         -> :8001
#   s2            + numpy/pandas/pvlib + s2         -> :8002
#   orchestrator  + orchestrator/                   -> :8000


# =========================================================================== #
# base -- the runtime every Python process agrees on
# =========================================================================== #
FROM python:3.14-slim AS base

# curl is for the compose healthchecks (all three services answer /health) and
# because exec-ing into a container that cannot curl is a miserable way to debug
# a timeout. Also the smallest useful answer to "is the network in here working".
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies in their own layer, ahead of any source, so editing a service does
# not invalidate a slow wheel-heavy install.
#
# This is the shared floor, derived by auditing the actual imports rather than by
# copying the repo-root requirements.txt, and the audited set is smaller than
# that file:
#
#   shared/          pydantic, httpx, python-dotenv, pyproj, shapely
#   orchestrator/    + fastapi, uvicorn, polyline
#   s1_legal_spots/  + fastapi, uvicorn            (shapely via shared)
#   s2_weather_cover/+ fastapi, uvicorn, numpy, pandas, pvlib, polyline
#
# shared/ is in the floor because all three import it -- the contracts, geometry
# and the disk cache. That is the point of it living there: a contract that drifts
# breaks every service at once, visibly, rather than one of them quietly.
#
# Two entries in the repo-root requirements.txt are absent because nothing in the
# tree imports them. `rasterio` was for the Google Solar API raster path, which
# was never built (S2 shades from OSM geometry via pvlib and says so in
# `shade_source`), and it drags GDAL and its C libraries in with it. `anthropic`
# was for S3, which does not exist. Dropping rasterio is most of the image-size
# difference between this and a naive single-image build. requirements.txt itself
# is left untouched: it is what the local .venv was built from.
RUN pip install --no-cache-dir \
        "fastapi" \
        "uvicorn[standard]" \
        "httpx" \
        "pydantic" \
        "python-dotenv" \
        "shapely" \
        "pyproj" \
        "polyline"

COPY shared/ /app/shared/
COPY scripts/ /app/scripts/

# Seed the Overpass disk cache into the image.
#
# shared/config.py derives REPO_ROOT from __file__, so with shared/ at
# /app/shared, DATA_DIR is /app/data and OVERPASS_CACHE_DIR is
# /app/data/cache/overpass -- no code change and no env var needed to make the
# volume mount point correct. Docker seeds a named volume from whatever the image
# holds at that path when the volume is first created, and writes go to the volume
# from then on. That is the whole mechanism: a first `docker compose up` answers
# from this cache, and later ones persist their refreshes.
#
# Copied rather than mounted at build time because these are the working copy of
# a gitignored cache, and the raw Overpass bytes are what MOCK=1 replays. This
# is the one place the cache becomes part of a deliverable, which is what makes a
# deploy reproducible with no network at all.
COPY data/cache/ /app/data/cache/

# Unprivileged, and the cache must be writable by it: S1 and S2 both persist into
# it, and a read-only cache turns every refetch into a silent failure to store.
RUN useradd --create-home --uid 10001 endpoint \
    && mkdir -p /app/data/cache \
    && chown -R endpoint:endpoint /app
USER endpoint

# Fail the build, not the first request of a demo, if a wheel is missing or the
# resolver paired something badly. The error here names the dependency.
RUN python -c "import shared.config, shared.models, shared.geo, shared.fixtures; print('base ok')"


# =========================================================================== #
# s1 -- Legal Spots. ENDPOINT.md section 6. :8001
# =========================================================================== #
# Adds no dependencies beyond the base, which is the actual dependency graph
# rather than an omission: S1's own imports are stdlib plus shapely, and shapely
# is already here because shared/geo.py projects through it.
#
#   POST /spots/legal      legal stopping spots near the rider
#   GET  /health           liveness (what compose gates on)
#   GET  /ready            503 unless the demo area is cached
#   GET  /spots/explain    dev only; stripped when STRIP_DEV_ROUTES=1
#   POST /admin/prefetch   warm the Overpass cache for a bbox
FROM base AS s1

# fixtures/ included: MOCK=1 reads them through shared.fixtures.fixtures_dir(),
# which resolves to REPO_ROOT/services/s1_legal_spots/fixtures. Baking them in is
# what lets a fresh clone come up with no network.
COPY services/s1_legal_spots/ /app/services/s1_legal_spots/

# Import the app at build time so a broken import is a build failure with a real
# traceback, rather than a container that exits on boot and takes the
# orchestrator's startup ordering down with it.
RUN python -c "from services.s1_legal_spots.main import app; print('s1 ok', len(app.routes), 'routes')"

EXPOSE 8001

# 0.0.0.0 explicitly. Uvicorn's default 127.0.0.1 is unreachable from the
# orchestrator's network namespace even with the port exposed, and the symptom is
# a connection refused that reads as "S1 is down".
CMD ["uvicorn", "services.s1_legal_spots.main:app", \
     "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]


# =========================================================================== #
# s2 -- Weather and Cover. ENDPOINT.md section 6. :8002
# =========================================================================== #
# The only stage with a real dependency delta. The scientific stack lives here
# and nowhere else:
#
#   numpy   walk_network.py, and the arithmetic under rain_exposure.py
#   pandas  weather.py, for the Open-Meteo hourly window
#   pvlib   weather.py, for solar position -- DST, refraction, equation of time
#
# pvlib is not incidental. Hand-rolling solar position is the kind of thing that
# is right for a week and wrong forever, and the sun side-flip demo depends on
# shadows genuinely reversing between 10:00 and 16:00 local. weather.py imports
# pandas and pvlib lazily inside a try/except (weather.py:307-309), so a missing
# wheel would degrade this service's classification instead of crashing it. That
# is a good fallback and a bad reason to leave them out, so they are installed and
# the fallback stays a fallback.
#
#   POST /conditions/rank     weather at pickup time, then a ranking suited to it
#   GET  /health              liveness (what compose gates on)
#   GET  /ready               what is cached
#   GET  /conditions/weather  weather only, for a UI badge
#   GET  /conditions/features raw cover features, for the map layer
#   POST /walk/routes         accessible walking routes, no ranking
FROM base AS s2

RUN pip install --no-cache-dir "numpy" "pandas" "pvlib"

# fixtures/ for the same reason as S1: S2's mock replay reads
# services/s2_weather_cover/fixtures, and the cover, shade and paths responses
# are exactly what the rain and sun scenarios are demonstrated against.
COPY services/s2_weather_cover/ /app/services/s2_weather_cover/

RUN python -c "from services.s2_weather_cover.main import app; print('s2 ok', len(app.routes), 'routes')"

EXPOSE 8002

CMD ["uvicorn", "services.s2_weather_cover.main:app", \
     "--host", "0.0.0.0", "--port", "8002", "--no-access-log"]


# =========================================================================== #
# orchestrator -- ENDPOINT.md section 7. :8000
# =========================================================================== #
# Depends on the base only. No services/ directory is copied, deliberately rather
# than by oversight: the orchestrator reaches S1 and S2 over HTTP and never
# imports them, so shipping their code here would be files it cannot use and would
# quietly invite someone to reach across the network boundary. shared/models.py is
# the right amount of coupling -- it knows the contract, not the implementation.
#
# S1_URL / S2_URL are set in docker-compose.yml rather than here, because they are
# deployment facts (compose service names) and not properties of the app. They are
# honoured by the env overrides at the bottom of shared/config.py; before that was
# wired up, the orchestrator dialled localhost:8001 inside its own container,
# failed every call, and degraded every ride while reporting itself healthy. See
# the comment on the S1_URL handling in shared/config.py.
#
# Ride state is in memory by design (orchestrator/ride.py), so this container is
# the single source of truth for live rides and a restart drops them. Replicas
# would need sticky routing or a real store; one container is the right shape for
# the demo and honest about the limitation.
#
#   POST /rides/request               start a ride, returns the mobility question
#   POST /rides/{id}/answer           predictive phase (S1 + S2)
#   POST /rides/{id}/confirm          the detour question
#   GET  /rides/{id}                  plan + car position, polled at 1 Hz
#   POST /rides/{id}/skip_to_arrival  demo shortcut
#   GET  /health  /ready              per-dependency readiness
FROM base AS orchestrator

# polyline, used by orchestrator/routing.py and orchestrator/simulator.py to
# decode route geometry, is in the base because S2 needs it too. Promoting it to
# this stage instead would just make S2 reinstall it.
COPY orchestrator/ /app/orchestrator/

RUN python -c "from orchestrator.main import app; print('orchestrator ok', len(app.routes), 'routes')"

EXPOSE 8000

CMD ["uvicorn", "orchestrator.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
