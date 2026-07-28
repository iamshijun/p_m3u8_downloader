"""FastAPI 服务: 封装 m3u8_downloader 对外提供 HTTP 接口.

启动:
    pip install fastapi uvicorn
    uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload

接口:
    POST /api/download   → StreamingResponse (application/x-ndjson) 流式返回进度
    GET  /api/health     → 健康检查
    GET  /                → 静态页面 web/index.html
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Optional

from fastapi import FastAPI
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
# 请求模型
# ---------------------------------------------------------------------------


class DownloadRequest(BaseModel):
    """下载请求参数, 字段与 m3u8_downloader 命令行参数一一对应."""

    m3u8_input: str = Field(..., description="m3u8 文件路径或 URL")
    output: str = Field("output.ts", description="输出文件名")
    base_url: Optional[str] = Field(None, description="视频片段的根 URL")
    workers: int = Field(10, ge=1, le=64, description="并发下载线程数")
    retries: int = Field(3, ge=0, description="失败重试次数")
    timeout: int = Field(5, ge=1, description="HTTP 请求超时秒数")
    tmp_dir: Optional[str] = Field(None, description="显式指定临时下载目录")
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
# 进度 Hook: 将事件推入 queue, 供 SSE/Chunked 流消费
# ---------------------------------------------------------------------------


class ProgressHook(BaseHook):
    """将下载生命周期事件推入线程安全队列."""

    def __init__(self, event_queue: "queue.Queue[dict]") -> None:
        self.q = event_queue
        self.total = 0
        self.done = 0
        self.failed = 0
        self._lock = threading.Lock()

    def on_playlist_parsed(self, playlist: M3U8Playlist) -> None:
        self.total = len(playlist)
        self._push({
            "type": "playlist",
            "total": self.total,
            "encrypted": playlist.is_encrypted,
            "target_duration": playlist.target_duration,
            "version": playlist.version,
        })

    def on_segment(self, index: int, segment: M3U8Segment, filepath: str) -> None:
        with self._lock:
            self.done += 1
            done = self.done
        self._push({
            "type": "segment",
            "index": index,
            "done": done,
            "total": self.total,
            "uri": segment.uri,
        })

    def on_segment_data(
        self, index: int, segment: M3U8Segment, data: bytes, filename: str
    ) -> None:
        with self._lock:
            self.done += 1
            done = self.done
        self._push({
            "type": "segment_data",
            "index": index,
            "done": done,
            "total": self.total,
            "size": len(data),
            "filename": filename,
        })

    def on_all_downloaded(self, playlist: M3U8Playlist, tmp_dir: str) -> None:
        self._push({
            "type": "all_downloaded",
            "tmp_dir": tmp_dir,
            "total": self.total,
        })

    def on_finish(self, result_path: str) -> None:
        # 只 push 一次 (可能有多个 hook 触发, 但这里只有本 hook 关心)
        self._push({"type": "finish", "result": result_path})

    def _push(self, payload: dict) -> None:
        payload["ts"] = time.time()
        self.q.put(payload)


# ---------------------------------------------------------------------------
# 下载任务执行
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


def _run_download(req: DownloadRequest, q: "queue.Queue[Optional[dict]]") -> None:
    """在后台线程执行下载, 事件通过 queue 输出."""
    try:
        q.put({"type": "start", "input": req.m3u8_input, "output": req.output})

        headers = _parse_headers(req.headers)
        http = HTTPClient(headers=headers, proxy=req.proxy, timeout=req.timeout)

        hooks: list[BaseHook] = [
            SegmentMergeHook(
                output=req.output,
                merge=not req.no_merge,
                keep_temp=req.keep_temp,
                tmp_dir=req.tmp_dir,
            ),
            ProgressHook(q),
        ]

        if req.upload:
            token = req.upload_token or os.getenv("ACCESS_TOKEN")
            if not token:
                q.put({"type": "error", "message": "upload 需要 upload_token 或 .env ACCESS_TOKEN"})
                return
            hooks.append(YunpanUploadHook(
                token=token,
                remote_prefix=req.upload,
                ondup=req.upload_ondup,
                max_retries=req.upload_retries,
                delete_after_upload=req.upload_delete,
            ))

        # --no-disk 校验
        no_merge = req.no_merge
        if req.no_disk:
            if not req.upload:
                q.put({"type": "error", "message": "--no-disk 需要 --upload"})
                return
            no_merge = True

        downloader = M3U8Downloader(
            m3u8_source=req.m3u8_input,
            output=req.output,
            base_url=req.base_url,
            workers=req.workers,
            retries=req.retries,
            no_merge=no_merge,
            keep_temp=req.keep_temp,
            tmp_dir=req.tmp_dir,
            no_disk=req.no_disk,
            http_client=http,
            hooks=hooks,
            key_param=req.key_param,
            key_format=req.key_format,
        )

        result = downloader.run()

        size = None
        if os.path.exists(result) and os.path.isfile(result):
            size = os.path.getsize(result)

        q.put({
            "type": "done",
            "result": result,
            "size": size,
        })

    except SystemExit as e:
        # 底层用了 sys.exit 抛错, 转成事件
        q.put({"type": "error", "message": f"下载器退出: {e}"})
    except Exception as e:
        q.put({"type": "error", "message": str(e), "exception": type(e).__name__})
    finally:
        q.put(None)  # sentinel


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------


app = FastAPI(title="M3U8 Downloader API", version="1.0.0")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/download")
def download(req: DownloadRequest) -> StreamingResponse:
    """启动下载并以 Chunked/NDJSON 流式返回进度事件.

    响应体每行一个 JSON 对象 (application/x-ndjson):
      {"type": "start", ...}
      {"type": "playlist", "total": N, "encrypted": bool, ...}
      {"type": "segment", "index": i, "done": k, "total": N}
      {"type": "all_downloaded", ...}
      {"type": "finish", "result": "..."}
      {"type": "done", "result": "...", "size": bytes}
      {"type": "error", "message": "..."}
    """
    event_queue: "queue.Queue[Optional[dict]]" = queue.Queue()
    thread = threading.Thread(target=_run_download, args=(req, event_queue), daemon=True)
    thread.start()

    def event_stream():
        # 先立即 flush 一个开始事件, 让客户端尽早看到数据
        yield json.dumps({"type": "connected", "ts": time.time()}, ensure_ascii=False) + "\n"
        heartbeat_interval = 15  # 秒
        while True:
            try:
                item = event_queue.get(timeout=heartbeat_interval)
            except queue.Empty:
                # 心跳, 防止代理/浏览器超时断开
                yield json.dumps({"type": "heartbeat", "ts": time.time()}) + "\n"
                continue
            if item is None:
                break
            yield json.dumps(item, ensure_ascii=False) + "\n"

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # 关闭 nginx 缓冲
        },
    )


# 静态页面挂载 (放最后, 避免拦截 /api/*)
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
if os.path.isdir(_WEB_DIR):
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
