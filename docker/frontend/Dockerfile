# Frontend. The rider app (Vite + React + Leaflet), built to static files and
# served by nginx. This is the only container with a published UI port, and the
# only one a browser ever needs to reach.
#
# Two stages: node builds, nginx serves. The runtime image contains no Node, no
# node_modules and no source -- just dist/ and a webserver -- which is roughly
# 50 MB instead of the ~600 MB a single-stage build would leave behind.
#
# The API base URL is the interesting decision, and it is why this image is
# portable at all. Vite freezes VITE_* variables at build time (client.ts reads
# import.meta.env.VITE_API_URL and falls back to http://localhost:8000), so
# baking an absolute URL produces an image that only works when the browser
# happens to be on the same machine as the orchestrator. Point a phone at
# http://<laptop-ip>:5500 and it breaks.
#
# So the default is the RELATIVE path /api, and nginx proxies /api/ to the
# orchestrator over the compose network. A relative path resolves against
# whatever host served the page, so one image works on localhost, on a LAN IP,
# and behind a domain name, with no rebuild. It also means the browser makes
# same-origin requests, so the orchestrator's permissive CORS middleware
# (allow_origins=["*"], correct for a local demo and noted as such in
# orchestrator/main.py) is simply never consulted.
#
# To point at an orchestrator somewhere else -- a remote demo box, a staging
# host -- override the build arg, which is the escape hatch this design keeps:
#   docker compose build --build-arg VITE_API_URL=https://api.example.com frontend
# Absolute URLs work fine through the proxy too; the /api default is a
# convenience, not a requirement.

# ---------------------------------------------------------------------------
# Stage 1: build
# ---------------------------------------------------------------------------
# Build context is the REPO ROOT, not ./frontend, so that the root
# .dockerignore applies. frontend/node_modules is already on disk and is ~110 MB
# of it; a context rooted at ./frontend ships that to the daemon on every build.
# The root .dockerignore excludes it, along with frontend/dist and any
# frontend/.env, so the context is just the ~2 MB of app source.
FROM node:22-alpine AS build

# Vite 8 requires Node 20.19+ or 22.12+.
WORKDIR /app

# VITE_API_URL defaults to the relative /api. See the note above; the value is
# only meaningful at build time, which is precisely the constraint this design
# works around.
ARG VITE_API_URL=/api
# Set to 1 to ship the in-browser placeholder API instead of the orchestrator
# (frontend/src/api/client.ts). Useful for UI work with no backend running.
ARG VITE_USE_PLACEHOLDER=0
ENV VITE_API_URL=$VITE_API_URL \
    VITE_USE_PLACEHOLDER=$VITE_USE_PLACEHOLDER

# Dependencies before source, so editing the app does not reinstall node_modules.
# npm ci (not npm install) because package-lock.json is committed: ci installs
# exactly the locked tree and fails loudly if the lock and package.json disagree,
# which is the difference between a reproducible image and a subtly different one.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./

# `npm run build` is `tsc --noEmit && vite build`, so a type error fails the image
# build. Worth having: this app is the only part of the project with a compiler
# enforcing anything, and letting a type error through to a demo is worse than
# not shipping.
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: serve
# ---------------------------------------------------------------------------
FROM nginxinc/nginx-unprivileged:1.29-alpine AS runtime

# The unprivileged image already runs as uid 101 on port 8080, so no su/gosu and
# no chown of nginx's state directories -- which is the usual reason a
# "non-root nginx" ends up running as root anyway.
COPY docker/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

# 8080, matching the image's own default and the non-root bind. Publishing
# 5500 on the host preserves the port from scripts/run_all.sh, so nothing that
# worked on the host stops working.
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:8080/ || exit 1
