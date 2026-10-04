#!/usr/bin/env python3
"""
Baidu Yunpan upload tool (standalone CLI).

    # Single file (with retry)
    python -m app.upload video.ts --retries 5

    # Directory with resume, 5 parallel workers
    python -m app.upload ./my_videos /apps/{app_name}movies/my_videos -w 5

    # Skip files larger than 100MB
    python -m app.upload ./my_videos /apps/{app_name}movies/my_videos --max-size 100MB

    # Keep local file after upload
    python -m app.upload video.ts --no-delete-after-upload
"""

import argparse
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx
from dotenv import load_dotenv
from tqdm import tqdm


# ── Constants ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_REMOTE_PREFIX = "/apps/asitanokibou/movies"
UPLOAD_URL = "https://d.pcs.baidu.com/rest/2.0/pcs/file"
PROGRESS_FILENAME = ".yunpan_upload_progress.json"

DEFAULT_RETRIES = 5
DEFAULT_WORKERS = 5


# ── Helpers ────────────────────────────────────────────────────────────────

# 时间戳格式, 与 m3u8_downloader.LOG_TIME_FORMAT 一致 (Java 的 yyyy-MM-dd HH:mm:ss)
LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def log(msg: str = "", *, file=None) -> None:
    """带时间戳的日志输出, 形如 ``[2026-02-05 14:33:01] 内容``.

    作为库被 m3u8_downloader 调用时, 输出会和下载日志混在同一份 stdout,
    加时间戳便于按时间排查 (实时 flush).
    """
    stream = file or sys.stdout
    stream.write(f"[{time.strftime(LOG_TIME_FORMAT)}] {msg}\n")
    stream.flush()


def log_tqdm(msg: str) -> None:
    """有 tqdm 进度条时用 tqdm.write 输出, 避免冲散进度条 (同样带时间戳)."""
    tqdm.write(f"[{time.strftime(LOG_TIME_FORMAT)}] {msg}")


def load_access_token(env_file: str | None = None) -> str:
    """Load ACCESS_TOKEN from a .env file.  Exit if not found."""
    env_path = Path(env_file or DEFAULT_ENV_FILE)

    if env_path.exists():
        load_dotenv(dotenv_path=str(env_path))

    token = os.getenv("ACCESS_TOKEN")
    if not token:
        log(
            f"ERROR: ACCESS_TOKEN not set. "
            f"Place it in {env_path} or use --token.",
            file=sys.stderr,
        )
        sys.exit(1)
    return token


def _human_size(num_bytes: int) -> str:
    """Pretty-print file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


# ── Progress tracking (directory resume) ───────────────────────────────────

def _progress_path(local_dir: Path) -> Path:
    return local_dir / PROGRESS_FILENAME


def _load_progress(local_dir: Path, remote_dir: str) -> set[str]:
    """Return the set of already-uploaded filenames.

    If the progress file doesn't exist, is unreadable, or was written for
    a *different* remote directory, return an empty set and remove the stale
    file.
    """
    pp = _progress_path(local_dir)
    if not pp.exists():
        return set()
    try:
        data = json.loads(pp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if data.get("remote_prefix") != remote_dir:
        log_tqdm("[resume] Remote path changed — starting fresh.")
        pp.unlink(missing_ok=True)
        return set()
    return set(data.get("uploaded", []))


def _save_progress(local_dir: Path, remote_dir: str, uploaded: set[str]) -> None:
    """Persist the set of uploaded filenames to the progress file."""
    _progress_path(local_dir).write_text(
        json.dumps(
            {
                "remote_prefix": remote_dir,
                "uploaded": sorted(uploaded),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _clear_progress(local_dir: Path) -> None:
    """Remove the progress file (called when all uploads succeed)."""
    _progress_path(local_dir).unlink(missing_ok=True)


# ── Upload logic ───────────────────────────────────────────────────────────

# 模块级 httpx.Client 单例: 复用底层连接池, 避免每次调用都创建/销毁 client
# httpx.Client 是线程安全的, 可在 ThreadPoolExecutor 的多个 worker 间共享
_http_client: Optional[httpx.Client] = None
_http_client_lock = threading.Lock()


def _get_http_client() -> httpx.Client:
    """懒加载并返回全局 httpx.Client 单例."""
    global _http_client
    if _http_client is None:
        with _http_client_lock:
            if _http_client is None:
                _http_client = httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0))
    return _http_client


def _do_upload_raw(local_path: Path, remote_path: str, token: str, ondup: str) -> dict:
    """Upload a single file (no retry, no logging).  Raise on failure."""
    with open(local_path, "rb") as fh:
        response = _get_http_client().post(
            UPLOAD_URL,
            params={
                "method": "upload",
                "access_token": token,
                "path": remote_path,
                "ondup": ondup,
                "rtype": 3,
            },
            files={"file": (local_path.name, fh, "application/octet-stream")},
        )
    #response.raise_for_status()
    if response.status_code != 200:
        log(f'response code {response.status_code} body: {response.text}')
        raise RuntimeError(f"HTTP error {response.status_code}: {response.text}")
    result: dict = response.json()

    errno = result.get("errno")
    if errno is not None and errno != 0:
        errmsg = result.get("errmsg", "unknown error")
        raise RuntimeError(f"API errno={errno}: {errmsg}")

    return result


def _upload_with_retry(
    local_path: Path,
    remote_path: str,
    token: str,
    ondup: str,
    max_retries: int,
) -> dict:
    """Upload a single file with exponential-backoff retry.

    Retry delays: 1 s, 2 s, 4 s, 8 s, ...
    """
    last_error = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = 2 ** (attempt - 1)
            log_tqdm(
                f"  [retry {attempt}/{max_retries}] {local_path.name} "
                f"(wait {delay}s)"
            )
            time.sleep(delay)
        try:
            return _do_upload_raw(local_path, remote_path, token, ondup)
        except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError, OSError) as e:
            last_error = e

    raise RuntimeError(
        f"Failed after {max_retries} retries: {last_error}"
    ) from last_error


def _error_detail(exc: Exception) -> str:
    """Extract a short error message for display."""
    msg = str(exc).strip()
    return msg[:120]


# ── Single-file upload ─────────────────────────────────────────────────────

def upload_file(
    local_path: str,
    remote_path: str,
    token: str,
    ondup: str,
    max_retries: int,
    max_size: int | None = None,
    delete_after_upload: bool = True,
) -> dict:
    """Upload a single file to Baidu Yunpan (with retry)."""
    local = Path(local_path)

    if not local.exists():
        log(f"ERROR: file not found: {local_path}", file=sys.stderr)
        sys.exit(1)
    if not local.is_file():
        log(f"ERROR: not a regular file: {local_path}", file=sys.stderr)
        sys.exit(1)

    size = local.stat().st_size

    # Check file size limit
    if max_size is not None and size > max_size:
        log(f"SKIPPED: {local}  ({_human_size(size)}) - exceeds size limit of {_human_size(max_size)}")
        return {
            'skipped': True,
            'path': str(local),
            'size': size,
            'reason': f'Exceeds size limit of {max_size} bytes'
        }

    log(f"Uploading: {local}  ({_human_size(size)})")
    log(f"Target:    {remote_path}")
    if max_retries > 0:
        log(f"Retries:   {max_retries}")

    result = _upload_with_retry(local, remote_path, token, ondup, max_retries)

    log("Upload successful!")
    log(f"  fs_id: {result.get('fs_id', 'N/A')}")
    log(f"  path:  {result.get('path', 'N/A')}")
    log(f"  size:  {result.get('size', 'N/A')}")
    if result.get("md5"):
        log(f"  md5:   {result['md5']}")

    # Delete local file after successful upload if requested
    if delete_after_upload:
        try:
            local.unlink()
            log(f"  Local file deleted: {local}")
        except OSError as e:
            log(f"  WARNING: Failed to delete local file {local}: {e}", file=sys.stderr)

    return result


# ── Bytes upload (no-disk mode) ────────────────────────────────────────────
# 供外部使用 非cli独立运行的时候使用
def _do_upload_bytes(data: bytes, filename: str, remote_path: str, token: str, ondup: str) -> dict:
    """Upload raw bytes as a file (no retry, no logging).  Raise on failure."""
    response = _get_http_client().post(
        UPLOAD_URL,
        params={
            "method": "upload",
            "access_token": token,
            "path": remote_path,
            "ondup": ondup,
            "rtype": 3,
        },
        files={"file": (filename, io.BytesIO(data), "application/octet-stream")},
    )
    #response.raise_for_status()
    if response.status_code != 200:
        log(f'response code {response.status_code} body: {response.text}')
        raise RuntimeError(f"HTTP error {response.status_code}: {response.text}")
    
    result: dict = response.json()

    errno = result.get("errno")
    if errno is not None and errno != 0:
        errmsg = result.get("errmsg", "unknown error")
        raise RuntimeError(f"API errno={errno}: {errmsg}")

    return result


def _upload_bytes_with_retry(
    data: bytes, filename: str, remote_path: str,
    token: str, ondup: str, max_retries: int,
) -> dict:
    """Upload raw bytes with exponential-backoff retry.

    Retry delays: 1 s, 2 s, 4 s, 8 s, ...
    """
    last_error = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = 2 ** (attempt - 1)
            log_tqdm(
                f"  [retry {attempt}/{max_retries}] {filename} "
                f"(wait {delay}s)"
            )
            time.sleep(delay)
        try:
            return _do_upload_bytes(data, filename, remote_path, token, ondup)
        except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError, OSError) as e:
            last_error = e

    raise RuntimeError(
        f"Failed after {max_retries} retries: {last_error}"
    ) from last_error


def upload_bytes(
    data: bytes,
    filename: str,
    remote_path: str,
    token: str,
    ondup: str,
    max_retries: int,
) -> dict:
    """Upload raw bytes to Baidu Yunpan (with retry)."""
    return _upload_bytes_with_retry(
        data, filename, remote_path, token, ondup, max_retries
    )


# ── Preflight check ────────────────────────────────────────────────────────

# Baidu quota API: 用户级接口, 仅用于验 access_token + 顺手拿到配额信息.
# errno 6 表示 access_token 无效 (身份认证失败), 比较明确.
QUOTA_URL = "https://pan.baidu.com/api/quota"

# 预检重试默认值: 只对网络/服务端临时故障重试, 退避 1s/2s/4s...
PREFLIGHT_RETRIES = 3
PREFLIGHT_BACKOFF_BASE = 1.0
# 临时性状态码 (网关错误 / 限流) → 可重试; 其余 4xx 视为不可重试
PREFLIGHT_RETRYABLE_STATUS = {408, 425, 429} | set(range(500, 600))


class PreflightError(RuntimeError):
    """云盘预检失败基类 (默认不重试)."""

    retryable = False


class PreflightAuthError(PreflightError):
    """access_token 无效 / 过期 / 无权限 —— 重试无意义, 应立即失败."""


class PreflightTransientError(PreflightError):
    """网络抖动 / 服务端临时故障 —— 值得重试."""

    retryable = True


def _preflight_once(token: str, timeout: float) -> dict:
    """单次预检请求. 失败时抛出已分类的 PreflightError 子类."""
    try:
        resp = httpx.get(
            QUOTA_URL,
            params={
                "checkexpire": 1,
                "checkfree": 1,
                "access_token": token,
            },
            timeout=httpx.Timeout(timeout, connect=timeout),
        )
    except httpx.RequestError as e:
        # ConnectError / ReadTimeout / RemoteProtocolError / ProxyError ...
        raise PreflightTransientError(f"网络错误: {e}") from e

    if resp.status_code in (401, 403):
        raise PreflightAuthError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code != 200:
        detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if resp.status_code in PREFLIGHT_RETRYABLE_STATUS:
            raise PreflightTransientError(detail)
        raise PreflightError(detail)

    try:
        result: dict = resp.json()
    except ValueError as e:
        # 代理 / 防火墙 / 验证页返回 HTML 时会到这里
        raise PreflightTransientError(f"响应不是合法 JSON: {resp.text[:200]}") from e

    errno = result.get("errno")
    if errno in (0, None):
        return {
            "errno": 0,
            "total": result.get("total"), #int: 总空间大小，单位B
            "used": result.get("used"), #int: 已使用大小，单位B
            "free": result.get("free"), # int: 免费容量，单位B
            "expire": result.get("expire"),#bool: 7天内是否有容量到期
        }
    # errno=6 表示认证失败  (reference: https://pan.baidu.com/union/doc/%E5%B9%B3%E5%8F%B0%E7%AE%80%E4%BB%8B/%E9%94%99%E8%AF%AF%E7%A0%81/)
    errmsg = result.get("errmsg", "unknown")
    raise PreflightAuthError(f"云盘预检失败 (errno={errno} {errmsg})")


def preflight_check(
    token: str,
    timeout: float = 10.0,
    max_retries: int = PREFLIGHT_RETRIES,
    on_retry: Optional[Callable[[int, int, float, Exception], None]] = None,
) -> dict:
    """Pre-flight check: validate Baidu access_token via the quota API.

    Calls ``GET /api/quota`` (a lightweight, non-destructive user-info call)
    to confirm the access_token is still valid. errno 6 indicates
    authentication failure; any other non-zero errno is also surfaced with
    its ``errmsg``.

    Designed to be called *before* starting a long-running m3u8 download, so
    we fail fast on an expired / invalid token instead of downloading hundreds
    of segments that we can never upload. 后续的上传会自己创建目标目录, 本函数
    不再做任何路径相关的检查 / 写入.

    重试策略 (避免网络抖动被误判成 token 失效):
      - 只重试 PreflightTransientError: httpx.RequestError (连接/读取超时等),
        5xx / 429, 非法 JSON 响应; 退避 1s, 2s, 4s, ...
      - PreflightAuthError (errno != 0 / 401 / 403) 立即抛出, 不做无谓重试,
        调用方据此终止下载
    预检使用独立的一次性请求, 不继承调用方的 proxy / header 配置.

    Args:
        token: 百度云盘 access_token.
        timeout: 单次请求的超时秒数 (connect / read 相同).
        max_retries: 临时故障的重试次数 (默认 3, 即最多请求 4 次).
        on_retry: 可选回调 ``(attempt, total, delay, error)``, 在每次重试前调用,
            供调用方上报进度 (CLI 打印 / API 事件流).

    Returns:
        A small dict with the API response on success. Raises on failure.
    """
    if not token:
        raise ValueError("token 不能为空")

    last_error: Optional[PreflightError] = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = PREFLIGHT_BACKOFF_BASE * (2 ** (attempt - 1))
            if on_retry is not None:
                try:
                    on_retry(attempt, max_retries, delay, last_error)
                except Exception:
                    pass          # 上报失败不影响预检本身
            time.sleep(delay)
        try:
            return _preflight_once(token, timeout)
        except PreflightTransientError as e:
            last_error = e

    assert last_error is not None
    raise last_error


# ── Directory upload ───────────────────────────────────────────────────────

def upload_directory(
    local_dir: str,
    remote_dir: str,
    token: str,
    ondup: str,
    resume: bool,
    workers: int,
    max_retries: int,
    max_size: int | None = None,
) -> None:
    """Upload every ordinary file in *local_dir* to *remote_dir*.

    - **resume**: skip already-uploaded files (persisted in *local_dir*).
    - **workers**: number of parallel upload threads (default 3).
    - **max_retries**: per-file retry count with exponential backoff.
    """
    local = Path(local_dir).resolve()

    if not local.exists():
        log(f"ERROR: directory not found: {local_dir}", file=sys.stderr)
        sys.exit(1)
    if not local.is_dir():
        log(f"ERROR: not a directory: {local_dir}", file=sys.stderr)
        sys.exit(1)

    all_files = sorted(
        [p for p in local.iterdir() if p.is_file()],
        key=lambda p: p.name,
    )

    if not all_files:
        log(f"No files found in {local}")
        return

    # Filter files by size limit
    if max_size is not None:
        size_filtered_files = []
        size_skipped_count = 0

        for fpath in all_files:
            if fpath.stat().st_size > max_size:
                size_filtered_files.append(fpath)
                size_skipped_count += 1

        if size_filtered_files:
            log(f"Size limit: {_human_size(max_size)} - skipping {size_skipped_count} file(s) that exceed limit:")
            for fpath in size_filtered_files:
                log(f"  SKIPPED: {fpath.name} ({_human_size(fpath.stat().st_size)})")
            print()

        all_files = [f for f in all_files if f.stat().st_size <= max_size]

    if not all_files:
        log("No files to upload after filtering.")
        return

    remote_dir = remote_dir.rstrip("/") + "/"

    log(f"Local dir:  {local}")
    log(f"Remote dir: {remote_dir}")
    log(f"Files:      {len(all_files)}")
    log(f"Workers:    {workers}")
    if max_retries > 0:
        log(f"Retries:    {max_retries}")
    if max_size is not None:
        log(f"Size limit: {_human_size(max_size)}")
    print()

    # ── Resume state ─────────────────────────────────────────────────
    uploaded: set[str] = set()
    if resume:
        uploaded = _load_progress(local, remote_dir)
        if uploaded:
            log_tqdm(
                f"[resume] {len(uploaded)} file(s) already uploaded — skipping.\n"
            )

    # Build pending list and skip count
    pending = [f for f in all_files if f.name not in uploaded]
    skipped = len(all_files) - len(pending)

    if not pending:
        log("All files already uploaded — nothing to do.")
        if resume:
            _clear_progress(local)
            log("[resume] Progress record cleared.")
        return

    success = 0
    failed = 0
    failed_details: list[tuple[str, str]] = []

    # ── Upload with tqdm ─────────────────────────────────────────────
    with tqdm(
        total=len(all_files),
        desc="Uploading",
        unit="file",
        initial=skipped,
        ncols=100,
    ) as pbar:

        if workers == 1:
            # ── Sequential ────────────────────────────────────────────
            for fpath in pending:
                remote_path = remote_dir + fpath.name
                try:
                    _upload_with_retry(fpath, remote_path, token, ondup, max_retries)
                    success += 1

                    if resume:
                        uploaded.add(fpath.name)
                        _save_progress(local, remote_dir, uploaded)
                except Exception as exc:
                    failed += 1
                    failed_details.append((fpath.name, _error_detail(exc)))
                    log_tqdm(f"  FAIL  {fpath.name}  {_error_detail(exc)}")

                pbar.update(1)
        else:
            # ── Parallel ──────────────────────────────────────────────
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_file = {
                    executor.submit(
                        _upload_with_retry,
                        fpath,
                        remote_dir + fpath.name,
                        token,
                        ondup,
                        max_retries,
                    ): fpath
                    for fpath in pending
                }

                for future in as_completed(future_to_file):
                    fpath = future_to_file[future]
                    try:
                        future.result()
                        success += 1

                        if resume:
                            uploaded.add(fpath.name)
                            _save_progress(local, remote_dir, uploaded)
                    except Exception as exc:
                        failed += 1
                        failed_details.append((fpath.name, _error_detail(exc)))
                        log_tqdm(f"  FAIL  {fpath.name}  {_error_detail(exc)}")

                    pbar.update(1)

    # ── Summary ──────────────────────────────────────────────────────
    print()
    print("─" * 50)
    log(
        f"Done.  {success} uploaded, {skipped} skipped, {failed} failed."
    )

    if failed_details:
        print()
        log("Failed files:")
        for name, reason in failed_details:
            log(f"  {name}  —  {reason}")

    if failed == 0 and resume:
        _clear_progress(local)
        log("[resume] Progress record cleared.")
    elif failed > 0 and resume:
        log(
            f"[resume] Progress saved ({success} ok). "
            f"Re-run to retry the {failed} failed file(s)."
        )


# ── CLI ────────────────────────────────────────────────────────────────────

def _positive_int(value: str) -> int:
    ival = int(value)
    if ival < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {ival}")
    return ival


def _parse_size(size_str: str) -> int:
    """Parse size string like '100MB', '1GB' to bytes.

    Examples:
        '100' -> 100 bytes
        '100KB' -> 102400 bytes
        '100MB' -> 104857600 bytes
        '100GB' -> 107374182400 bytes
    """
    size_str = size_str.strip().upper()

    if size_str.isdigit():
        return int(size_str)

    units = {
        'KB': 1024,
        'MB': 1024 ** 2,
        'GB': 1024 ** 3,
        'TB': 1024 ** 4,
        'B': 1,
    }

    for unit, multiplier in units.items():
        if size_str.endswith(unit):
            number = size_str[:-len(unit)].strip()
            if not number.isdigit():
                raise argparse.ArgumentTypeError(f"Invalid size format: {size_str}")
            return int(number) * multiplier

    raise argparse.ArgumentTypeError(f"Invalid size format: {size_str}. Use format like '100MB', '1GB', etc.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload a file or directory to Baidu Yunpan (standalone CLI).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single file
  python -m app.upload video.ts
  python -m app.upload video.ts -r /apps/asitanokibou/movies/my-video.ts --retries 5

  # Directory with resume + 5 parallel workers
  python -m app.upload ./my_videos -r /apps/asitanokibou/movies/my_videos -w 5

  # Directory — no resume, overwrite existing files
  python -m app.upload ./my_videos -r /apps/asitanokibou/movies/my_videos --no-resume -o overwrite

  # Skip files larger than 100MB
  python -m app.upload ./my_videos -r /apps/asitanokibou/movies/my_videos --max-size 100MB

  # Skip files larger than 1GB
  python -m app.upload ./large_files -r /apps/large --max-size 1GB

  # Keep local file after upload (don't delete)
  python -m app.upload video.ts --no-delete-after-upload

  # Upload and delete (default behavior)
  python -m app.upload video.ts
        """,
    )
    parser.add_argument(
        "file",
        help="Local file or directory path to upload.",
    )
    parser.add_argument(
        "--remote-path",
        "-r",
        default=None,
        help=(
            f"Target path on Baidu Pan "
            f"(default: {DEFAULT_REMOTE_PREFIX}/<name>)."
        ),
    )
    parser.add_argument(
        "--token",
        "-t",
        default=None,
        help="Baidu Pan access token (overrides ACCESS_TOKEN from .env).",
    )
    parser.add_argument(
        "--ondup",
        "-o",
        choices=["fail", "overwrite", "newcopy"],
        default="overwrite",
        help="Action when target file already exists (default: fail).",
    )
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help=f"Path to .env file (default: {DEFAULT_ENV_FILE}).",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        default=False,
        help="Disable resume for directory uploads (do not track progress).",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of parallel upload threads for directory mode "
             f"(default: {DEFAULT_WORKERS}).",
    )
    parser.add_argument(
        "--retries",
        type=_positive_int,
        default=DEFAULT_RETRIES,
        help=f"Max retries per file with exponential backoff "
             f"(default: {DEFAULT_RETRIES}, 0 = no retry).",
    )
    parser.add_argument(
        "--max-size",
        type=_parse_size,
        default=None,
        help="Skip files larger than this size. "
             "Examples: '100MB', '1GB', '500KB'. If not set, no size limit.",
    )
    delete_group = parser.add_mutually_exclusive_group()
    delete_group.add_argument(
        "--delete-after-upload",
        action="store_true",
        default=True,
        help="Delete local file after successful upload (default: True).",
    )
    delete_group.add_argument(
        "--no-delete-after-upload",
        action="store_true",
        help="Do not delete local file after successful upload.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    token = args.token or load_access_token(args.env_file)
    path_arg = Path(args.file)

    # Determine delete_after_upload flag
    delete_after_upload = args.delete_after_upload and not args.no_delete_after_upload

    # Determine remote path
    if args.remote_path:
        remote_path = args.remote_path
    else:
        remote_path = f"{DEFAULT_REMOTE_PREFIX}/{path_arg.name}"
    
    if path_arg.is_dir():
        upload_directory(
            local_dir=str(path_arg),
            remote_dir=remote_path,
            token=token,
            ondup=args.ondup,
            resume=not args.no_resume,
            workers=args.workers,
            max_retries=args.retries,
            max_size=args.max_size,
        )
    else:
        upload_file(
            local_path=str(path_arg),
            remote_path=remote_path,
            token=token,
            ondup=args.ondup,
            max_retries=args.retries,
            max_size=args.max_size,
            delete_after_upload=delete_after_upload,
        )


if __name__ == "__main__":
    main()
