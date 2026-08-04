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
```

## Architecture

```
dataclasses:
  KeyInfo       — AES-128 key (method, key_uri, key_data, iv)
  M3U8Segment   — single segment (uri, duration, key)
  M3U8Playlist  — parsed playlist (segments, version, media_sequence, is_encrypted)

classes:
  HTTPClient    — wraps urllib.request with proxy/headers/timeout. Methods: get(), get_text()
  M3U8Downloader — orchestrator. Constructor takes all config, run() executes full pipeline

pipeline: fetch → parse → resolve_base_url → load_keys → download_segments → [decrypt] → merge
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
pip install fastapi uvicorn        # extra deps (see requirements.txt)
python3 api_server.py              # or: uvicorn api_server:app --host 0.0.0.0 --port 8000
# open http://localhost:8000/
```

Endpoints:

```
POST   /api/download            — create a task, returns {task_id} (non-blocking)
POST   /api/download/batch      — batch create tasks, returns {created, skipped, errors}:
                                 dedupes by URL / sanitized output filename against the
                                 current registry + the batch itself; malformed entries
                                 land in errors (per-line, with optional `line` echoed back)
GET    /api/tasks               — list all task snapshots
GET    /api/tasks/{id}          — single task detail
GET    /api/tasks/{id}/stream   — NDJSON stream (application/x-ndjson, HTTP Chunked):
                                  replays event history, then pushes live events
POST   /api/tasks/{id}/cancel   — request cancellation (best-effort)
DELETE /api/tasks/{id}          — remove a terminated task from the registry
GET    /api/health              — health check + max_concurrent
GET    /                         — serves web/index.html (StaticFiles)
```

Key design points:
- **Concurrency**: global `ThreadPoolExecutor(max_workers=MAX_CONCURRENT)`; excess tasks wait as `pending`. Tunable via env `MAX_CONCURRENT_DOWNLOADS` (default 3).
- **Task model**: `Task` holds status (`pending/running/done/error/cancelled`), progress, event history, subscriber queues, and a `cancel_event`. `TaskManager` owns the registry + executor.
- **Output isolation**: each task writes to `downloads/{task_id}/`; `_safe_name()` sanitizes the basename to prevent path traversal and filename collisions between concurrent tasks.
- **Progress**: `ProgressHook` (subclass of `BaseHook`) forwards `on_playlist_parsed / on_segment / on_segment_data / on_all_downloaded` into the task's event queue; `set_status()` broadcasts terminal states.
- **Streaming/subscribe**: `Task.subscribe()` snapshots history and registers a queue under a lock (no lost/duplicate events); terminal state pushes a `None` sentinel to close streams. Reconnecting or opening a new tab replays history.
- **Cancellation**: the downloader has no built-in cancel point. `CancelHook` checks `cancel_event` in segment callbacks and raises `_Cancelled(BaseException)` — deriving from `BaseException` (not `Exception`) bypasses the downloader's `except Exception` retry loop. Queued (not-yet-started) tasks are cancelled immediately via `future.cancel()`; running tasks are best-effort (in-flight segments may finish first).
- **Note**: the task registry is in-memory; a server restart loses task records (files under `downloads/` remain).


## Service scripts (`scripts/*.sh`)

Shell scripts to run the FastAPI service as a background process (Linux only; Windows is not supported):

```bash
./scripts/start.sh                  # start in background (writes run/server.pid, run/server.log)
./scripts/stop.sh                   # graceful stop: SIGTERM → wait 15s → SIGKILL
./scripts/stop.sh --force           # skip graceful, SIGKILL immediately
./scripts/stop.sh --timeout=30      # custom graceful timeout in seconds
./scripts/restart.sh                # stop + start (forwards env / args to start.sh)
./scripts/status.sh                 # show pid, elapsed time, listener on PORT
tail -f run/server.log              # follow logs
```

Environment variables (optional, consumed by `start.sh` / `restart.sh`):
- `HOST` — listen address (default `0.0.0.0`)
- `PORT` — listen port (default `8000`)
- `MAX_CONCURRENT` — concurrent downloads (default `3`, forwarded as `MAX_CONCURRENT_DOWNLOADS`)
- `PYTHON` — interpreter (default `python3`)

Layout:
- `run/server.pid` — main process pid; `stop.sh` also walks `pgrep -P` to kill the uvicorn worker, so the port is released even when the main process has already exited
- `run/server.log` — combined stdout + stderr, appended
- `run/` is git-ignored

Exit codes (suitable for `systemctl` / CI):
- `start.sh` — `0` started; `1` already running / port busy / missing deps / process exited within 1s
- `stop.sh` — `0` stopped or no-op; `1` still running; `2` bad arguments
- `status.sh` — `0` running; `1` stale pid; `3` not running

Port check: `start.sh` runs `ss` / `lsof` / `netstat` (whichever exists) against `$PORT` before launching. If busy it prints the occupying process and exits 1 — no pid file is written, no process spawned.

Launch: both `start.sh` and the systemd unit invoke `python3 -m uvicorn api_server:app --host ... --port ...` instead of `python3 api_server.py`. The latter hard-codes `0.0.0.0:8000` and ignores `HOST`/`PORT`; uvicorn-CLI honors them.

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
- Form covers all `m3u8_downloader` parameters (input, output, base_url, workers, retries, timeout, proxy, headers, key_param, no_merge/keep_temp/no_disk, and the Baidu upload options).
- "添加下载任务" posts to `/api/download`; each task renders an independent card with its own progress bar, status badge, stats, per-task log, cancel/remove buttons.
- Each card subscribes to `/api/tasks/{id}/stream` via `fetch` + `ReadableStream` reader, parsing NDJSON line-by-line.
- On load, `GET /api/tasks` restores existing tasks and reconnects streams for non-terminal ones.
- **Form memory**: on successful submit, `saveForm()` persists the form to `localStorage["m3u8_last_form"]`; `restoreForm()` refills it on next load (includes `upload_token`).

## Legacy Wrapper scripts

All wrappers pass `"$@"` through to `m3u8_downloader.py`.

## Key implementation details

- **M3U8 parsing**: `_parse_playlist()` handles `#EXT-X-KEY` with a regex, tracks stateful current_key across segments. Supports METHOD=AES-128, METHOD=NONE.
- **IV calculation**: explicit `IV=0x...` from KEY tag, or `struct.pack(">Q", media_sequence + i).rjust(16, b"\x00")`.
- **Decryption**: `_decrypt()` static method — AES-128-CBC + PKCS7 unpadding. Done in `_download_single()` right after download, so segments on disk are already plaintext.
- **Key caching**: `_load_keys()` caches key bytes by URI — same key URI only downloaded once.
- **Base URL resolution**: prioritized: `--base_url` arg → auto-derived from m3u8 URL → error.
- **Progress**: uses `tqdm` if importable, falls back to text progress every 100 segments.
- **Retry**: each segment retries with 1s/2s/3s backoff before reporting failure.
