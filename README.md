# M3U8 Video Downloader

把 HLS 直播/点播的 m3u8 流下载成本地可播放文件：解析播放列表 → 并发拉取分片 → AES-128 解密 → 合并。
带一个 FastAPI 服务 + 单页 Web UI，支持多任务队列、实时进度、批量导入，以及可选的云盘边下边传。

核心下载器只依赖 Python 标准库；Web 服务、上传等能力按需安装额外依赖。

---

## 特性

- **单文件核心**：`m3u8_downloader.py` 即可完成下载/解密/合并，无第三方依赖
- **AES-128 解密**：支持 `EXT-X-KEY`（显式 `IV` 或按 `EXT-X-MEDIA-SEQUENCE` 推导）、`METHOD=NONE`
- **master playlist**：自动选最高清晰度，或用 `--resolution` 设清晰度上限
- **分段下载**：`--segments` 只取部分分片（单个位置 / 区间 / 到末尾），用于分批补漏
- **多种落盘模式**：合并为单文件 / 保留分片目录 / 完全不落盘
- **上传钩子**：可边下边传到云盘；不落盘模式下数据不经过磁盘
- **Web 服务**：任务队列、NDJSON 实时进度、取消、批量导入导出、失败原因可见
- **可扩展**：Hook 生命周期接口 + 上传后端工厂，新增后端不改核心流程

---

## 环境要求

- Python **3.9+**
- 无必需第三方依赖。可选：

| 依赖 | 作用 |
|---|---|
| `tqdm` | 进度条（缺省时退化为文本进度） |
| `pycryptodome` | AES-128 解密（加密流必需） |
| `fastapi` / `uvicorn` / `pydantic` | Web 服务 |
| `httpx` | 云盘上传 |
| `python-dotenv` | 读取 `.env`（缺失时有内置兜底解析） |

```bash
pip install -r requirements.txt
```

---

## 命令行用法

```bash
# 最简：直接给 m3u8 的 URL 或本地文件
python3 m3u8_downloader.py https://example.com/video.m3u8 -o my_video.ts

# 需要 Referer / UA 的站点
python3 m3u8_downloader.py https://example.com/master.m3u8 -o out.ts \
  -H "Referer: https://example.com/" -H "User-Agent: Mozilla/5.0"

# 限清晰度上限，只要 720p 及以下
python3 m3u8_downloader.py https://example.com/master.m3u8 -o out.ts --resolution 720p

# 只下第 100~199 个分片（0 起始），不合并、保留分片目录
python3 m3u8_downloader.py https://example.com/video.m3u8 -o out.ts \
  --segments 100-199 --no-merge
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `m3u8_input` | m3u8 URL 或本地文件路径（必填） |
| `-o, --output` | 输出文件名，默认 `output.ts` |
| `-w, --workers` | 并发下载线程数，默认 `10` |
| `-r, --retries` | 单分片失败重试次数，默认 `3` |
| `-t, --timeout` | HTTP 超时秒数，默认 `5` |
| `-H, --header` | 自定义请求头 `Key:Value`，可重复 |
| `-p, --proxy` | 代理地址，如 `http://127.0.0.1:7890` |
| `--base_url` | 分片为相对路径且无法从 m3u8 URL 推导时使用 |
| `--segments` | 只下载指定分片：`0,3,5` / `10-20` / `1600-`；空=全部 |
| `--resolution` | 清晰度上限：`480p`（仅高度）或 `854x480`（宽高都要满足） |
| `--no-merge` | 不合并，保留分片目录并向其中拷贝 m3u8 |
| `--tmp-dir` / `--keep-temp` | 指定临时目录 / 保留临时目录 |
| `--no-disk` | 不落磁盘，数据直接交给上传钩子（需配合 `--upload`） |
| `-u, --upload` | 上传目标远程目录，如 `/apps/videos/my_video/` |
| `--upload-token` | 上传凭证；不传则回退 `.env` 的 `ACCESS_TOKEN` |

几点行为约定：

- **`--resolution` 是上限而非偏好**：没有变体满足上限时**直接报错**并列出可用清晰度，不会悄悄降级。
- **`--segments` 不改变分片编号**：文件名、合并顺序仍用原始序号，因此可以分批下载同一份 playlist 的不同区间。
- **上传时 `playlist.m3u8` 始终是完整清单**：分批下载不会让远端清单只覆盖本批分片。
- **凭证优先级**：命令行/请求里传入的 token **永远优先**，`.env` 的 `ACCESS_TOKEN` 只是兜底。

---

## Web 服务

```bash
cp .env.example .env          # 按需修改端口 / 并发 / 上传兜底 token
./scripts/start.sh            # 后台启动（自动创建 .venv 并同步依赖）
./scripts/start.sh -f # 前台运行，Ctrl-C 停止
# 打开 http://localhost:8000/ （端口见 .env 的 PORT）
```

其他脚本：`./scripts/stop.sh`、`./scripts/restart.sh`、`./scripts/status.sh`，日志在 `run/server.log`。
生产部署可用 `sudo ./scripts/install-systemd.sh`（含 systemd unit 与 logrotate 配置）。

### HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/download` | 创建单个任务，返回 `{task_id}`；参数非法或"必然失败"前置条件不满足时返回 400 |
| `POST` | `/api/download/batch` | 批量创建，返回 `{created, skipped, errors}`；逐行校验，坏行只进 `errors` |
| `GET` | `/api/tasks` | 全部任务快照 |
| `GET` | `/api/tasks/{id}` | 单个任务详情 |
| `GET` | `/api/tasks/{id}/stream` | NDJSON 事件流（先回放历史，再推实时事件） |
| `POST` | `/api/tasks/{id}/cancel` | 请求取消（尽力而为） |
| `POST` | `/api/tasks/cancel-pending` | 一次性取消全部排队中的任务 |
| `DELETE` | `/api/tasks/{id}` | 移除已结束任务 |
| `GET` | `/api/health` | 健康检查 + 最大并发数 |
| `GET` | `/` | Web UI |

```bash
curl -X POST http://localhost:8000/api/download \
  -H 'Content-Type: application/json' \
  -d '{"m3u8_input":"https://example.com/video.m3u8","output":"demo.ts","workers":8}'

curl -N http://localhost:8000/api/tasks/<task_id>/stream   # 跟进度
```

请求体字段与 CLI 参数基本一一对应（`m3u8_input` / `output` / `workers` / `retries` / `timeout` /
`proxy` / `headers` / `segments` / `resolution` / `no_merge` / `no_disk` / `upload`）。

### 配置项（`.env`）

优先级：**环境变量 > `.env` > 代码默认值**。模板见 `.env.example`。

| 键 | 默认 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 监听地址 |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | 同时运行的任务数，超出的排队 |
| `BATCH_LIMIT` | `500` | 单次批量导入上限 |
| `MAX_REGISTRY` | `2000` | 任务注册表上限（超出驱逐最旧的已结束任务） |
| `TASK_TTL_SECONDS` | `1800` | 已结束任务保留时长 |
| `ACCESS_TOKEN` | — | 上传 token **兜底值**，请求里带 token 时不使用 |

> ⚠️ **安全提示**：服务本身**没有鉴权**，且 `/api/download` 可请求任意 http(s) 地址。
> 只应在本地或可信内网使用，**不要把端口裸暴露到公网**；确有需要请自行在前面加反向代理 + 访问控制。
> 任务注册表在内存中，重启即丢失（`downloads/` 下的文件会保留）。

---

## 二次开发

### 整体结构

```
m3u8_downloader.py    核心：解析 / 下载 / 解密 / 合并 + Hook 框架（零必需依赖）
pan_upload.py         云盘上传实现（httpx），可独立使用
playlist_convert.py   m3u8 清单格式转换小工具
api_server.py         FastAPI 封装：任务注册表、队列、事件流
web/                  单页前端（原生 ES Module，无构建步骤）
scripts/              启停脚本、systemd 安装脚本与模板
tests/                单元测试（标准库 unittest）
```

核心流水线（`M3U8Downloader.run()`）：

```
获取 m3u8 → 解析 → [master playlist? 选变体] → 解析 base_url → 加载密钥
         → 并发下载分片（下载即解密）→ [合并] → 结束
```

各阶段之间靠 **Hook** 连接，`api_server.py` 正是以"调用方 + Hook"的方式复用核心，没有改动下载流程。

### 扩展点一：自定义 Hook（最常见的二次开发方式）

继承 `BaseHook`，按需覆盖回调，在构造 `M3U8Downloader` 时通过 `hooks=[...]` 传入：

| 回调 | 时机 | 线程 |
|---|---|---|
| `on_playlist_parsed` | 播放列表解析完成 | 主线程 |
| `on_segment` | 单个分片下载并解密到文件 | worker |
| `on_segment_data` | 不落盘模式：直接拿到分片字节 | worker |
| `on_all_downloaded` | 全部下载完成 | 主线程 |
| `on_finish` | 整个流程结束 | 主线程 |

回调收到 `HookContext`（`ctx`），可读可写，关键字段：
`playlist` / `full_playlist`（过滤前的完整清单）、`segment` / `segment_index` / `filepath` /
`data` / `filename`、`tmp_dir`、`result_path`、`should_continue`。

写回 `ctx` 可把数据传给下游 Hook；把 `ctx.should_continue` 设为 `False` 可截断后续 Hook 链。

```python
from m3u8_downloader import BaseHook, DownloadConfig, M3U8Downloader

class NotifyHook(BaseHook):
    def on_all_downloaded(self, ctx):
        print("done:", [s.index for s in ctx.playlist.segments][:3], "...")

M3U8Downloader(DownloadConfig(m3u8_source=url, output="a.ts"),
               hooks=[NotifyHook()]).run()
```

> 注意：`hooks` 显式传入时**默认合并 Hook 不会自动注册**。需要合并就自己把
> `SegmentMergeHook` 一并放进列表。

**中断长时间下载**：在 Hook 里抛一个继承 `BaseException` 的异常（而非 `Exception`），
就能绕过分片级的重试逻辑直接终止 —— `api_server.py` 的 `CancelHook` 就是这么做的。

### 扩展点二：新增上传后端

1. 在 `m3u8_downloader.py` 中继承 `UploadHook`，实现 `on_segment` / `on_segment_data`；
   需要写清单就再实现 `on_playlist_parsed`（记得用 `ctx.full_playlist`）。
2. 在 `create_upload_hook()` 工厂里注册新的 `hook_type`。
3. 若需要"下载前预检凭证"，在 `upload_preflight()` 里加对应分支，返回配额 dict 或抛异常。

之后 CLI 的 `--upload` 与 API 的 `upload.type` 会自动支持新后端，核心流程无需改动。
接口契约（`upload_file` / `upload_bytes` 的入参与返回）可参考 `pan_upload.py`。

### 扩展点三：Web 层

- **新增请求字段**：`api_server.py` 的 `DownloadRequest` 增加字段 → `_run()` 里接到 `DownloadConfig`
  → 前端 `web/js/pages/tasksPage.js` 的 `collectRequest()` 增加同名字段。前端无构建步骤，改完刷新页面即可。
- **新增接口**：直接在 `api_server.py` 加路由即可；任务状态机、事件广播都在 `Task` / `TaskManager` 中。
- **前端结构**：`web/js/api/`（HTTP 与事件流）、`web/js/components/`（任务卡片、NDJSON 流）、
  `web/js/pages/`（页面逻辑）、`web/js/utils/`（DOM、localStorage、校验）。

### 编辑时必须同步的"共享契约"

有几处校验逻辑被多层重复实现，**改一处必须同时改全部**，否则前端放行的输入会被后端拒绝（或反之）。
每处对应的 `tests/` 表即是契约本体：

| 契约 | 涉及位置 | 测试 |
|---|---|---|
| m3u8 来源合法性 | `api_server.validate_m3u8_source()` / `web/js/utils/validate.js` | `test_m3u8_source_validation.py` |
| 清晰度上限格式 | `m3u8_downloader.parse_resolution_limit()` / `api_server.validate_resolution_limit()` / `validate.js` | `test_resolution_limit.py` |
| 上传前置条件与 token 优先级 | `api_server.validate_task_upload()` / `_run()` / `m3u8_downloader` CLI 提示 | `test_upload_token_priority.py` |

### 测试

```bash
.venv/bin/python -m unittest                            # 全部
.venv/bin/python -m unittest -v tests.test_segment_filter
```

测试不触网、不写 `downloads/`（用假对象替换 `TaskManager`、把 `DOWNLOAD_ROOT` 指向临时目录）。
新增行为时优先往对应的表驱动用例里加一行。

---

## 已知限制

- 取消是**尽力而为**：已在传输中的分片可能先跑完，取消点只在分片回调处
- `--resolution` 只对 master playlist 生效；单条码率的清单没有变体可选，会被忽略
- Web 服务无鉴权、任务注册表在内存中（见上文安全提示）
- 不处理 DRM（Widevine 等），只支持标准 HLS 的 AES-128 / 无加密

## License

[MIT](LICENSE) © 2026 iamshijun

可自由使用、修改、分发、商用，保留版权与许可声明即可。

> 本项目只提供下载工具，不对使用者获取的内容及所在地区的合规性负责。请遵守目标站点的服务条款与当地法律。
