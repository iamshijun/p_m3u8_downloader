"""
M3U8 视频下载 & 合并工具 (支持 AES-128 解密 + Hook 插件机制)

用法:
    python m3u8_downloader.py <m3u8文件路径或URL> [选项]

示例:
    # 本地 m3u8 文件, 下载并合并
    python m3u8_downloader.py video.m3u8 --base-url https://example.com/video/ -o merged.ts
    # m3u8 URL, 仅下载不合并
    python m3u8_downloader.py https://example.com/playlist.m3u8 --no-merge -o my_video
    # 下载并上传到百度云盘
    python m3u8_downloader.py video.m3u8 --base-url https://example.com/video/ --no-disk \\
        --upload /apps/movies/my_video/ --upload-token YOUR_TOKEN
"""

import argparse
import os
import re
import shutil
import struct
import sys
import tempfile
import time
from abc import ABC
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse ,parse_qs, urlencode, parse_qsl,urlunparse

import urllib.request


# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------

class EncryptionMethod(Enum):
    """加密方法枚举"""
    NONE = "NONE"
    AES_128 = "AES-128"


class ProgressMode(Enum):
    """进度显示模式"""
    TQDM = "tqdm"
    SIMPLE = "simple"


# ---------------------------------------------------------------------------
# 类型定义
# ---------------------------------------------------------------------------

HeadersDict = dict[str, str]
URL = str
FilePath = Path | str


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def has_tqdm() -> bool:
    """检查是否安装了 tqdm"""
    try:
        from tqdm import tqdm
        return True
    except ImportError:
        return False


def has_crypto() -> bool:
    """检查是否安装了 pycryptodome"""
    try:
        from Crypto.Cipher import AES
        return True
    except ImportError:
        return False


def parse_headers(raw_headers: Optional[list[str]]) -> HeadersDict:
    """将命令行 -H 参数解析为 headers 字典"""
    if not raw_headers:
        return {}

    headers: HeadersDict = {}
    for header in raw_headers:
        if ":" not in header:
            print(f"[!] 忽略无效的 Header 格式 (缺少冒号): {header}")
            continue

        key, _, value = header.partition(":")
        headers[key.strip()] = value.strip()

    return headers


def load_token(token_arg: Optional[str]) -> Optional[str]:
    """获取 ACCESS_TOKEN: 命令行 > .env"""
    if token_arg:
        return token_arg

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    return os.getenv("ACCESS_TOKEN")


def is_url(m3u8_source: str) -> bool:
    """检查是否为 URL"""
    return urlparse(m3u8_source).scheme in ("http", "https")


# ---------------------------------------------------------------------------
# 进度管理器
# ---------------------------------------------------------------------------


class ProgressBar:
    """进度条管理器：统一封装 tqdm 和 print 的逻辑"""

    def __init__(self, total: Optional[int] = None, desc: str = "", unit: str = "item"):
        self.total = total
        self.desc = desc
        self.unit = unit
        self.pbar = None
        self.progress_mode = ProgressMode.TQDM if has_tqdm() and total else ProgressMode.SIMPLE

    def __enter__(self):
        """支持 with 语法"""
        if self.progress_mode == ProgressMode.TQDM:
            from tqdm import tqdm
            self.pbar = tqdm(total=self.total, desc=self.desc, unit=self.unit)
        return self

    def __exit__(self, *args):
        """退出 with 语法，自动关闭进度条"""
        self.close()

    def update(self, n: int = 1) -> bool:
        """更新进度，返回是否成功更新了进度条"""
        if self.progress_mode == ProgressMode.TQDM and self.pbar:
            self.pbar.update(n)
            return True
        return False

    def write(self, msg: str) -> None:
        """输出消息（支持tqdm的环境）"""
        if self.progress_mode == ProgressMode.TQDM and self.pbar:
            from tqdm import tqdm
            tqdm.write(msg)
        else:
            print(msg)

    def close(self) -> None:
        """关闭进度条"""
        if self.progress_mode == ProgressMode.TQDM and self.pbar:
            self.pbar.close()

    @staticmethod
    def simple_progress(current: int, total: int, desc: str = "") -> str:
        """生成简单的文本进度信息"""
        return f"{desc}: {current}/{total}"


# ---------------------------------------------------------------------------
# 域对象
# ---------------------------------------------------------------------------


@dataclass
class KeyInfo:
    """AES-128 加密密钥信息"""

    method: EncryptionMethod
    key_uri: str = ""
    key_data: bytes = b""
    iv: Optional[bytes] = None
    key_format: Optional[str] = None


@dataclass
class M3U8Segment:
    """单个视频片段"""

    uri: str
    duration: float = 0.0
    key: Optional[KeyInfo] = None


@dataclass
class M3U8Playlist:
    """解析后的 M3U8 播放列表"""

    segments: list[M3U8Segment] = field(default_factory=list)
    media_sequence: int = 0
    target_duration: float = 0.0
    version: int = 3

    @property
    def is_encrypted(self) -> bool:
        return any(seg.key is not None for seg in self.segments)

    def __len__(self) -> int:
        return len(self.segments)


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------


class HTTPClient:
    """封装代理、自定义 Header、超时的 HTTP 客户端"""

    def __init__(
        self,
        headers: Optional[HeadersDict] = None,
        proxy: Optional[str] = None,
        timeout: int = 5,
    ):
        self.headers = headers or {}
        self.proxy = proxy
        self.timeout = timeout
        self._opener: Optional[urllib.request.OpenerDirector] = None

        if proxy:
            proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            self._opener = urllib.request.build_opener(proxy_handler)
            urllib.request.install_opener(self._opener)
            print(f"[*] 使用代理: {proxy}")

        if self.headers:
            print(f"[*] 自定义请求头: {self.headers}")

    def get(self, url: URL) -> bytes:
        """GET 请求，返回响应体字节"""
        if self._opener or self.headers:
            req = urllib.request.Request(url, headers=self.headers)
            with (self._opener or urllib.request.build_opener()).open(req, timeout=self.timeout) as resp:
                return resp.read()
        else:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return resp.read()

    def get_text(self, url: URL) -> str:
        """GET 请求，返回响应体文本"""
        return self.get(url).decode("utf-8")


# ---------------------------------------------------------------------------
# Hook 系统
# ---------------------------------------------------------------------------


class BaseHook(ABC):
    """Hook 基类 — 生命周期钩子/回调接口方法，子类按需覆盖"""

    def on_segment(self, index: int, segment: M3U8Segment, filepath: str) -> None:
        """单个片段下载(落地到文件)+解密成功时调用 (在 worker 线程中调用)"""

    def on_segment_data(self, index: int, segment: M3U8Segment, data: bytes, filename: str) -> None:
        """不落地模式: 直接接收字节数据 (在 worker 线程中调用)"""

    def on_all_downloaded(self, playlist: M3U8Playlist, tmp_dir: str) -> None:
        """所有片段下载完成后调用 (在主线程中调用)"""

    def on_finish(self, result_path: str) -> None:
        """整个流程结束后调用"""


@dataclass
class MergeConfig:
    """合并配置"""
    output: str
    merge: bool = True
    keep_temp: bool = False
    tmp_dir: Optional[str] = None


class SegmentMergeHook(BaseHook):
    """默认合并钩子: 将下载的分片按顺序合并为单个文件，或保留为目录"""

    def __init__(self, config: MergeConfig):
        self.config = config
        self._m3u8_file: Optional[str] = None

    def on_all_downloaded(self, playlist: M3U8Playlist, tmp_dir: str) -> None:
        if self.config.merge:
            self._do_merge(tmp_dir, len(playlist))
        else:
            self._keep_segments(tmp_dir)

    def _do_merge(self, tmp_dir: str, total: int) -> None:
        """按顺序合并片段"""
        print(f"[*] 合并片段到: {self.config.output}")

        with ProgressBar(total=total, desc="合并进度", unit="seg") as pbar:
            output_path = Path(self.config.output)
            with output_path.open("wb") as outfile:
                for i in range(total):
                    segment_path = Path(tmp_dir) / f"seg_{i:06d}"
                    if segment_path.exists():
                        with segment_path.open("rb") as infile:
                            shutil.copyfileobj(infile, outfile)
                    pbar.update(1)

    def _keep_segments(self, tmp_dir: str) -> None:
        """保留分片: 将临时目录重命名并拷贝 m3u8 文件"""
        if self._m3u8_file and Path(self._m3u8_file).is_file():
            m3u8_dest = Path(tmp_dir) / "playlist.m3u8"
            m3u8_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self._m3u8_file, m3u8_dest)

        if self.config.keep_temp:
            print(f"[*] 已使用指定临时目录: {tmp_dir}")
            return

        output_path = Path(self.config.output)
        segments_dir = output_path.with_suffix("") / f"{output_path.stem}_segments"

        if segments_dir.exists():
            shutil.rmtree(segments_dir, ignore_errors=True)

        print(f'[*] 移动临时目录到: {segments_dir}')
        shutil.move(tmp_dir, str(segments_dir))

    def on_finish(self, result_path: str) -> None:
        if not self.config.keep_temp and self.config.merge:
            pass  # 由外部处理


class YunpanUploadHook(BaseHook):
    """上传钩子: 每下载完一个分片就上传到百度云盘"""

    def __init__(
        self,
        token: str,
        remote_prefix: str,
        ondup: str = "overwrite",
        max_retries: int = 3,
        delete_after_upload: bool = True,
    ):
        self.token = token
        self.remote_prefix = remote_prefix.rstrip("/")
        self.ondup = ondup
        self.max_retries = max_retries
        self.delete_after_upload = delete_after_upload
        self.upload_file = None
        self.upload_bytes = None

        try:
            from pan_upload import upload_file, upload_bytes
            self.upload_file = upload_file
            self.upload_bytes = upload_bytes
        except ImportError:
            print(f"[!] 无法导入 yunpan_hls")

    def on_segment(self, index: int, segment: M3U8Segment, filepath: str) -> None:
        if not self.upload_file:
            return

        remote_path = f"{self.remote_prefix}/{os.path.basename(filepath)}"
        self._do_upload(filepath, remote_path)

    def on_segment_data(self, index: int, segment: M3U8Segment, data: bytes, filename: str) -> None:
        if not self.upload_bytes:
            return

        remote_path = f"{self.remote_prefix}/seg_{index:06d}"
        self._do_upload_bytes(data, filename, remote_path)

    def _do_upload(self, local_path: str, remote_path: str) -> None:
        try:
            result = self.upload_file(
                local_path=local_path,
                remote_path=remote_path,
                token=self.token,
                ondup=self.ondup,
                max_retries=self.max_retries,
                delete_after_upload=self.delete_after_upload,
            )
            print(f"[*] 上传成功: {remote_path} (fs_id={result.get('fs_id', '?')})")
        except Exception as e:
            print(f"[!] 上传失败: {remote_path} — {e}")

    def _do_upload_bytes(self, data: bytes, filename: str, remote_path: str) -> None:
        try:
            self.upload_bytes(
                data=data,
                filename=filename,
                remote_path=remote_path,
                token=self.token,
                ondup=self.ondup,
                max_retries=self.max_retries,
            )
        except Exception as e:
            print(f"[!] 上传失败: {remote_path} — {e}")


# ---------------------------------------------------------------------------
# M3U8 解析器
# ---------------------------------------------------------------------------


class M3U8Parser:
    """M3U8 解析器"""

    _EXT_KEY_RE = re.compile(
        r"#EXT-X-KEY\s*:\s*"
        r"(?:.*?METHOD\s*=\s*([^,\s]+))?"
        r'(?:.*?URI\s*=\s*"([^"]*)")?'
        r"(?:.*?IV\s*=\s*(0x[0-9a-fA-F]+))?"
        r"(?:.*?KEYFORMAT\s*=\s*\"([^\"]+)\")?",
        re.IGNORECASE,
    )

    @classmethod
    def parse(cls, content: str) -> M3U8Playlist:
        """解析 m3u8 文本内容"""
        playlist = M3U8Playlist()
        current_key: Optional[KeyInfo] = None
        current_duration = 0.0

        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue

            if line.startswith("#EXT-X-VERSION:"):
                playlist.version = int(line.split(":")[1])
            elif line.startswith("#EXT-X-TARGETDURATION:"):
                playlist.target_duration = float(line.split(":")[1])
            elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                playlist.media_sequence = int(line.split(":")[1])
            elif line.startswith("#EXTINF:"):
                current_duration = float(line.split(":")[1].rstrip(","))
            elif line.startswith("#EXT-X-KEY"):
                current_key = cls._parse_key_line(line)
            elif not line.startswith("#"):
                seg = M3U8Segment(uri=line, duration=current_duration, key=current_key)
                playlist.segments.append(seg)

        return playlist

    @classmethod
    def _parse_key_line(cls, line: str) -> Optional[KeyInfo]:
        """解析 #EXT-X-KEY 行"""
        m = cls._EXT_KEY_RE.match(line)
        if not m:
            return None

        method = (m.group(1) or "").upper()
        if method == "NONE":
            return None
        if method == "AES-128":
            iv_str = m.group(3)
            key_format = m.group(4) if m.group(4) else None
            return KeyInfo(
                method=EncryptionMethod.AES_128,
                key_uri=m.group(2) or "",
                iv=bytes.fromhex(iv_str[2:]) if iv_str else None,
                key_format=key_format,
            )

        print(f"[!] 不支持的加密方法: {method}, 按明文处理")
        return None


# ---------------------------------------------------------------------------
# 配置类
# ---------------------------------------------------------------------------


@dataclass
class DownloadConfig:
    """下载配置"""
    m3u8_source: str
    output: str = "output.ts"
    base_url: Optional[str] = None
    workers: int = 10
    retries: int = 3
    no_merge: bool = False
    keep_temp: bool = False
    tmp_dir: Optional[str] = None
    no_disk: bool = False
    key_param: Optional[str] = None
    key_format: Optional[str] = None


# ---------------------------------------------------------------------------
# M3U8 下载器
# ---------------------------------------------------------------------------


class M3U8Downloader:
    """M3U8 视频下载器: 获取 m3u8 → 解析 → 下载片段 → 解密 → [hooks]"""

    def __init__(self, config: DownloadConfig, http_client: HTTPClient, hooks: list[BaseHook]):
        self.config = config
        self.http = http_client
        self.hooks = hooks
        self.tmp_dir: Optional[str] = None
        self._tmp_m3u8_path: Optional[str] = None

    def run(self) -> str:
        """执行完整下载流程，返回输出文件/目录路径"""
        playlist = self._fetch_playlist()
        if not playlist.segments:
            print("[!] m3u8 文件中未找到任何视频片段")
            sys.exit(1)
        print(f"[*] 共发现 {len(playlist)} 个视频片段")

        self._resolve_base_url()
        self._load_keys(playlist)

        if playlist.is_encrypted:
            if not has_crypto():
                print("[!] 检测到加密片段，但未安装 pycryptodome. 请运行: pip install pycryptodome")
                sys.exit(1)
            print("[*] 检测到 AES-128 加密，密钥已加载")

        self._setup_tmp_dir()

        if self.config.no_disk:
            self._download_segments_no_disk(playlist)
            result = self.config.output
            for hook in self.hooks:
                hook.on_finish(result)
            self._cleanup()
            return result

        self._inject_m3u8_context()
        self._download_segments(playlist)

        for hook in self.hooks:
            hook.on_all_downloaded(playlist, self.tmp_dir)

        result_path = self._finalize()
        return result_path

    def _setup_tmp_dir(self) -> None:
        """设置临时目录"""
        if not self.config.tmp_dir:
            self.tmp_dir = tempfile.mkdtemp(prefix="m3u8_dl_")
        else:
            self.tmp_dir = self.config.tmp_dir
        print(f"[*] 临时目录: {self.tmp_dir}")

    def _fetch_playlist(self) -> M3U8Playlist:
        """获取 m3u8 内容并解析"""
        if is_url(self.config.m3u8_source):
            print(f"[*] 下载 m3u8 文件: {self.config.m3u8_source}")
            content = self.http.get_text(self.config.m3u8_source)
            self._tmp_m3u8_path = self._save_temp_m3u8(content)
            return M3U8Parser.parse(content)
        else:
            print(f"[*] 解析 m3u8 文件: {self.config.m3u8_source}")
            with open(self.config.m3u8_source, "r", encoding="utf-8") as f:
                return M3U8Parser.parse(f.read())

    def _save_temp_m3u8(self, content: str) -> str:
        """保存临时 m3u8 文件"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".m3u8", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            return tmp.name

    def _resolve_base_url(self) -> None:
        """确定 base_url"""
        if self.config.base_url:
            self.config.base_url = self.config.base_url.rstrip("/") + "/"
            print(f"[*] 根 URL (--base_url): {self.config.base_url}")
        elif self._tmp_m3u8_path:
            self.config.base_url = f"{Path(self.config.m3u8_source).parent}/"
            print(f"[*] 根 URL (自动): {self.config.base_url}")
        elif is_url(self.config.m3u8_source):
            self.config.base_url = f"{urlparse(self.config.m3u8_source).scheme}://{urlparse(self.config.m3u8_source).netloc}/"

    def _load_keys(self, playlist: M3U8Playlist) -> None:
        """下载加密密钥并计算每段的 IV"""
        key_cache: dict[str, bytes] = {}

        for i, seg in enumerate(playlist.segments):
            if seg.key is None:
                continue
            if seg.key.key_uri not in key_cache:
                key_url = self._resolve_url_with_token(seg.key.key_uri)
                print(f"[*] 下载密钥: {key_url}")
                key_cache[seg.key.key_uri] = self.http.get(key_url)
            seg.key.key_data = key_cache[seg.key.key_uri]

            if seg.key.iv is None:
                seq = playlist.media_sequence + i
                seg.key.iv = struct.pack(">Q", seq).rjust(16, b"\x00")

    def _resolve_url(self, uri: str) -> str:
        """将相对 URI 拼接为完整 URL"""
        if urlparse(uri).netloc:
            return uri
        if self.config.base_url is None:
            sys.exit(f"[!] 无法拼接相对路径 '{uri}', 未提供 base_url")
        return urljoin(self.config.base_url, uri)

    def _resolve_url_with_token(self, uri: str) -> str:
        """将相对 URI 拼接为完整 URL 并附加 token 参数"""
        if not self.config.key_param:
            return self._resolve_url(uri)

        url = self._resolve_url(uri)
        parsed = urlparse(url)
        query_params = dict(parse_qs(parsed.query)) if parsed.query else {}
        new_params = dict(parse_qsl(self.config.key_param))
        query_params.update(new_params)

        new_query = urlencode(query_params, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    def _inject_m3u8_context(self) -> None:
        """将 m3u8 源文件路径注入到 SegmentMergeHook"""
        m3u8_file = self._tmp_m3u8_path or (
            self.config.m3u8_source if os.path.isfile(self.config.m3u8_source) else None
        )
        if m3u8_file is None:
            return
        for hook in self.hooks:
            if isinstance(hook, SegmentMergeHook):
                hook._m3u8_file = m3u8_file

    def _download_segments(self, playlist: M3U8Playlist) -> list[str]:
        """并发下载所有片段"""
        urls = [self._resolve_url(seg.uri) for seg in playlist.segments]
        print(f"[*] 开始下载 (并发数: {self.config.workers})...")

        paths: list[Optional[str]] = [None] * len(playlist)
        success = 0
        fail = 0

        with ProgressBar(total=len(playlist), desc="下载", unit="seg") as pbar:
            with ThreadPoolExecutor(max_workers=self.config.workers) as executor:
                futures = {}
                for i, url in enumerate(urls):
                    dest = os.path.join(self.tmp_dir, f"seg_{i:06d}")
                    seg_key = playlist.segments[i].key
                    futures[executor.submit(
                        self._download_segment_task, url, dest, i, playlist.segments[i], seg_key
                    )] = i

                for future in as_completed(futures):
                    idx, filepath, status = future.result()
                    if status == "ok":
                        success += 1
                        paths[idx] = filepath
                    else:
                        fail += 1
                        pbar.write(f"  [!] 片段 {idx} ({playlist.segments[idx].uri}): {status}")

                    pbar.update(1)
                    if (success + fail) % 100 == 0 or (success + fail) == len(playlist):
                        pbar.write(f"  [*] {ProgressBar.simple_progress(success + fail, len(playlist), '进度')} (成功: {success}, 失败: {fail})")

        if fail > 0:
            print(f"[!] {fail} 个片段下载失败，合并结果可能不完整")

        return [p for p in paths if p is not None]

    def _download_segment_task(
        self,
        url: str,
        dest: str,
        index: int,
        segment: M3U8Segment,
        key_info: Optional[KeyInfo] = None,
    ) -> tuple[int, str, str]:
        """下载单个片段，失败自动重试"""
        for attempt in range(self.config.retries):
            try:
                raw = self.http.get(url)
                if key_info is not None:
                    raw = self._decrypt(raw, key_info)
                with open(dest, "wb") as f:
                    f.write(raw)

                for hook in self.hooks:
                    hook.on_segment(index, segment, dest)
                return (index, dest, "ok")
            except Exception as e:
                if attempt == self.config.retries - 1:
                    return (index, "", f"失败: {e}")
                time.sleep(1 * (attempt + 1))
        return (index, "", "unknown error")

    def _download_segments_no_disk(self, playlist: M3U8Playlist) -> None:
        """不落地下载：下载解密后直接 on_segment_data"""
        urls = [self._resolve_url(seg.uri) for seg in playlist.segments]
        print(f"[*] 开始不落地下载 (并发数: {self.config.workers})...")

        with ProgressBar(total=len(playlist), desc="下载&上传", unit="seg") as pbar:
            with ThreadPoolExecutor(max_workers=self.config.workers) as executor:
                futures = {}
                for i, url in enumerate(urls):
                    seg = playlist.segments[i]
                    futures[executor.submit(
                        self._download_and_feed, url, i, seg, seg.key
                    )] = i

                for future in as_completed(futures):
                    idx, status = future.result()
                    if status == "ok":
                        pass  # 成功不需要特殊处理
                    else:
                        pbar.write(f"  [!] 片段 {idx} ({playlist.segments[idx].uri}): {status}")
                    pbar.update(1)

        if any(status != "ok" for _, status in
               (future.result() for future in as_completed(futures))):
            print(f"[!] 部分片段失败")

    def _download_and_feed(
        self,
        url: str,
        index: int,
        segment: M3U8Segment,
        key_info: Optional[KeyInfo] = None,
    ) -> tuple[int, str]:
        """下载 + 解密 + on_segment_data（不写磁盘）"""
        for attempt in range(self.config.retries):
            try:
                raw = self.http.get(url)
                if key_info is not None:
                    raw = self._decrypt(raw, key_info)
                filename = os.path.basename(urlparse(url).path) or f"seg_{index:06d}"
                for hook in self.hooks:
                    hook.on_segment_data(index, segment, raw, filename)
                return (index, "ok")
            except Exception as e:
                if attempt == self.config.retries - 1:
                    return (index, f"失败: {e}")
                time.sleep(1 * (attempt + 1))
        return (index, "unknown error")

    def _decrypt(self, data: bytes, key_info: KeyInfo) -> bytes:
        """AES-128-CBC 解密并去除 PKCS7 填充"""
        from Crypto.Cipher import AES
        cipher = AES.new(key_info.key_data, AES.MODE_CBC, iv=key_info.iv or b"\x00" * 16)
        decrypted = cipher.decrypt(data)
        pad_len = decrypted[-1]
        if 1 <= pad_len <= 16:
            decrypted = decrypted[:-pad_len]
        return decrypted

    def _finalize(self) -> str:
        """收尾：调用 on_finish，清理临时文件，打印结果"""
        if self.config.no_merge:
            if self.config.keep_temp:
                result = self.tmp_dir
            else:
                name, _ = os.path.splitext(self.config.output)
                result = name + "_segments"
                print(f"[*] 完成! 切片目录: {result}")
        else:
            result = self.config.output
            if os.path.exists(result):
                total_size_mb = os.path.getsize(result) / (1024 * 1024)
                print(f"[*] 完成! 输出文件: {result} ({total_size_mb:.1f} MB)")

        for hook in self.hooks:
            hook.on_finish(result)

        self._cleanup()
        return result

    def _cleanup(self) -> None:
        """清理临时文件"""
        if self.config.keep_temp:
            print(f"[*] 临时文件保留在: {self.tmp_dir}")
            return

        print("[*] 清理临时文件...")
        if self.tmp_dir:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        if self._tmp_m3u8_path and os.path.exists(self._tmp_m3u8_path):
            os.unlink(self._tmp_m3u8_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_cli_args() -> argparse.Namespace:
    """构建命令行参数"""
    parser = argparse.ArgumentParser(description="M3U8 视频下载 & 合并工具")

    # 基本参数
    parser.add_argument("m3u8_input", help="m3u8 文件路径或 URL")
    parser.add_argument("--output", "-o", default="output.ts", help="输出文件名 (默认: output.ts)")
    parser.add_argument("--workers", "-w", type=int, default=10, help="并发下载线程数 (默认: 10)")
    parser.add_argument("--timeout", "-t", type=int, default=5, help="HTTP 请求超时秒数 (默认: 5)")
    parser.add_argument("--retries", "-r", type=int, default=3, help="下载失败重试次数 (默认: 3)")

    # 网络相关
    parser.add_argument("--base-url", default=None, help="视频片段的根 URL (片段为相对路径时需要)")
    parser.add_argument("--proxy", "-p", default=None, help="代理地址, 例如 http://127.0.0.1:7890")
    parser.add_argument("--header", "-H", action="append", default=None,
                        help="自定义请求头, 格式 Key:Value, 可多次指定")

    # 下载控制
    parser.add_argument("--tmp-dir", default=None, help="显式指定临时下载目录")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时下载目录不删除")
    parser.add_argument("--no-merge", action="store_true", help="不合并片段, 仅下载到目录并保留 m3u8 文件")
    parser.add_argument("--no-disk", action="store_true", help="不落磁盘, 下载后直接传给上传 hook 上传到云盘")

    # 加密相关
    parser.add_argument("--key-param", default=None, help="EXT-X-KEY 请求时附加的参数")
    parser.add_argument("--key-format", default=None, help="EXT-X-KEY 返回的格式")

    # 上传相关
    upload_group = parser.add_argument_group("百度云盘上传")
    upload_group.add_argument("--upload", "-u", metavar="REMOTE_DIR", help="上传到百度云盘")
    upload_group.add_argument("--upload-token", default=None, help="百度 ACCESS_TOKEN (默认从 .env 读取)")
    upload_group.add_argument("--upload-ondup", default="overwrite", choices=["fail", "overwrite", "newcopy"],
                             help="云端冲突策略 (默认: overwrite)")
    upload_group.add_argument("--upload-retries", type=int, default=5, help="上传重试次数 (默认: 3)")
    upload_group.add_argument("--upload-delete", action="store_true", default=True,
                             help="上传成功后删除本地分片 (默认)")
    upload_group.add_argument("--no-upload-delete", action="store_false", dest="upload_delete",
                             help="上传后保留本地分片")

    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    """验证命令行参数"""
    if args.no_disk and not args.upload:
        print("[!] --no-disk 需要配合 --upload 使用")
        sys.exit(1)

    if args.no_disk and not args.no_merge:
        print("[*] --no-disk 自动开启 --no-merge 模式 (数据不入磁盘无法合并)")
        args.no_merge = True


def build_hooks(args: argparse.Namespace) -> list[BaseHook]:
    """构建 hook 列表"""
    hooks = [SegmentMergeHook(
        config=MergeConfig(
            output=args.output,
            merge=not args.no_merge,
            keep_temp=args.keep_temp,
            tmp_dir=args.tmp_dir
        )
    )]

    if args.upload:
        token = load_token(args.upload_token)
        if not token:
            print("[!] 启用 --upload 但未提供 ACCESS_TOKEN, 请用 --upload-token 指定或写入 .env")
            sys.exit(1)
        hooks.append(YunpanUploadHook(
            token=token,
            remote_prefix=args.upload,
            ondup=args.upload_ondup,
            max_retries=args.upload_retries,
            delete_after_upload=args.upload_delete,
        ))

    return hooks


def main() -> None:
    """主入口"""
    args = build_cli_args()
    validate_args(args)

    hooks = build_hooks(args)
    headers = parse_headers(args.header)
    http_client = HTTPClient(headers=headers, proxy=args.proxy, timeout=args.timeout)

    config = DownloadConfig(
        m3u8_source=args.m3u8_input,
        output=args.output,
        base_url=args.base_url,
        workers=args.workers,
        retries=args.retries,
        no_merge=args.no_merge,
        keep_temp=args.keep_temp,
        tmp_dir=args.tmp_dir,
        no_disk=args.no_disk,
        key_param=args.key_param,
        key_format=args.key_format,
    )

    downloader = M3U8Downloader(config=config, http_client=http_client, hooks=hooks)
    downloader.run()


if __name__ == "__main__":
    main()