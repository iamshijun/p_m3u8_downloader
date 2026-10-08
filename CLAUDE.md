# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A single-file Python CLI tool (`m3u8_downloader.py`) that downloads HLS video streams from M3U8 playlists, with AES-128 decryption support. Zero required dependencies beyond Python stdlib. Optional deps: `tqdm` (progress bar), `pycryptodome` (AES decryption).

## Commands

```bash
# Syntax check
python3 -c "import py_compile; py_compile.compile('m3u8_downloader.py', doraise=True)"

# Run
python3 m3u8_downloader.py <m3u8_file_or_url> [options]

# Tests (from repo root; needs requirements.txt deps, stdlib `unittest` only)
.venv/bin/python -m unittest
.venv/bin/python -m unittest -v tests.test_m3u8_source_validation
```

### Tests (`tests/`)

`tests/test_m3u8_source_validation.py` guards the m3u8-source validation contract:
- `ValidateM3u8SourceTest` — table-driven cases for `api_server.validate_m3u8_source()`
- `DownloadEndpointValidationTest` — `TestClient` contract for both entries (`/api/download` → 400, `/api/download/batch` → per-line `errors`), with `manager` replaced by a fake so no test touches the network or writes to `downloads/`

The `CASES` table in that file is the **shared contract** for three places that must change together: `api_server.validate_m3u8_source()`, `web/js/utils/validate.js` `validateM3u8Source()`, and the table itself.

`tests/test_resolution_limit.py` guards the `--resolution` (清晰度上限) contract:
- `ParseResolutionLimitTest` — `m3u8_downloader.parse_resolution_limit()` (the single parser/validator; `"480p"` → `(None, 480)`, `"854x480"` → `(854, 480)`; invalid raises `ValueError` with a Chinese message)
- `SelectVariantByLimitTest` — variant selection against a fake `HTTPClient`: height-only cap, dual-dimension cap (a too-wide `1920x480` loses to `854x480`), all-above-limit → `M3U8DownloadError` listing available resolutions, no `RESOLUTION` on any variant → warn + fall back to bandwidth
- `ValidateResolutionLimitTest` / `DownloadEndpointResolutionTest` — `api_server.validate_resolution_limit()` and the `/api/download` (400) + `/api/download/batch` (per-line `errors`) contract

Its `PARSE_CASES` / `INVALID_CASES` tables are likewise the **shared contract** for `m3u8_downloader.parse_resolution_limit()`, `api_server.validate_resolution_limit()`, `web/js/utils/validate.js` `validateResolutionLimit()`, and the tables themselves — the API/JS layers only re-wrap the downloader's message, so all three must keep returning the same text.

## Architecture

```
dataclasses:
  KeyInfo       — AES-128 key (method, key_uri, key_data, iv)
  M3U8Segment   — single segment (uri, duration, key)
  M3U8Variant   — one stream of a master playlist (uri, bandwidth, width, height, resolution)
  M3U8Playlist  — parsed playlist (segments, variants, version, media_sequence, is_encrypted)

classes:
  HTTPClient    — wraps urllib.request with proxy/headers/timeout. Methods: get(), get_text()
  M3U8Downloader — orchestrator. Constructor takes all config, run() executes full pipeline

pipeline: fetch → parse → [select master variant] → resolve_base_url → load_keys → download_segments → [decrypt] → merge
```

When `--no-merge` is set, the merge step is skipped; downloaded segments and the m3u8 file are moved to `{output}_segments/` directory.

## Unified Downloader

The `unified_downloader.py` script replaces individual wrapper scripts with intelligent domain detection:

```bash
# Auto-detects domain and applies appropriate configuration
python3 unified_downloader.py <m3u8_url> [options...]

# Examples:
python3 unified_downloader.py https://missav.ai/video.m3u8
python3 unified_downloader.py https://7mmtv.sx/video.m3u8 --no-merge

# View configuration
python3 unified_downloader.py --config

# Get help
python3 unified_downloader.py --help
```

Domain configuration is stored in `domain_config.{json,toml}`. The script tries TOML first (if `toml` package is installed), then falls back to JSON.

## Web API server (`api_server.py`)

A FastAPI wrapper that exposes `m3u8_downloader` as an HTTP service with a multi-task queue and streaming progress. It **imports** `M3U8Downloader` / hooks as a library and does not modify the downloader.

```bash
./scripts/start.sh                 # background (auto .venv + pip sync, writes run/server.pid)
./scripts/start.sh foreground      # foreground (Ctrl-C to stop, no pid file)
python3 api_server.py              # direct run — all three read the project-root .env
# open http://localhost:8011/      (PORT from .env, default 8000)
```

Endpoints:

```
POST   /api/download            — create a task, returns {task_id} (non-blocking);
                                 400 with a Chinese `detail` when `m3u8_input` fails
                                 `validate_m3u8_source()`, or when `resolution` fails
                                 `validate_resolution_limit()`
POST   /api/download/batch      — batch create tasks, returns {created, skipped, errors}:
                                 dedupes by URL / sanitized output filename against the
                                 current registry + the batch itself; malformed entries
                                 land in errors (per-line, with optional `line` echoed back),
                                 including `m3u8_input` scheme validation and
                                 `resolution` format validation
GET    /api/tasks               — list all task snapshots
GET    /api/tasks/{id}          — single task detail
GET    /api/tasks/{id}/stream   — NDJSON stream (application/x-ndjson, HTTP Chunked):
                                  replays event history, then pushes live events
POST   /api/tasks/{id}/cancel   — request cancellation (best-effort)
DELETE /api/tasks/{id}          — remove a terminated task from the registry
GET    /api/health              — health check + max_concurrent
GET    /                         — serves web/index.html (StaticFiles)
```

### Configuration

`api_server.py` loads the project-root `.env` at import time (`_load_env_file()`; shell scripts only parse `.env` for display/port probing). Priority: **environment variable > `.env` > code default**. Copy `.env.example` to start:

| Key | Default | Purpose |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `8000` | uvicorn bind address (read by `api_server.py`, not the shell scripts) |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | ThreadPoolExecutor size |
| `BATCH_LIMIT` | `500` | per-batch task limit on `/api/download/batch` |
| `MAX_REGISTRY` | `2000` | registry cap; evicts oldest terminal tasks beyond it |
| `TASK_TTL_SECONDS` | `1800` | how long terminal tasks stay in the registry |
| `ACCESS_TOKEN` | — | fallback upload token when a task doesn't pass `upload.token` |

`_load_env_file()` prefers python-dotenv and falls back to a hand-rolled parser with `setdefault` semantics (existing env vars win), so `python3 api_server.py` works even on interpreters without python-dotenv.

Key design points:
- **`.env` loading**: `_load_env_file()` runs **before** the config constants, so every `os.getenv`-backed setting (`MAX_CONCURRENT_DOWNLOADS`/`BATCH_LIMIT`/`MAX_REGISTRY`/`TASK_TTL_SECONDS`/`HOST`/`PORT`/`ACCESS_TOKEN`) also reads `.env`; the `ACCESS_TOKEN` fallback no longer depends on the shell pre-sourcing `.env`.
- **Concurrency**: global `ThreadPoolExecutor(max_workers=MAX_CONCURRENT)`; excess tasks wait as `pending`. Tunable via env `MAX_CONCURRENT_DOWNLOADS` (default 3).
- **Task model**: `Task` holds status (`pending/running/done/error/cancelled`), progress, event history, subscriber queues, and a `cancel_event`. `TaskManager` owns the registry + executor.
- **Output isolation**: each task writes to `downloads/{task_id}/`; `_safe_name()` sanitizes the basename to prevent path traversal and filename collisions between concurrent tasks.
- **Progress**: `ProgressHook` (subclass of `BaseHook`) forwards `on_playlist_parsed / on_segment / on_segment_data / on_all_downloaded` into the task's event queue; `set_status()` broadcasts terminal states.
- **Streaming/subscribe**: `Task.subscribe()` snapshots history and registers a queue under a lock (no lost/duplicate events); terminal state pushes a `None` sentinel to close streams. Reconnecting or opening a new tab replays history.
- **Cancellation**: the downloader has no built-in cancel point. `CancelHook` checks `cancel_event` in segment callbacks and raises `_Cancelled(BaseException)` — deriving from `BaseException` (not `Exception`) bypasses the downloader's `except Exception` retry loop. Queued (not-yet-started) tasks are cancelled immediately via `future.cancel()`; running tasks are best-effort (in-flight segments may finish first).
- **Upload preflight**: when `--upload` / `upload` is set, `m3u8_downloader.upload_preflight()` (the single shared entry point, called by both CLI `main()` and `api_server._run_preflight`) validates the token via `pan_upload.preflight_check()` **before** any segment is downloaded, so an expired token fails fast. `preflight_check` retries only transient failures (`httpx.RequestError`, 5xx/429, non-JSON body) with 1s/2s/4s backoff (`PREFLIGHT_RETRIES=3`); `PreflightAuthError` (errno != 0 / 401 / 403) propagates immediately with no retry. It deliberately ignores the task's proxy/headers (plain one-shot `httpx.get`). The API emits `preflight` / `preflight_retry` / `preflight_ok` events which the frontend renders.
- **`__main__`**: passes the `app` object (not the `"api_server:app"` string) to `uvicorn.run` — the string form makes uvicorn re-import the module as `api_server`, executing the module-level `manager = TaskManager(...)` twice (two thread pools + two cleanup threads, the `__main__` copy a pure leak).
- **Note**: the task registry is in-memory; a server restart loses task records (files under `downloads/` remain).


## Service scripts (`scripts/*.sh`)

Shell scripts to run the FastAPI service (structure aligned with `movie_api`; Linux/macOS; Windows is not supported):

```bash
./scripts/start.sh                  # background: auto .venv + pip sync + nohup (writes run/server.pid, run/server.log)
./scripts/start.sh foreground       # foreground: exec .venv/bin/python api_server.py (Ctrl-C to stop; no pid file; -f/--foreground 同义)
./scripts/stop.sh                   # stop: SIGTERM children+parent → poll 5s → SIGKILL; always exit 0 (no arguments)
./scripts/restart.sh                # stop + start (forwards env / args to start.sh; `restart.sh foreground` works)
./scripts/status.sh                 # show pid, elapsed time, listener on PORT
tail -f run/server.log              # follow logs
```

Environment variables (optional):
- `HOST` / `PORT` — read by `api_server.py` itself (priority: env > `.env` > default `0.0.0.0:8000`); the scripts only reuse them for display and port probing
- `MAX_CONCURRENT` — forwarded to `MAX_CONCURRENT_DOWNLOADS` **only when explicitly set** (an unconditional default would override `.env`'s value)
- `PYTHON` — bootstrap interpreter for creating `.venv` (default `python3`)
- `SKIP_PIP` — set `1` to skip the per-start dependency sync

venv:
- `start.sh` creates `.venv` if missing (`$PYTHON -m venv`), then runs `pip install -r requirements.txt` on every start (idempotent); a failed sync is only a warning (network blips shouldn't block restarts), followed by a hard `import fastapi, uvicorn` check
- launch command: `.venv/bin/python api_server.py` — the app decides HOST/PORT; the uvicorn CLI is not used

Layout:
- `run/server.pid` — background-mode pid; `stop.sh` kills children first (`pkill -P`) then the parent, so the port is released even when a child holds it
- `run/server.log` — combined stdout + stderr, appended
- `run/` is git-ignored
- `stop.sh` only manages background instances: foreground runs don't write a pid file — stop them with Ctrl-C

Exit codes (suitable for `systemctl` / CI):
- `start.sh` — `0` started; `1` already running / process exited within 1s (log tail printed); `2` bad arguments
- `stop.sh` — always `0` (stopped or no-op, idempotent). Graceful window is 5s (0.1s × 50 polling) before SIGKILL — with active downloads / open NDJSON streams it will usually SIGKILL; the task registry is in-memory, so nothing durable is lost
- `status.sh` — `0` running; `1` stale pid; `3` not running

Port check: `start.sh` probes `$PORT` (via `scripts/_common.sh` `port_in_use`: ss → lsof → netstat, whichever exists) and only **warns** if busy — the shell-side PORT is a coarse parse of `.env` and doesn't own the veto; the real verdict is the post-launch `kill -0` liveness check + log tail.

Launch: `start.sh` runs `.venv/bin/python api_server.py` (the app reads HOST/PORT from env/`.env` itself). The systemd unit still invokes `python3 -m uvicorn api_server:app --host ... --port ...` with the system python — unchanged here, it relies on its own `EnvironmentFile` + shell expansion.

### systemd deployment (production / server)

```bash
sudo ./scripts/install-systemd.sh                              # install with defaults
sudo ./scripts/install-systemd.sh --user=m3u8-downloader --dir=/opt/m3u8  # custom paths
sudo ./scripts/install-systemd.sh --no-start                   # install but don't start
sudo systemctl {status|restart|stop|start} m3u8-downloader
sudo journalctl -u m3u8-downloader -f
```

The installer (`scripts/install-systemd.sh`):
- Verifies root + `systemctl` + `api_server.py` + `fastapi/uvicorn/pydantic`
- Creates system user/group (default `m3u8-downloader`) if missing; chowns `<INSTALL_DIR>/{run,downloads}` to it
- Renders `scripts/m3u8-downloader.service` (substituted paths) → `/etc/systemd/system/m3u8-downloader.service`
- Renders `scripts/m3u8-downloader.logrotate` → `/etc/logrotate.d/m3u8-downloader` (daily, 7 generations, gzip, `copytruncate`)
- `daemon-reload` + `enable`, then `start` (or `restart` if already running)

The unit uses `Type=simple`, `LogsDirectory=m3u8-downloader` (`/var/log/m3u8-downloader/server.log`), `EnvironmentFile=-<INSTALL_DIR>/.env` (optional; leading `-` means missing file is OK), and a `/bin/sh -c '...'` `ExecStart` so `$HOST`/`$PORT` env vars are honored at exec time (systemd itself does not expand variables in `ExecStart`). Hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, explicit `ReadWritePaths`.

The shell scripts and the systemd unit don't share runtime files — don't run both at once (they'd fight for the same port):
- `start.sh` → `run/server.pid` + `run/server.log` (project-local, dev)
- systemd → `/run/m3u8-downloader.pid` + `/var/log/m3u8-downloader/server.log` (system-managed, prod)

### Log rotation

`scripts/m3u8-downloader.logrotate` covers both `<INSTALL_DIR>/run/server.log` (dev) and `/var/log/m3u8-downloader/server.log` (prod) in one block: `daily`, `rotate 7`, `compress`, `delaycompress`, `copytruncate`. The `copytruncate` directive keeps the running process writing to the same inode — no SIGHUP needed (uvicorn doesn't handle SIGHUP). Installed by `install-systemd.sh` to `/etc/logrotate.d/m3u8-downloader`. Verify with `logrotate -d /etc/logrotate.d/m3u8-downloader`.


## Web UI (`web/index.html`)

Single-file frontend served by `api_server.py`. Multi-task download manager:
- Form covers all `m3u8_downloader` parameters (input, output, base_url, workers, retries, timeout, proxy, headers, key_param, segments, resolution, no_merge/keep_temp/no_disk, and the Baidu upload options).
- "添加下载任务" posts to `/api/download`; each task renders an independent card with its own progress bar, status badge, stats, per-task log, cancel/remove buttons.
- Each card subscribes to `/api/tasks/{id}/stream` via `fetch` + `ReadableStream` reader, parsing NDJSON line-by-line.
- On load, `GET /api/tasks` restores existing tasks and reconnects streams for non-terminal ones.
- **Form memory**: on successful submit, `saveForm()` persists the form to `localStorage["m3u8_last_form"]`; `restoreForm()` refills it on next load (includes `upload_token`).
- **Source validation (`web/js/utils/validate.js`)**: `validateM3u8Source()` mirrors `api_server.validate_m3u8_source()` — a source with a scheme must be `http`/`https` with a non-empty host (so `hhttps://` is rejected), while a scheme-less string is treated as a local path and passes; `http:/x` (missing `//`) is also flagged. The single-task form validates on submit, and batch import validates every line in `updateImportPreview()`, which disables "导入" and shows the offending line's raw text; `importTasks()` hard-returns before `createBatchTasks()` whenever any line fails, so a bad batch never hits the API. Dedupe `skipped` entries are **not** treated as errors and never block a batch.
- **Resolution cap validation (`validateResolutionLimit()` in the same file)**: mirrors `m3u8_downloader.parse_resolution_limit()` (empty/absent = unrestricted, `480p` = height only, `854x480` = both dimensions, anything else rejected) and returns the downloader's message, so the user sees the same text as the API. Checked on submit by the single-task form, and once against the shared form config before batch import (`resolution` is a form-level option, so it is not part of the per-line `updateImportPreview()` sweep).

## Legacy Wrapper scripts

All wrappers pass `"$@"` through to `m3u8_downloader.py`.

## Key implementation details

- **M3U8 parsing**: `_parse_playlist()` handles `#EXT-X-KEY` with a regex, tracks stateful current_key across segments. Supports METHOD=AES-128, METHOD=NONE. A playlist whose only content is `#EXT-X-STREAM-INF` + URI pairs is a *master playlist* (`is_master`), not an empty one.
- **Master playlist / resolution cap**: `run()` resolves a master playlist once (`_resolve_master_playlist()`) — it picks a variant, fetches that variant's m3u8, re-points `config.m3u8_source` at it, and continues the normal pipeline. Without `--resolution` it picks the highest by `(pixels, bandwidth)` (`_select_best_variant()`); with `--resolution` (`_select_variant_by_limit()`) it first filters to variants with a known `RESOLUTION` inside the limit — `height <= max_height` **and** (`max_width is None` or `width <= max_width`) — then takes the highest of those. **No variant inside the limit is an error, not a silent downgrade** (`M3U8DownloadError` listing the available resolutions); variants that carry only `BANDWIDTH` make the cap unenforceable, so it warns and falls back to bandwidth. The cap is parsed once in `__init__`, so a malformed value fails before any network I/O. A single-quality playlist has no variant to filter — the cap is logged as ignored.
- **IV calculation**: explicit `IV=0x...` from KEY tag, or `struct.pack(">Q", media_sequence + i).rjust(16, b"\x00")`.
- **Decryption**: `_decrypt()` static method — AES-128-CBC + PKCS7 unpadding. Done in `_download_single()` right after download, so segments on disk are already plaintext.
- **Key caching**: `_load_keys()` caches key bytes by URI — same key URI only downloaded once.
- **Base URL resolution**: prioritized: `--base_url` arg → auto-derived from m3u8 URL → error.
- **Progress**: uses `tqdm` if importable, falls back to text progress every 100 segments.
- **Logging**: all CLI output goes through `log()` (defined in `m3u8_downloader.py`, re-exported by `api_server.py`; `pan_upload.py` keeps its own copy to stay importable standalone) which prefixes `[yyyy-MM-dd HH:mm:ss]` and flushes — no bare `print()` for messages. `ProgressBar.write` / `pan_upload.log_tqdm` use `tqdm.write` with the same prefix so progress bars aren't torn. `api_server` also passes a timestamped `log_config` to `uvicorn.run` (uvicorn's defaults have no `asctime`). Format constant: `LOG_TIME_FORMAT`.
- **Retry**: each segment retries with 1s/2s/3s backoff before reporting failure.
