"""FastAPI 服务: 封装 m3u8_downloader 提供多任务下载 + 流式进度接口.

启动:
    pip install fastapi uvicorn
    python3 api_server.py
    # 或 uvicorn api_server:app --host 0.0.0.0 --port 8000

接口:
    POST   /api/download              → 创建下载任务, 返回 {task_id}
    GET    /api/tasks                 → 列出所有任务 (状态快照)
    GET    /api/tasks/{id}            → 单个任务详情
    GET    /api/tasks/{id}/stream     → StreamingResponse(NDJSON) 订阅进度 (先回放历史再实时)
    POST   /api/tasks/{id}/cancel     → 请求取消 (尽力而为)
    DELETE /api/tasks/{id}            → 从注册表移除已结束的任务
    GET    /api/health                → 健康检查
    GET    /                          → 静态页面 web/index.html

并发:
    全局 ThreadPoolExecutor(max_workers=MAX_CONCURRENT) 控制同时运行的任务数,
    超出的任务在队列中等待 (状态 pending). 环境变量 MAX_CONCURRENT_DOWNLOADS 可调.

取消:
    m3u8_downloader 内部不提供取消点, 本服务通过 CancelHook 在每个分片回调时检查
    取消标志并抛出 (BaseException 派生, 绕过下载器的 except Exception 重试).
    因此取消是"尽力而为": 已在网络请求中的分片可能仍会短暂继续, 之后任务终止.
    尚未开始运行(仍在队列中)的任务可被立即取消.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from m3u8_downloader import (
    BaseHook,
    HTTPClient,
    M3U8Downloader,
    M3U8Playlist,
    M3U8Segment,
    SegmentMergeHook,
    YunpanUploadHook,
)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
DOWNLOAD_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
TERMINAL_STATES = {"done", "error", "cancelled"}


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class DownloadRequest(BaseModel):
    """下载请求参数, 字段与 m3u8_downloader 命令行参数一一对应."""

    m3u8_input: str = Field(..., description="m3u8 文件路径或 URL")
    output: str = Field("output.ts", description="输出文件名 (仅取 basename)")
    base_url: Optional[str] = Field(None, description="视频片段的根 URL")
    workers: int = Field(10, ge=1, le=64, description="并发下载线程数")
    retries: int = Field(3, ge=0, description="失败重试次数")
    timeout: int = Field(5, ge=1, description="HTTP 请求超时秒数")
    keep_temp: bool = Field(False, description="保留临时目录不删除")
    proxy: Optional[str] = Field(None, description="代理地址 http://host:port")
    headers: Optional[list[str]] = Field(None, description="自定义请求头, 格式 Key:Value")
    key_param: Optional[str] = Field(None, description="EXT-X-KEY 请求时附加的参数")
    key_format: Optional[str] = Field(None, description="EXT-X-KEY 返回的格式")
    no_merge: bool = Field(False, description="不合并, 保留分片目录 + m3u8")
    no_disk: bool = Field(False, description="不落磁盘, 直接推给 upload hook")

    # 上传相关
    upload: Optional[str] = Field(None, description="上传到百度云盘的远程目录")
    upload_token: Optional[str] = Field(None, description="百度 ACCESS_TOKEN")
    upload_ondup: str = Field("overwrite", description="冲突策略: fail/overwrite/newcopy")
    upload_retries: int = Field(5, description="上传重试次数")
    upload_delete: bool = Field(True, description="上传成功后是否删除本地分片")


# ---------------------------------------------------------------------------
# 取消机制 (外部 Hook, 不修改下载器)
# ---------------------------------------------------------------------------


class _Cancelled(BaseException):
    """用于中断下载的信号异常.

    继承 BaseException 而非 Exception, 以绕过 m3u8_downloader
    在分片下载中的 `except Exception` 重试逻辑, 使其快速传播终止.
    """


class CancelHook(BaseHook):
    """在下载生命周期回调点检查取消标志."""

    def __init__(self, cancel_event: threading.Event) -> None:
        self._event = cancel_event

    def _check(self) -> None:
        if self._event.is_set():
            raise _Cancelled()

    def on_playlist_parsed(self, playlist: M3U8Playlist) -> None:
        self._check()

    def on_segment(self, index: int, segment: M3U8Segment, filepath: str) -> None:
        self._check()

    def on_segment_data(self, index, segment, data, filename) -> None:
        self._check()


# ---------------------------------------------------------------------------
# 进度 Hook
# ---------------------------------------------------------------------------


class ProgressHook(BaseHook):
    """将下载生命周期事件转发给 Task."""

    def __init__(self, task: "Task") -> None:
        self.task = task
        self._done = 0
        self._lock = threading.Lock()

    def on_playlist_parsed(self, playlist: M3U8Playlist) -> None:
        self.task.total = len(playlist)
        self.task.encrypted = playlist.is_encrypted
        self.task.emit({
            "type": "playlist",
            "total": self.task.total,
            "encrypted": playlist.is_encrypted,
            "target_duration": playlist.target_duration,
            "version": playlist.version,
        })

    def on_segment(self, index: int, segment: M3U8Segment, filepath: str) -> None:
        self._advance(index, uri=segment.uri)

    def on_segment_data(self, index, segment, data, filename) -> None:
        self._advance(index, size=len(data), filename=filename)

    def on_all_downloaded(self, playlist: M3U8Playlist, tmp_dir: str) -> None:
        self.task.emit({"type": "all_downloaded", "tmp_dir": tmp_dir, "total": self.task.total})

    def _advance(self, index: int, **extra) -> None:
        with self._lock:
            self._done += 1
            done = self._done
        self.task.done = done
        payload = {"type": "segment", "index": index, "done": done, "total": self.task.total}
        payload.update(extra)
        self.task.emit(payload)


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


@dataclass
class Task:
    id: str
    req: DownloadRequest
    status: str = "pending"          # pending/running/done/error/cancelled
    total: int = 0
    done: int = 0
    encrypted: bool = False
    result: Optional[str] = None
    size: Optional[int] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    cancel_event: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _history: list[dict] = field(default_factory=list)
    _subscribers: "list[queue.Queue]" = field(default_factory=list)
    future: object = None

    # -- 事件广播 --------------------------------------------------------

    def emit(self, event: dict) -> None:
        """记录事件到历史并推给所有订阅者."""
        event.setdefault("ts", time.time())
        event["task_id"] = self.id
        with self._lock:
            self._history.append(event)
            for q in self._subscribers:
                q.put(event)

    def set_status(self, status: str, **extra) -> None:
        self.status = status
        if status == "running" and self.started_at is None:
            self.started_at = time.time()
        if status in TERMINAL_STATES:
            self.finished_at = time.time()
        payload = {"type": "status", "status": status}
        payload.update(extra)
        self.emit(payload)
        if status in TERMINAL_STATES:
            self._close_subscribers()

    def _close_subscribers(self) -> None:
        with self._lock:
            for q in self._subscribers:
                q.put(None)  # sentinel

    def subscribe(self) -> "tuple[list[dict], Optional[queue.Queue]]":
        """注册订阅者. 返回 (历史快照, 队列或None).

        若任务已结束, 队列为 None (只需回放历史即可)."""
        with self._lock:
            history = list(self._history)
            if self.status in TERMINAL_STATES:
                return history, None
            q: "queue.Queue" = queue.Queue()
            self._subscribers.append(q)
            return history, q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "input": self.req.m3u8_input,
            "output": self.result or self.req.output,
            "upload": self.req.upload,
            "total": self.total,
            "done": self.done,
            "encrypted": self.encrypted,
            "result": self.result,
            "size": self.size,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ---------------------------------------------------------------------------
# TaskManager
# ---------------------------------------------------------------------------


def _parse_headers(raw_headers: Optional[list[str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    if not raw_headers:
        return result
    for h in raw_headers:
        if ":" not in h:
            continue
        k, _, v = h.partition(":")
        result[k.strip()] = v.strip()
    return result


def _safe_name(name: str) -> str:
    """取 basename 并过滤危险字符, 防止路径穿越."""
    base = os.path.basename(name or "").strip() or "output.ts"
    return re.sub(r"[^\w.\-]", "_", base)


class TaskManager:
    def __init__(self, max_concurrent: int) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)

    def create(self, req: DownloadRequest) -> Task:
        task_id = uuid.uuid4().hex[:12]
        task = Task(id=task_id, req=req)
        with self._lock:
            self._tasks[task_id] = task
        task.future = self._executor.submit(self._run, task)
        return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[dict]:
        with self._lock:
            tasks = list(self._tasks.values())
        return [t.snapshot() for t in sorted(tasks, key=lambda t: t.created_at, reverse=True)]

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if not task:
            return False
        if task.status in TERMINAL_STATES:
            return False
        task.cancel_event.set()
        # 若仍在队列中未开始, 可立即取消
        if task.future is not None and task.future.cancel():
            task.set_status("cancelled", reason="队列中取消")
            self.remove(task_id)
        return True

    def remove(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status not in TERMINAL_STATES:
                return False
            del self._tasks[task_id]
            return True

    # -- 实际下载执行 ----------------------------------------------------

    def _run(self, task: Task) -> None:
        req = task.req
        if task.cancel_event.is_set():
            task.set_status("cancelled", reason="启动前取消")
            return

        task.set_status("running")
        try:
            # 每个任务独立目录, 避免输出/临时文件冲突
            task_dir = os.path.join(DOWNLOAD_ROOT, task.id)
            os.makedirs(task_dir, exist_ok=True)
            output_path = os.path.join(task_dir, _safe_name(req.output))
            tmp_dir = os.path.join(task_dir, "tmp")

            task.emit({"type": "start", "input": req.m3u8_input, "output": output_path})

            headers = _parse_headers(req.headers)
            http = HTTPClient(headers=headers, proxy=req.proxy, timeout=req.timeout)

            no_merge = req.no_merge
            if req.no_disk:
                if not req.upload:
                    task.error = "--no-disk 需要 --upload"
                    task.set_status("error", message=task.error)
                    return
                no_merge = True

            hooks: list[BaseHook] = [
                CancelHook(task.cancel_event),
                SegmentMergeHook(
                    output=output_path,
                    merge=not no_merge,
                    keep_temp=req.keep_temp,
                    tmp_dir=tmp_dir,
                ),
                ProgressHook(task),
            ]

            if req.upload:
                token = req.upload_token or os.getenv("ACCESS_TOKEN")
                if not token:
                    task.error = "upload 需要 upload_token 或 .env ACCESS_TOKEN"
                    task.set_status("error", message=task.error)
                    return
                hooks.append(YunpanUploadHook(
                    token=token,
                    remote_prefix=req.upload,
                    ondup=req.upload_ondup,
                    max_retries=req.upload_retries,
                    delete_after_upload=req.upload_delete,
                ))

            downloader = M3U8Downloader(
                m3u8_source=req.m3u8_input,
                output=output_path,
                base_url=req.base_url,
                workers=req.workers,
                retries=req.retries,
                no_merge=no_merge,
                keep_temp=req.keep_temp,
                tmp_dir=tmp_dir,
                no_disk=req.no_disk,
                http_client=http,
                hooks=hooks,
                key_param=req.key_param,
                key_format=req.key_format,
            )

            result = downloader.run()

            if os.path.exists(result) and os.path.isfile(result):
                task.size = os.path.getsize(result)
            task.result = result
            task.set_status("done", result=result, size=task.size)

        except _Cancelled:
            task.set_status("cancelled", reason="运行中取消")
        except SystemExit as e:
            task.error = f"下载器退出: {e}"
            task.set_status("error", message=task.error)
        except Exception as e:
            task.error = str(e)
            task.set_status("error", message=task.error, exception=type(e).__name__)


manager = TaskManager(MAX_CONCURRENT)


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------


app = FastAPI(title="M3U8 Downloader API", version="2.0.0")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "max_concurrent": MAX_CONCURRENT}


@app.post("/api/download")
def download(req: DownloadRequest) -> dict:
    """创建下载任务, 立即返回 task_id (不阻塞)."""
    task = manager.create(req)
    return {"task_id": task.id, "status": task.status}


@app.get("/api/tasks")
def list_tasks() -> dict:
    return {"tasks": manager.list()}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    task = manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task.snapshot()


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict:
    task = manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    ok = manager.cancel(task_id)
    return {"task_id": task_id, "cancel_requested": ok, "status": task.status}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str) -> dict:
    if not manager.remove(task_id):
        raise HTTPException(status_code=400, detail="任务不存在或未结束")
    return {"task_id": task_id, "removed": True}


@app.get("/api/tasks/{task_id}/stream")
def stream_task(task_id: str) -> StreamingResponse:
    """订阅任务进度: 先回放历史事件, 再实时推送 (NDJSON, HTTP Chunked)."""
    task = manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    history, q = task.subscribe()

    def event_stream():
        yield json.dumps({"type": "connected", "task_id": task_id, "ts": time.time()}, ensure_ascii=False) + "\n"
        # 回放历史
        for ev in history:
            yield json.dumps(ev, ensure_ascii=False) + "\n"
        if q is None:
            # 任务已结束, 历史已包含终态
            return
        try:
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    yield json.dumps({"type": "heartbeat", "ts": time.time()}) + "\n"
                    continue
                if item is None:
                    break
                #通过ndjson (newline defined json) 数组通过换行的方式来表示
                yield json.dumps(item, ensure_ascii=False) + "\n"
        finally:
            task.unsubscribe(q)

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


# 静态页面挂载 (放最后, 避免拦截 /api/*)
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
if os.path.isdir(_WEB_DIR):
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
