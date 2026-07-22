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

## Legacy Wrapper scripts

- `start_download.sh` — generic wrapper with User-Agent header
- `missav_download.sh` — for missav.ai (proxy + Referer)
- `7mmtv_sp_download.sh` — for 7mmtv.sx (Referer header)

All wrappers pass `"$@"` through to `m3u8_downloader.py`.

## Key implementation details

- **M3U8 parsing**: `_parse_playlist()` handles `#EXT-X-KEY` with a regex, tracks stateful current_key across segments. Supports METHOD=AES-128, METHOD=NONE.
- **IV calculation**: explicit `IV=0x...` from KEY tag, or `struct.pack(">Q", media_sequence + i).rjust(16, b"\x00")`.
- **Decryption**: `_decrypt()` static method — AES-128-CBC + PKCS7 unpadding. Done in `_download_single()` right after download, so segments on disk are already plaintext.
- **Key caching**: `_load_keys()` caches key bytes by URI — same key URI only downloaded once.
- **Base URL resolution**: prioritized: `--base_url` arg → auto-derived from m3u8 URL → error.
- **Progress**: uses `tqdm` if importable, falls back to text progress every 100 segments.
- **Retry**: each segment retries with 1s/2s/3s backoff before reporting failure.
