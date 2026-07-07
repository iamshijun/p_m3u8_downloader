# M3U8 视频下载 & 合并工具 — 项目总结

## 项目概述

一个 Python 命令行工具，输入 M3U8 文件（本地路径或 URL），自动下载所有视频切片片段，支持 AES-128 解密，最终合并为单个文件或保留切片目录。

- **文件**: `m3u8_downloader.py`
- **零外部依赖可运行**（仅使用标准库 `urllib.request`）
- **可选依赖**: `tqdm`（进度条）、`pycryptodome`（AES 解密）
- **Python 版本**: 3.8+

---

## 架构设计（面向对象）

```
dataclasses:
  KeyInfo        — AES-128 密钥信息 (method, key_uri, key_data, iv)
  M3U8Segment    — 单个片段 (uri, duration, key)
  M3U8Playlist   — 解析后的播放列表 (segments, version, media_sequence, ...)

classes:
  HTTPClient     — HTTP 客户端 (封装代理/Header/超时, get/get_text)
  M3U8Downloader — 主下载器 (编排全流程: 获取→解析→下载密钥→下载→解密→合并)

cli:
  main()         — argparse 参数解析, 实例化并运行
```

### 执行流程

```
main()
  └─> M3U8Downloader.run()
        ├─ _fetch_playlist()    →  下载/读取 m3u8 → M3U8Playlist
        ├─ _resolve_base_url()  →  确定 base_url
        ├─ _load_keys()         →  下载 AES 密钥, 计算 IV
        ├─ _download_segments() →  并发下载 + 解密 → 临时文件列表
        ├─ _merge()             →  二进制合并为 output.ts  [--no-merge 则跳过]
        └─ _cleanup()           →  清理临时文件
```

---

## CLI 参数

| 参数 | 简写 | 默认值 | 说明 |
|------|------|--------|------|
| `m3u8_input` | — | (必需) | m3u8 文件路径或 HTTP(S) URL |
| `--base_url` | — | None | 切片根 URL，相对路径时需要；若 m3u8 本身是 URL 则自动推导 |
| `--output` | `-o` | `output.ts` | 输出文件名；`--no-merge` 时作为目录名前缀 |
| `--workers` | `-w` | `10` | 并发下载线程数 |
| `--retries` | `-r` | `3` | 下载失败重试次数 |
| `--timeout` | `-t` | `5` | HTTP 请求超时秒数 |
| `--proxy` | `-p` | None | 代理地址，如 `http://127.0.0.1:7890` |
| `--header` | `-H` | — | 自定义请求头，可多次指定，格式 `Key:Value` |
| `--no-merge` | — | False | 不合并切片，保留下载目录和 m3u8 文件 |
| `--keep-temp` | — | False | 保留临时目录不删除 |

---

## 使用示例

```bash
# 本地 m3u8 + 指定根 URL
python3 m3u8_downloader.py video.m3u8 --base_url https://example.com/video/ -o merged.ts

# m3u8 URL（自动推导 base_url）
python3 m3u8_downloader.py https://example.com/playlist.m3u8 -o output.ts

# 代理 + 自定义请求头 + 高并发
python3 m3u8_downloader.py video.m3u8 --base_url https://example.com/video/ \
  -p http://127.0.0.1:7890 \
  -H 'Referer:https://example.com' \
  -H 'Cookie:session=abc123' \
  -w 20 -t 10 -r 5

# 仅下载不合并（切片 + m3u8 存放在输出目录）
python3 m3u8_downloader.py https://example.com/playlist.m3u8 --no-merge -o my_video
# → 输出到 my_video_segments/ 目录
```

---

## 加密支持 (AES-128)

自动识别 M3U8 中的 `#EXT-X-KEY:METHOD=AES-128` 标签：

1. **下载密钥** — 从 `URI` 指向的地址下载 16 字节 AES-128 密钥
2. **计算 IV** — 有显式 `IV=0x...` 就使用；否则按 HLS 规范用 `MEDIA_SEQUENCE + 序号` 作为 128-bit big-endian
3. **解密** — AES-128-CBC 解密后去除 PKCS7 填充
4. **同一密钥缓存** — 多次出现的相同 key URI 只下载一次
5. **`METHOD=NONE`** — 支持后续片段切回明文

需要安装 `pycryptodome`：

```bash
pip install pycryptodome
```

---

## 安装

```bash
# 可选：进度条
pip install tqdm
# 可选：加密 m3u8 支持
pip install pycryptodome
```

---

## M3U8 文件示例

本项目的 `video.m3u8` 是一个典型示例：

```m3u8
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXTINF:4.000000,
video0.jpeg
#EXTINF:4.000000,
video1.jpeg
...
#EXTINF:3.733333,
video1779.jpeg
#EXT-X-ENDLIST
```

共 1780 个片段，每段约 4 秒，最后一个片段 3.73 秒。

加密 M3U8 示例：

```m3u8
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0xd9f1c97e8ec6c96376a7b5039a302e0f
#EXTINF:4.0,
segment0.ts
#EXT-X-KEY:METHOD=NONE
#EXTINF:4.0,
segment1.ts
```

---

## 开发历程

功能按顺序迭代添加：

1. 基础 M3U8 解析 + 下载 + 二进制合并
2. `--proxy` 代理支持
3. `--header` / `-H` 自定义请求头（可多次指定）
4. `tqdm` 进度条（检测到则启用，否则回退文本进度）
5. `m3u8_input` 支持 URL，`base_url` 改为可选
6. `--timeout` 超时 + `--retries` 重试次数
7. AES-128-CBC 加密解密支持（`#EXT-X-KEY`）
8. 面向对象重构：引入 `KeyInfo`/`M3U8Segment`/`M3U8Playlist` 数据类，`HTTPClient` + `M3U8Downloader` 类
9. `--no-merge` 不合并模式
