"""FastAPI 服务: 封装 m3u8_downloader 提供多任务下载 + 流式进度接口.

启动:
    pip install fastapi uvicorn
    python3 api_server.py
    # 或 uvicorn api_server:app --host 0.0.0.0 --port 8000

接口:
    POST   /api/download              → 创建下载任务, 返回 {task_id}
    POST   /api/download/batch        → 批量创建下载任务, 返回 {created, skipped, errors}
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

import asyncio
import gc
import json
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from m3u8_downloader import (
    BaseHook,
    DownloadConfig,
    HTTPClient,
    M3U8Downloader,
    M3U8Playlist,
    M3U8Segment,
    SegmentMergeHook,
    UploadHook,
    YunpanUploadHook,
    create_upload_hook,
)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
DOWNLOAD_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
BATCH_LIMIT = int(os.getenv("BATCH_LIMIT", "500"))   # 单次批量导入的任务上限
MAX_REGISTRY = int(os.getenv("MAX_REGISTRY", "2000"))  # 任务注册表最大条目数, 超过时按 finished_at 驱逐最旧

# 内存控制常量
HISTORY_MAX = 200              # 每个任务保留的最大事件数（超出则丢弃旧事件）
SUBSCRIBER_QUEUE_MAX = 200     # 每个订阅者队列的缓冲区上限（慢消费时丢弃旧事件）
TERMINAL_TTL_SECONDS = int(os.getenv("TASK_TTL_SECONDS", "1800"))  # 已结束任务保留时间
CLEANUP_INTERVAL = 60          # 清理线程扫描间隔（秒）


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class UploadConfig(BaseModel):
    """通用上传配置: 后端无关的上传参数.

    支持通过 type 字段选择不同的上传后端 (yunpan / s3 / gcs / local 等).
    通用字段: type, path, token
    后端特有字段以 flat 方式内联 (兼容 yunpan 的 ondup / max_retries / delete_after_upload).
    """

    type: str = Field("yunpan", description="上传后端类型: yunpan / s3 / gcs / local")
    path: str = Field(..., description="远程/目标路径")
    token: Optional[str] = Field(None, description="认证 token (部分后端需要)")
    ondup: str = Field("overwrite", description="冲突策略 (yunpan): fail / overwrite / newcopy")
    max_retries: int = Field(5, description="上传重试次数")
    delete_after_upload: bool = Field(True, description="上传成功后是否删除本地分片")


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

    # 上传配置 (可选, 传入时触发上传)
    upload: Optional[UploadConfig] = Field(None, description="上传配置")


class BatchDownloadRequest(BaseModel):
    """批量下载请求: tasks 为若干 DownloadRequest 字段的 JSON 对象.

    每个任务必填 m3u8_input / output; 其余字段为前端合并后的表单配置.
    使用 dict 而非 DownloadRequest, 以便逐条单独校验、跳过非法项, 而不至于整批 422.
    可选携带 line (原始行号), 仅用于错误/跳过明细回显, 不会传给 DownloadRequest.
    """

    tasks: list[dict] = Field(..., min_length=1, description="要批量创建的任务列表")


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


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.ERROR, TaskStatus.CANCELLED)


@dataclass
class Task:
    id: str
    req: DownloadRequest
    status: TaskStatus = TaskStatus.PENDING
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
        """记录事件到历史并推给所有订阅者.

        历史有上限, 队列为有界队列; 慢消费/满队列时丢弃事件, 防止内存无限增长."""
        event.setdefault("ts", time.time())
        event["task_id"] = self.id
        with self._lock:
            self._history.append(event)
            while len(self._history) > HISTORY_MAX:
                self._history.pop(0)
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass  # 消费者太慢, 丢弃该事件

    def set_status(self, status: TaskStatus, **extra) -> None:
        self.status = status
        if status == TaskStatus.RUNNING and self.started_at is None:
            self.started_at = time.time()
        if status.is_terminal:
            self.finished_at = time.time()
            # 任务结束后只保留最近 50 条事件用于回放和快照, 释放大部分历史内存
            with self._lock:
                if len(self._history) > 50:
                    del self._history[:-50]
        payload = {"type": "status", "status": status.value}
        if self.started_at is not None:
            payload["started_at"] = self.started_at
        if self.finished_at is not None:
            payload["finished_at"] = self.finished_at
        payload.update(extra)
        self.emit(payload)
        if status.is_terminal:
            self._close_subscribers()
            # 终态时立即释放 future 引用, 让 _run 帧和 downloader 对象可以被 GC 回收
            # 否则 future 会一直保留 _run 帧(含 M3U8Downloader / 解析后的 playlist 等)直到 TTL 清理
            self.future = None

    def _close_subscribers(self) -> None:
        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            self._subscribers.clear()  # 清空引用, 辅助 GC

    def subscribe(self) -> "tuple[list[dict], Optional[queue.Queue]]":
        """注册订阅者. 返回 (历史快照, 队列或None).

        若任务已结束, 队列为 None (只需回放历史即可)."""
        with self._lock:
            history = list(self._history)
            if self.status.is_terminal:
                return history, None
            q: "queue.Queue" = queue.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
            self._subscribers.append(q)
            return history, q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def snapshot(self) -> dict:
        upload_path = self.req.upload.path if self.req.upload else None
        return {
            "id": self.id,
            "status": self.status.value,
            "input": self.req.m3u8_input,
            "output": self.result or self.req.output,
            "upload": upload_path,
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


def _safe_name(name: str) -> str:
    """取 basename 并过滤危险字符, 防止路径穿越."""
    base = os.path.basename(name or "").strip() or "output.ts"
    return re.sub(r"[^\w.\-]", "_", base)


def _run_preflight(hook_type: str, token: str) -> None:
    """上传预检: 根据后端类型执行对应的认证/连通性检查."""
    if hook_type == "yunpan":
        from pan_upload import preflight_check
        preflight_check(token=token)
    # 其他后端可在此扩展
    # elif hook_type == "s3":
    #     _s3_preflight(...)


class TaskManager:
    def __init__(self, max_concurrent: int) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

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

    def registered_io_keys(self) -> "tuple[set[str], set[str]]":
        """返回当前注册表中所有任务的 (URL 集合, 规范化文件名集合), 用于批量去重."""
        with self._lock:
            tasks = list(self._tasks.values())
        urls = {t.req.m3u8_input.strip() for t in tasks}
        names = {_safe_name(t.req.output) for t in tasks}
        return urls, names

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if not task:
            return False
        if task.status.is_terminal:
            return False
        task.cancel_event.set()
        # 尽力取消 future: PENDING 时会成功, RUNNING 时一定返回 False (会由 _run 捕获 _Cancelled 收尾)
        if task.future is not None:
            task.future.cancel()
        # concurrent.futures.Future.cancel() 对 ThreadPoolExecutor 中的 future 只在 state == PENDING 时返回 True。
        # 一旦 worker 线程把任务从队列里取出开始执行,state 变成 RUNNING,cancel() 就一定返回False。                                                                                                                                                                                                                                                                                                                                                                                     
        # self._executor.submit(self._run, task) 之后,worker 线程取任务是毫秒级的事(从 PENDING → RUNNING 几乎没有窗口),
        # 再加上 _run 自身也会快速 set_status("running"),等到 cancel 进来时 
        # future 基本已经是 RUNNING,所以 cancel() 返回 False 是预期行为,不是异常
        # 不论 future.cancel() 结果如何, 都要把状态推进到 cancelled,
        # 否则 remove() 会因非终态而失败, 任务留在 registry 中
        if not task.status.is_terminal:
            task.set_status(TaskStatus.CANCELLED, reason="用户取消")
        return True

    def remove(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return True
            if not task.status.is_terminal:
                return False
            del self._tasks[task_id]
        # 释放引用 辅助 GC — 必须在锁外操作避免死锁
        task._history.clear()
        task._subscribers.clear()
        task.future = None
        return True

    def _cleanup_loop(self) -> None:
        """后台线程: 定期清理超过 TTL 的已结束任务 + size cap 驱逐 + 显式 GC."""
        while True:
            time.sleep(CLEANUP_INTERVAL)
            # 显式触发循环 GC, 打破 task <-> future <-> _run 帧 之间的循环引用
            # (set_status 终态已释放 future, 这里兜底处理其他潜在循环)
            gc.collect()
            #保留的时间窗口,小于的任务会被清理
            cutoff = time.time() - TERMINAL_TTL_SECONDS
            with self._lock:
                stale = [
                    tid for tid, t in self._tasks.items()
                    if t.status.is_terminal
                    and t.finished_at is not None
                    and t.finished_at < cutoff
                ]
                # size cap: 即使 TTL 未到, 总量超限也按 finished_at 驱逐最旧的终态任务
                if len(self._tasks) > MAX_REGISTRY:
                    terminal_sorted = sorted(
                        [(tid, t) for tid, t in self._tasks.items() if t.status.is_terminal],
                        key=lambda x: x[1].finished_at or 0,
                    )
                    excess = len(self._tasks) - MAX_REGISTRY
                    for tid, _ in terminal_sorted[:excess]:
                        if tid not in stale:
                            stale.append(tid)
            for tid in stale:
                self.remove(tid)

    # -- 实际下载执行 ----------------------------------------------------

    def _run(self, task: Task) -> None:
        req = task.req
        if task.cancel_event.is_set():
            task.set_status(TaskStatus.CANCELLED, reason="启动前取消")
            return

        task.set_status(TaskStatus.RUNNING)
        try:
            # 每个任务独立目录, 避免输出/临时文件冲突
            task_dir = os.path.join(DOWNLOAD_ROOT, task.id)
            os.makedirs(task_dir, exist_ok=True)
            output_path = os.path.join(task_dir, _safe_name(req.output))
            tmp_dir = os.path.join(task_dir, "tmp")

            task.emit({"type": "start", "input": req.m3u8_input, "output": output_path})

            headers = HTTPClient.parse_headers(req.headers)
            http = HTTPClient(headers=headers, proxy=req.proxy, timeout=req.timeout)

            no_merge = req.no_merge
            if req.no_disk:
                if not req.upload:
                    task.error = "--no-disk 需要 --upload"
                    task.set_status(TaskStatus.ERROR, message=task.error)
                    return
                no_merge = True

            hooks: list[BaseHook] = [
                CancelHook(task.cancel_event),
                SegmentMergeHook(
                    output=output_path,
                    merge=not no_merge,
                    keep_temp=req.keep_temp,
                    tmp_dir=tmp_dir,
                    quiet=True,
                ),
                ProgressHook(task),
            ]

            if req.upload:
                upload_cfg = req.upload
                token = upload_cfg.token or os.getenv("ACCESS_TOKEN")
                if not token:
                    task.error = "upload 需要 upload.token 或 .env ACCESS_TOKEN"
                    task.set_status(TaskStatus.ERROR, message=task.error)
                    return
                # 下载前预检 (按后端类型分别校验)
                task.emit({"type": "preflight", "upload": upload_cfg.path})
                try:
                    _run_preflight(upload_cfg.type, token)
                except Exception as e:
                    task.error = f"上传预检失败: {e}"
                    task.set_status(TaskStatus.ERROR, message=task.error, stage="preflight")
                    return
                task.emit({"type": "preflight_ok", "upload": upload_cfg.path})
                hooks.append(create_upload_hook(
                    upload_cfg.type,
                    token=token,
                    remote_prefix=upload_cfg.path,
                    ondup=upload_cfg.ondup,
                    max_retries=upload_cfg.max_retries,
                    delete_after_upload=upload_cfg.delete_after_upload,
                ))

            config = DownloadConfig(
                m3u8_source=req.m3u8_input,
                output=output_path,
                base_url=req.base_url,
                workers=req.workers,
                retries=req.retries,
                no_merge=no_merge,
                keep_temp=req.keep_temp,
                tmp_dir=tmp_dir,
                no_disk=req.no_disk,
                key_param=req.key_param,
                key_format=req.key_format,
                quiet=True,
            )
            downloader = M3U8Downloader(
                config=config,
                http_client=http,
                hooks=hooks,
            )

            result = downloader.run()

            if os.path.exists(result) and os.path.isfile(result):
                task.size = os.path.getsize(result)
            task.result = result
            task.set_status(TaskStatus.DONE, result=result, size=task.size)

        except _Cancelled:
            task.set_status(TaskStatus.CANCELLED, reason="运行中取消")
        except SystemExit as e:
            task.error = f"下载器退出: {e}"
            task.set_status(TaskStatus.ERROR, message=task.error)
        except Exception as e:
            task.error = str(e)
            task.set_status(TaskStatus.ERROR, message=task.error, exception=type(e).__name__)


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


@app.post("/api/download/batch")
def download_batch(req: BatchDownloadRequest) -> dict:
    """批量创建下载任务: 逐个校验 + 去重后创建.

    去重规则: URL 或 规范化后的文件名 与本次批量内或当前注册表中仍在的任务重复 → 跳过.
    非法项(缺 URL/文件名、参数校验失败、超出 BATCH_LIMIT)计入 errors, 合法项照常创建.
    返回 {"created": [...], "skipped": [...], "errors": [...]}.
    """
    created: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    seen_urls, seen_names = manager.registered_io_keys()

    for i, raw in enumerate(req.tasks, start=1):
        if i > BATCH_LIMIT:
            errors.append({"line": i, "reason": f"超出单次导入上限 {BATCH_LIMIT}"})
            continue
        item = dict(raw)
        line_no = item.pop("line", i)
        url = str(item.get("m3u8_input") or "").strip()
        # upload 可能是字符串 (兼容) 或 UploadConfig dict
        upload_raw = item.get("upload")
        upload_path = ""
        if isinstance(upload_raw, str):
            upload_path = upload_raw.strip()
            if upload_path:
                item["upload"] = {"type": "yunpan", "path": upload_path}
            else:
                item["upload"] = None
        elif isinstance(upload_raw, dict):
            upload_path = str(upload_raw.get("path", "") or "").strip()

        if not url:
            errors.append({"line": line_no, "reason": "缺少 URL"})
            continue
        if not upload_path:
            errors.append({"line": line_no, "reason": "缺少文件名", "input": url})
            continue
        if url in seen_urls:
            skipped.append({"line": line_no, "reason": "URL 重复", "input": url})
            continue
        name_key = upload_path
        if name_key in seen_names:
            skipped.append({"line": line_no, "reason": "文件名重复", "upload": upload_path})
            continue

        item["m3u8_input"] = url
        try:
            task_req = DownloadRequest(**item)
        except Exception as e:
            errors.append({"line": line_no, "reason": f"参数校验失败: {e}", "input": url})
            continue

        seen_urls.add(url)
        seen_names.add(name_key)
        task = manager.create(task_req)
        created.append({"task_id": task.id, "m3u8_input": url, "upload": upload_path})

    return {"created": created, "skipped": skipped, "errors": errors}


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
async def stream_task(task_id: str, request: Request) -> StreamingResponse:
    """订阅任务进度: 先回放历史事件, 再实时推送 (NDJSON, HTTP Chunked)."""
    task = manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    history, q = task.subscribe()

    async def event_stream():
        yield json.dumps({"type": "connected", "task_id": task_id, "ts": time.time()}, ensure_ascii=False) + "\n"
        # 回放历史
        for ev in history:
            yield json.dumps(ev, ensure_ascii=False) + "\n"
        if q is None:
            # 任务已结束, 历史已包含终态
            return
        try:
            while True:
                # 检测客户端断开, 主动退出避免 finally 迟迟不执行导致 queue 泄漏
                if await request.is_disconnected():
                    break
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(1)
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
