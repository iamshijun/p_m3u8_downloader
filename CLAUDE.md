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
