you are an expert engineer with extensive experience in av's and microservice architecture. Read the ENDPOINT.md file and tell me at a high level what you understand about the project and I will direct you from there

ok perfect, I want you to focus on the backend of the project, specifically the services excluding the computer vision service. Focus on the data models in the shared directory, and the orchcestrator. Ignore the web app and its frontend and backend. I want you to start on the services first specifically s1_legal_spots. You can have free reign on tradeoffs and archticture of this part of the project but I want you to always give me a proposed plan with the improvements and why you think its better than the original version. If you need a point of reference on how parts of the project should function refer to ENDPOINTS.md first before referring to me. if you have any questions always ask first before and dont assume anything
# AGENTS.md

Repo root. One populated service: `src/backend/services/vision/` — zero-shot
Mask2Former road/non-road segmentation over video. Read
`src/backend/services/vision/AGENTS.md` before working inside it; that file
holds the model traps, verification steps, and pipeline layout.

## Layout
- `.venv/` — the only environment, at the **repo root**, not in the service dir.
- `src/backend/services/vision/roadseg/` + `scripts/` — the entire codebase,
  ~1.5k lines. There is nothing else.
- No `pyproject.toml`, no lockfile, no CI, no pre-commit, no Makefile, no
  `opencode.json`, no `tests/`. The root has no test/lint/typecheck command to
  run; the service is not pip-installed either — each `scripts/*.py` does
  `sys.path.insert` on its own parent so `roadseg` imports as a plain package.

## Environment
- The venv has **no pip**. Install with uv:
  `uv pip install --python .venv/bin/python -r src/backend/services/vision/requirements.txt`
- Always the repo-root interpreter: `/home/yart/projects/endpoint/.venv/bin/python`
  (Python 3.14.4). From the service dir a relative `../../../../.venv/bin/python`
  works but prints a `RuntimeWarning: Unexpected value in sys.prefix`; prefer
  the absolute path. System python has none of the deps.

## .gitignore is anchored wrong
Root patterns `results/videos/*.mp4` and `assets/samples/*.jpg` resolve against
the repo root, but the files live under `src/backend/services/vision/`, so
**nothing is ignored**: `git check-ignore` matches no path and ~19 MB of videos
and PNGs are already tracked (largest: `results/videos/selftest_compare.png`,
14 MB). A pipeline run therefore surfaces as untracked binaries. Do not commit
them, and do not assume "outputs are gitignored". Fix by prefixing those
patterns with `src/backend/services/vision/` and untracking the blobs.

## History
Commits `0c6ae5a` and `89e90c3` ("refactor: nuked repo") deleted a previous
adverse-weather perception project (ACDC/CADC datasets, detection, tracking,
LiDAR, its `CLAUDE.md`). Nothing from it survives — do not go looking for that
code or restore it. `vision` is the current project.

## Git
- Commit style `type: short imperative` (`feat:`, `fix:`, `docs:`, `chore:`,
  `refactor:`).
- Long jobs (model downloads, multi-minute inference runs) go in `tmux`; the
  box is reached over SSH and a disconnect kills foreground work.
- Prefer `wget -c` over browser downloads for any single file over ~1 GB, and
  delete large archives after extraction.
