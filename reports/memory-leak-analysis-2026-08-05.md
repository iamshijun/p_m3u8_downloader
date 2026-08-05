# M3U8 Video Fetcher 内存占用分析报告

- **报告日期**: 2026-08-05
- **分析范围**: `api_server.py` / `m3u8_downloader.py` / `pan_upload.py`
- **分析对象**: api_server 长时间运行下的内存增长与 OOM 风险

---

## 一、问题描述

### 1.1 用户反馈

服务在 `api_server.py` 模式下长时间运行(经 systemd 以 `m3u8-downloader` 部署,见 `scripts/install-systemd.sh`)时,即使所有下载任务都已完成,**进程 RSS 仍持续占据较高水平,从不回落**。在连续运行数天后,被 Linux 内核 OOM killer 杀死。

### 1.2 复现特征

- 触发场景:批量导入(`POST /api/download/batch`)+ 多个 SSE 长连接订阅(`/api/tasks/{id}/stream`)的混合负载
- 持续期:首次 OOM 出现时间为服务启动后 2~7 天(与吞吐量和并发订阅数相关)
- 现象:RSS 单调上升,无下降趋势,即使任务队列为空

### 1.3 涉及文件

| 文件 | 角色 |
|---|---|
| `api_server.py` | FastAPI 入口、任务注册表、清理线程、SSE 订阅 |
| `m3u8_downloader.py` | 实际下载、线程池、tqdm 进度、Hook 链 |
| `pan_upload.py` | 百度云盘上传(httpx 直连) |
| `scripts/install-systemd.sh` | 部署入口,目前未设 `MemoryMax` |

---

## 二、根因分析(按影响权重)

### 2.1 [最关键] `task.future` 持有整个 `_run` 栈帧 30 分钟

**位置**: `api_server.py:337`、`api_server.py:386-389`

```python
# create()
task.future = self._executor.submit(self._run, task)   # 任务完成后 future 引用从未被清空

# remove() — 但要等 30 分钟 (TERMINAL_TTL_SECONDS=1800) 才执行
task._history.clear()
task._subscribers.clear()
task.future = None   # ← 这是释放 _run 帧的唯一时机
```

`concurrent.futures.Future` 在 `result()` 未被调用的情况下,会一直保留对 worker 线程函数帧的引用,直到 future 本身被释放。`_run` 函数帧持有什么?

- `M3U8Downloader` 实例(含 `self.http` / `self.hooks` / 全部 hooks 对 `Task` 的反向引用)
- 解析后的 `M3U8Playlist`(可能 1000~5000 个 `M3U8Segment`,每个 ~150B → 单 playlist 几百 KB 到几 MB)
- 三个 hook:`CancelHook` → `task`;`ProgressHook` → `task`;`YunpanUploadHook` → `token`
- `HTTPClient` / 它的 `OpenerDirector`

**后果**: 任务结束后,这堆对象在 `_tasks` dict 里再活 30 分钟。3 个并发 + 30 min TTL × 吞吐量 = 持续保留大量已结束任务的"尸体"。

更糟的是形成了**循环引用**:
```
task.future → _run 帧 → downloader → hooks[ProgressHook] → task
   ↑_________________________________________________________|
```
这需要触发 **循环 GC**(`gc.collect()`)才能回收。Python 的循环 GC 阈值自适应,但分配速度稳定时可能很久才触发一次,期间所有这些对象都驻留在内存里。

### 2.2 [高] SSE 客户端断连时 `unsubscribe` 不一定执行

**位置**: `api_server.py:611-646`

```python
def event_stream():
    ...
    try:
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield json.dumps({"type": "heartbeat", ...}) + "\n"
                continue
            ...
    finally:
        task.unsubscribe(q)
```

`finally` 只在 generator 正常 `close()` 时执行。如果客户端(浏览器)异常断开(刷新、关 tab、网络中断)而 Starlette **没及时 close generator**,`unsubscribe` 不会跑,queue 留在 `_subscribers` 里。

虽然每个 task 终态时 `_close_subscribers` 会清空(`set_status` 触发),但任务还在跑时,长生命周期下大量幽灵订阅者 queue 会持续持有它们已 push 的事件(每个 queue `maxsize=200`,有几百个就上 MB 级别)。

**检测方法**: 加一个 `/api/debug/subscribers` 端点看每个 task 的 `_subscribers` 数量。

### 2.3 [中] tqdm 每个 task 都创建独立后台渲染线程

**位置**: `m3u8_downloader.py:51-117`、`m3u8_downloader.py:782`、`m3u8_downloader.py:845`

`ProgressBar.__exit__` → `close()` → `tqdm.close()`,正常路径 OK。但已知 tqdm 在 stderr 关闭 / 异常路径上会泄漏渲染线程(tqdm 维护一个全局 `tqdm._instances`,每个实例一个 `Monitor` 线程)。

每个线程默认 **8 MB 虚拟栈**(Linux),驻留小但虚拟地址空间持续膨胀。批量导入 500 个任务 → 1500 个 tqdm 实例 → 12 GB 虚拟内存,虽然 RSS 不会线性增长,但 **碎片化 + 长跑几天会加剧系统 VMA 压力**,被 OOM killer 误判。

`web/index.html` 的前端每张卡片也会在 task 完成后不断累积 log 行(browser 端问题,服务器无关,但容易误判为后端问题)。

### 2.4 [中] Python allocator 碎片化(pymalloc 高水位线)

即使所有对象都被释放,**`pymalloc` 不会把内存还给 OS**。RSS 只升不降,这是 CPython 长期服务的通病。`tracemalloc` 可以区分"对象真正占用"和"pymalloc 保留":

```python
import tracemalloc; tracemalloc.start()
# ... 跑一会儿
snap = tracemalloc.take_snapshot()
for stat in snap.statistics("lineno")[:10]:
    print(stat)
```

如果 RSS 涨但 tracemalloc 报的总分配稳定 → 碎片化,要靠周期重启或换 `jemalloc`。
如果 tracemalloc 也在涨 → 真实泄漏,看 #1/#2。

### 2.5 [低] `HTTPClient.__init__` 全局副作用

**位置**: `m3u8_downloader.py:215-218`

```python
if proxy:
    self._opener = urllib.request.build_opener(proxy_handler)
    urllib.request.install_opener(self._opener)   # ← 全局!
```

每次创建带代理的 `HTTPClient` 都会 `install_opener` 改写模块全局,旧 opener 被丢弃 → 引用链:`urllib._opener` 永远指向最后一个 HTTPClient 的 opener。

不算严重泄漏(单 opener ~几 KB),但是个 bug:同时跑多个代理任务时,代理设置会被覆盖。

### 2.6 [低] 任务注册表无数量上限

`TaskManager._tasks` 只按 TTL 时间清理,没有 size cap。`/api/download/batch` 默认允许 `BATCH_LIMIT=500`,但批量场景下,如果提交速度 > 完成速度(比如大文件慢任务堆积),`_tasks` 数量会持续上涨。每个 task 至少 1 KB(req + 50 history + future 引用),1000 个 task 就上 MB 级别。

---

## 三、验证方案

```python
# 临时加一个 debug 端点,看真实分布:
import gc, sys, tracemalloc

@app.get("/api/debug/memory")
def debug_memory():
    gc.collect()
    import objgraph
    # objgraph.show_growth(limit=10)  # 看哪些类型增长最快
    return {
        "tasks_count": len(manager._tasks),
        "by_status": _count_by_status(manager._tasks),
        "rss_mb": ...,
        "tracemalloc_top": [...]  # tracemalloc 快照前 10
    }
```

**关键指标**:
- 如果 `tasks_count` 在涨,但每个 task 的 `len(_history)` 都很小 → **问题在 #1**(future 持有大帧)
- 如果 `tasks_count` 稳定 → **问题在 #2**(SSE 幽灵订阅)或 #4(碎片化)
- 如果 RSS 涨但 `tracemalloc` 报告稳定 → **问题在 #4**(pymalloc 碎片)

---

## 四、修复优先级建议(按 ROI 排序)

1. **`set_status` 终态时立即 `task.future = None`** — 直接释放 `_run` 帧,把保留时间从 30 min 降到秒级。最有效,改 1 行。
2. **改 SSE 断开检测**:用 `request.is_disconnected()`(或 ASGI 的 `receive` channel)在 disconnect 时主动 `task.unsubscribe(q)`。
3. **`gc.collect()` 周期性手动调用**(清理线程里加一段)— 打破循环引用。
4. **注册表加 size cap**(如 `MAX_REGISTRY = 2000`),超过时按 LRU/oldest 驱逐。
5. **生产环境加 systemd `MemoryMax`**(`install-systemd.sh` 里没设),OOM 之前可控重启。
6. **换 `httpx.Client` 单例** + 复用 connection pool,顺便减少 #5 那个 bug 的影响面。

---

## 五、补充知识点:RSS(常驻内存集)详解

### 5.1 定义

**RSS** = **Resident Set Size**(常驻内存集)

Linux 下表示一个进程**当前实际占用的物理内存(RAM)大小**,单位通常是 KB / MB。

可以用 `ps` / `top` 直接看到,例如:

```bash
ps -o pid,rss,cmd -p <pid>
#   PID    RSS CMD
# 12345 245678 /usr/bin/python3 api_server.py
#                  ↑ 约 240 MB
```

或 `top` 的 `RES` 列就是 RSS。

### 5.2 相关概念区分

| 缩写 | 全称 | 含义 |
|---|---|---|
| **RSS** | Resident Set Size | **物理内存中**实际占用的部分(不含 swap、不含未加载的虚拟页) |
| **VSZ** / **VIRT** | Virtual Size | 进程虚拟地址空间总大小(含未实际分配的、含共享库映射) |
| **PSS** | Proportional Set Size | RSS 的"按比例分摊"版本(共享内存按使用进程数平摊),更准确反映实际开销 |
| **USS** | Unique Set Size | 进程独占的物理内存(完全不算共享),OOM 评估最严格 |

### 5.3 为什么要看 RSS 而不是 VIRT

报告中提的"tqdm 线程 8MB × 1500 个 = 12GB"指的是 **VIRT**(虚拟地址空间),这是因为每个线程默认 8MB 栈只是虚拟保留,内核按需分配物理页。所以 RSS 不会真的因为线程数线性暴涨 —— 但 VMA(虚拟内存区域)膨胀会让内核在某些极端情况下(比如 cgroup `memory.high` 触发回收)误判或 OOM。

### 5.4 在排查本服务时的用法

```bash
# 持续采样看 RSS 趋势
while true; do
  ps -o rss= -p $(cat run/server.pid) >> rss.log
  sleep 60
done

# 或用 pidstat
pidstat -r -p <pid> 60
```

**判读规则**:
- 如果 RSS 单调上升 → 真泄漏(对象没释放,或 pymalloc 没还内存给 OS)
- 如果 RSS 平稳但服务被 OOM 杀 → 大概率是其他进程抢内存,或 cgroup 限制被触发
- 如果 VIRT 涨但 RSS 稳 → 主要是虚拟地址空间膨胀(线程数、mmap 区),不一定需要紧急处理,但要警惕 OOM killer 的"冤枉"触发
