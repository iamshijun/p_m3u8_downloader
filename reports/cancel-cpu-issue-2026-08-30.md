# [Bug] 取消下载中任务导致进程 CPU 打满直至被系统 Kill

- **创建日期**: 2026-08-30
- **严重程度**: 高 (P1)
- **涉及文件**: `m3u8_downloader.py` / `api_server.py`
- **仓库**: `p_m3u8_downloader`

---

## 一、问题描述

### 1.1 用户反馈

下载进行中时,对未完成任务发起**取消**(`POST /api/tasks/{id}/cancel`),几乎每次都会让整个服务进程 **CPU 打满**,持续数分钟,直到被系统 kill(或被迫手动重启)。期间服务无响应,新任务无法正常启动。

### 1.2 现场特征 (来自 `run/server.log`)

```
[*] 共发现 938 个视频片段
[*] 开始不落地下载 (并发数: 10)...
下载&上传:   8/938 [00:24<53:55, 3.48s/seg]   ← 取消前
POST /api/tasks/fc5ec274c602/cancel HTTP/1.1" 200 OK   ← 发起取消
下载&上传:   9/938 [00:30<1:04:43, 4.18s/seg]   ← 取消后仍在下载!
DELETE /api/tasks/fc5ec274c602 HTTP/1.1" 200 OK   ← 任务已被移除,但下载线程还在跑
```

任务状态立即变为 `cancelled`,但**下载并未停止** —— 剩余约 930 个分片仍被全部下载(938 × ~4s/分片 ÷ 10 并发 ≈ **6 分钟持续满载**)。

### 1.3 影响

- 取消是"假取消":剩余分片全部下载完才真正停止
- 每个僵尸任务占住 `MAX_CONCURRENT_DOWNLOADS`(默认 3)线程池槽位,新任务永远 `pending`
- 多个任务同时取消 → 数十个线程持续下载+解密+上传 → CPU 打满、资源耗尽
- 服务无响应后用户反复重启/取消 → 情况恶化,最终被系统 kill

---

## 二、根因分析

### 2.1 [核心] `ThreadPoolExecutor` 退出时隐式 `shutdown(wait=True)` 排干所有剩余分片

**位置**: `m3u8_downloader.py` — `_download_segments()` / `_download_segments_no_disk()`

```python
with ThreadPoolExecutor(max_workers=self.config.workers) as executor:   # ← 问题所在
    futures = {executor.submit(...): i for ...}
    for future in as_completed(futures):
        idx, filepath, status = future.result()   # ← 取消时 _Cancelled 在此抛出
```

取消机制本身没问题:取消时 `CancelHook` 在分片回调处抛出 `_Cancelled`(派生自 `BaseException`,绕过下载器的 `except Exception` 重试),并在 `future.result()` 处传播。

**但** `_Cancelled` 一旦从 `with` 块中抛出,Python 仍会执行 `ThreadPoolExecutor.__exit__` → `shutdown(wait=True)`,而该调用会**阻塞等待所有已提交的任务执行完毕**——包括**排队中尚未开始**的分片:

- 已提交的剩余分片全部照常下载(每个 `http.get` 是阻塞网络调用,取消无法中断)
- `_Cancelled` 只能等到执行器排干后才继续向上传播
- 于是"取消"退化为"把整份播放列表下载完"

**验证数据**(本地模拟 CDN + 真实 `api_server` 复现):

| 场景 | 取消前 | 取消事件设置 | 任务真正停止 |
|---|---|---|---|
| 300 分片 (磁盘模式) | done=15 | ~0.75s | **40.45s** (修复后 2.37s) |
| 150 加密分片 (no_disk) | done=15 | ~0.35s | **80.32s** (修复后 6.99s) |
| 200 分片 (端到端 API) | done=19 | 0.27s | 持续下载全部剩余分片 (修复后 1.28s) |

### 2.2 [连带] `tmp_dir` 未创建 → 磁盘模式下载全部失败

**位置**: `api_server.py` — `TaskManager._run()`

```python
task_dir = os.path.join(DOWNLOAD_ROOT, task.id)
os.makedirs(task_dir, exist_ok=True)      # 只创建了任务目录
tmp_dir = os.path.join(task_dir, "tmp")   # ← 从未创建!
```

下载器对**已指定** `tmp_dir` 的配置不会再 `tempfile.mkdtemp`,因此每个分片 `open(dest, "wb")` 都抛 `FileNotFoundError`:

```
[!] 片段 4 (seg_000004.ts): 失败: [Errno 2] No such file or directory: '.../tmp/seg_000004'
```

导致 `no_disk=False` 的常规磁盘下载**全部失败**(`downloads/` 下大量空目录即为证据),用户只能改用 no_disk/上传模式。

### 2.3 [连带] `retries=0` 时零次尝试

**位置**: `m3u8_downloader.py` — `_download_segment_task()` / `_download_and_feed()`

```python
for attempt in range(self.config.retries):   # retries=0 → range(0) 为空循环
```

`retries=0` 的语义应为"不重试,只尝试一次",但实际是一次都不尝试,全部分片报 `unknown error`。

### 2.4 [连带] 取消在合并/收尾阶段被忽略

**位置**: `api_server.py` — `CancelHook` / `TaskManager._run()`

- `CancelHook` 只覆盖了 `on_playlist_parsed / on_segment / on_segment_data`,未覆盖 `on_all_downloaded`(合并前),取消落在分片全下载完但未合并的窗口时,合并照常跑完
- `_run()` 在 `downloader.run()` 正常返回后无条件 `set_status(DONE)`,会覆盖 `cancel()` 已设置的 `CANCELLED` 状态 → **已取消的任务显示为成功**

### 2.5 [连带] m3u8 输入为超大文件(如视频直链)时整体读入内存

**位置**: `m3u8_downloader.py` — `_fetch_playlist()` / `HTTPClient.get()`

日志中曾出现将 `.mp4` 直链当作 m3u8 输入的情况:`http.get_text()` 会 `resp.read()` **整体读入**该文件(数百 MB ~ 1GB+)后才解析,取消无法中断读取 → 内存压力 → 被系统 kill。

---

## 三、修复内容

### `m3u8_downloader.py`

1. **`_download_segments()` / `_download_segments_no_disk()`**: 弃用 `with ThreadPoolExecutor` 隐式 `shutdown(wait=True)`,改为手动管理,在 `finally` 中 `fut.cancel()` 取消所有未执行的分片 + `executor.shutdown(wait=False)` 不等待排干。取消/异常时立即放弃剩余分片,在途的至多 `workers` 个分片跑完当前请求后自然退出(在 hook 处抛 `_Cancelled` 立即结束)。
2. **重试循环**: `attempts = max(1, self.config.retries)`,修复 `retries=0` 零尝试的问题(保持原有 `retries=N → N 次尝试` 语义不变)。
3. **`HTTPClient.get()/get_text()`**: 新增可选参数 `max_bytes`,读取上限 +1 字节,超限立即抛 `M3U8DownloadError`。`_fetch_playlist()` 与 master playlist 拉取传入 `MAX_PLAYLIST_BYTES = 20MB`,防止超大文件被整体读入内存。

### `api_server.py`

4. **`TaskManager._run()`**: 显式 `os.makedirs(tmp_dir, exist_ok=True)`,修复磁盘模式全失败。
5. **`TaskManager._run()`**: `downloader.run()` 返回后检查 `task.cancel_event.is_set()`,已取消任务标记 `CANCELLED` 而非 `DONE`,避免覆盖取消状态。
6. **`CancelHook`**: 新增 `on_all_downloaded()` 覆盖,合并前取消同样生效。

---

## 四、验证结果

| 验证项 | 结果 |
|---|---|
| 取消 300 分片任务 (磁盘) | 停止耗时 40.45s → **2.37s** |
| 取消 150 加密分片任务 (no_disk) | 停止耗时 80.32s → **6.99s** |
| 取消 200 分片任务 (端到端 API) | 取消后 **1.28s** 进入终态 cancelled |
| 磁盘模式下载 | 20/20 分片落盘并成功合并 (修复前全失败) |
| `retries=0` | 正常执行 1 次尝试 |
| 25MB 伪 m3u8 输入 | 0.2s 内快速报错 `响应体超过上限 20971520 字节` |
| CLI 回归 (合并) | 输出文件大小精确匹配 |

---

## 五、改动清单

```
m3u8_downloader.py  (+81 / -20)
  - _download_segments / _download_segments_no_disk: 取消/异常时不再等待线程池排干
  - _download_segment_task / _download_and_feed: retries=0 修复
  - HTTPClient.get / get_text: max_bytes 上限
  - _fetch_playlist / _resolve_master_playlist: m3u8 拉取 20MB 上限
  - 新增 MAX_PLAYLIST_BYTES 常量

api_server.py  (+15 / -0)
  - TaskManager._run: 创建 tmp_dir; 取消后不标记 DONE
  - CancelHook: 新增 on_all_downloaded 检查
```

---

## 六、备注

- 取消语义仍为"尽力而为":已在网络请求中的分片(至多 `workers` 个)会跑完当前请求后停止,符合模块 docstring 中既有的描述
- 相关历史:本次修复与 `memory-leak-analysis-2026-08-05.md`(future 引用释放)同属任务生命周期管理问题,建议回归时一起验证
