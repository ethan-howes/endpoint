# Endpoint -- one command for the common things.
#
#   make up        build and start everything (this is the deploy)
#   make smoke     prove a real ride works end to end
#   make down      stop, keep images and the Overpass cache
#
# Everything here is a thin wrapper over `docker compose`. Nothing in this file
# is required to run the stack -- `docker compose up --build` works on its own.
# It exists so the commands you actually type are short and so the fiddly ones
# (running a script inside a container, pointing the smoke test at the compose
# network) are written down once instead of rediscovered under pressure.
#
# Configuration is passed as ENDPOINT_* environment variables rather than read
# from the repo's .env, on purpose. .env is the host-development file: it sets
# S1_URL=http://localhost:8001, which is correct for `python -m uvicorn` on this
# machine and wrong inside the compose network. Compose reads .env for variable
# substitution, so reusing those names would quietly point the containers at
# themselves. See the note at the top of docker-compose.yml.

SHELL := /bin/bash
COMPOSE := docker compose

# Host ports, matching the compose defaults. Overridable the same way as in
# docker-compose.yml, so `make up ENDPOINT_FRONTEND_PORT=8080` does what it says.
ORCH_PORT ?= 8000
FRONTEND_PORT ?= 5500

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@echo "Endpoint -- docker compose"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  frontend  http://localhost:$(FRONTEND_PORT)"
	@echo "  api docs  http://localhost:$(ORCH_PORT)/docs"

# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

.PHONY: up
up: ## Build and start the whole stack (the one-command deploy)
	$(COMPOSE) up --build -d
	@$(MAKE) --no-print-directory wait
	@echo
	@$(MAKE) --no-print-directory ready

.PHONY: down
down: ## Stop the stack, keeping images and the Overpass cache
	$(COMPOSE) down

.PHONY: restart
restart: down up ## Stop and start again

.PHONY: ps
ps: ## Show container status and health
	$(COMPOSE) ps

# Wait for every healthcheck to pass, so `make up` does not return while the
# orchestrator is still waiting on S1. Without this, a script that runs `make up`
# and immediately drives a ride races the startup and reports a connection
# refused that is not a real fault.
.PHONY: wait
wait: ## Block until all four containers report healthy
	@echo "waiting for healthchecks..."
	@for i in $$(seq 1 60); do \
		pending=$$($(COMPOSE) ps --format '{{.Service}} {{.Health}}' 2>/dev/null \
			| awk '$$2 != "healthy" && $$2 != "" {print $$1}'); \
		if [ -z "$$pending" ]; then echo "all healthy"; exit 0; fi; \
		sleep 2; \
	done; \
	echo "TIMEOUT waiting for: $$pending"; $(COMPOSE) ps; exit 1

.PHONY: rebuild
rebuild: ## Rebuild every image from scratch, ignoring the layer cache
	$(COMPOSE) build --no-cache --pull

# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

.PHONY: smoke
smoke: ## Drive a real ride end to end (needs `make up` first)
	@echo "smoke test against the running stack..."
	@$(COMPOSE) exec -T \
		-e S1_URL=http://s1:8001 \
		-e S2_URL=http://s2:8002 \
		s1 python -m scripts.smoke_test --orchestrator http://orchestrator:8000
	@echo
	@echo "The -e flags are load-bearing. scripts/smoke_test.py takes the"
	@echo "orchestrator URL as an argument but reads S1 and S2 from SETTINGS, and"
	@echo "inside the s1 container localhost:8002 is nothing. Pointing them at the"
	@echo "compose service names is what lets it reach S2."

.PHONY: ready
ready: ## Report per-dependency readiness and whether the demo data is cached
	@echo "orchestrator /ready (per-dependency):"
	@curl -fsS http://localhost:$(ORCH_PORT)/ready | python3 -m json.tool || true
	@echo
	@echo "S1 cache state for the demo rider (503 here means the area is not cached):"
	@$(COMPOSE) exec -T s1 curl -sS -o /dev/null -w '  HTTP %{http_code}\n' \
		"http://localhost:8001/ready?lat=25.7584&lng=-80.3725" || true
	@$(COMPOSE) exec -T s1 curl -sS "http://localhost:8001/ready?lat=25.7584&lng=-80.3725" \
		| python3 -m json.tool 2>/dev/null || true

.PHONY: test
test: ## Run the pytest suite in the local venv
	@echo "Note: this runs against .venv, not the images. The images prove their own"
	@echo "dependency set at build time (a RUN python -c 'import ...' in each"
	@echo "Dockerfile) and `make smoke` covers the integration path."
	.venv/bin/python -m pytest -q

# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #

.PHONY: prefetch
prefetch: ## Warm the Overpass cache over the network (needed for ENDPOINT_MOCK=0)
	@echo "prefetching the demo area into the shared cache volume..."
	@$(COMPOSE) exec -T \
		-e DEMO_BBOX="$${ENDPOINT_DEMO_BBOX:-25.7533,-80.3762,25.7605,-80.3682}" \
		s1 python -m scripts.prefetch_demo_area
	@echo
	@echo "No container is started for this. scripts/ is in every Python image and"
	@echo "s1's image has services/s1_legal_spots, which is what the prefetcher"
	@echo "imports to build its queries. It writes into the same named volume S1 and"
	@echo "S2 read, so one run serves both."

.PHONY: cache-size
cache-size: ## Show how much Overpass data is cached
	@$(COMPOSE) run --rm --no-deps -T s1 sh -c \
		'du -sh /app/data/cache 2>/dev/null; ls /app/data/cache/overpass 2>/dev/null | wc -l | xargs echo "  responses:"'

.PHONY: reset-cache
reset-cache: ## Delete the Overpass cache volume (next up re-seeds it from the image)
	$(COMPOSE) down
	$(COMPOSE) volume rm endpoint-overpass-cache

# --------------------------------------------------------------------------- #
# Debugging
# --------------------------------------------------------------------------- #

.PHONY: logs
logs: ## Tail logs from all four containers
	$(COMPOSE) logs -f --tail=100

.PHONY: logs-s1
logs-s1: ## Tail S1's log
	$(COMPOSE) logs -f --tail=100 s1

.PHONY: logs-s2
logs-s2: ## Tail S2's log
	$(COMPOSE) logs -f --tail=100 s2

.PHONY: logs-orchestrator
logs-orchestrator: ## Tail the orchestrator's log
	$(COMPOSE) logs -f --tail=100 orchestrator

.PHONY: shell
shell: ## Shell into a container (make shell S=s1)
	$(COMPOSE) exec $(S) bash

.PHONY: clean
clean: ## Remove containers, images and the cache volume
	$(COMPOSE) down --rmi local --volumes

.PHONY: nuke
nuke: clean ## Also remove build cache
	$(COMPOSE) down --rmi all --volumes
	docker builder prune -af
