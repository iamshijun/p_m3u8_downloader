"""
M3U8 视频下载 & 合并工具 (支持 AES-128 解密 + Hook 插件机制)

用法:
    python m3u8_downloader.py <m3u8文件路径或URL> [选项]

示例:
    # 本地 m3u8 文件, 下载并合并
    python m3u8_downloader.py video.m3u8 --base_url https://example.com/video/ -o merged.ts
    # m3u8 URL, 仅下载不合并
    python m3u8_downloader.py https://example.com/playlist.m3u8 --no-merge -o my_video
    # 下载并上传到百度云盘
    python m3u8_downloader.py video.m3u8 --base_url https://example.com/video/ -no-disk \
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
from typing import Optional, Union
from urllib.parse import urljoin, urlparse

import urllib.request

try:
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# M3U8DownloadError
class M3U8DownloadError(Exception):
    def __init__(self, message):
       self.message = message
       super().__init__(self.message)

# 分片文件命名模板 (统一格式, 修改此处影响所有引用)
SEGMENT_FILENAME_TPL = "seg_{:06d}"

# ---------------------------------------------------------------------------
# Progress Manager
# ---------------------------------------------------------------------------


class ProgressBar:
    """进度条管理器：统一封装 tqdm 和 print 的逻辑."""

    def __init__(
        self,
        total: Optional[int] = None,
        desc: str = "",
        unit: str = "item",
        quiet: bool = False,
    ):
        """初始化进度条.

        Args:
            total: 总数，None 表示不确定
            desc: 描述文本
            unit: 单位
            quiet: 静默模式 (即使安装了 tqdm 也不使用), 适用于 API 调用场景,
                   此时进度由 hook 上报, 避免日志噪声.
        """
        self.total = total
        self.desc = desc
        self.unit = unit
        self.quiet = quiet
        self.pbar = None
        self.use_pbar = (not quiet) and HAS_TQDM and total is not None

    def __enter__(self):
        """支持 with 语法."""
        if self.use_pbar:
            self.pbar = tqdm(total=self.total, desc=self.desc, unit=self.unit)
        return self

    def __exit__(self, *args):
        """退出 with 语法，自动关闭进度条."""
        self.close()

    def update(self, n: int = 1) -> bool:
        """更新进度.

        Args:
            n: 增量，默认 1
        """
        if self.use_pbar and self.pbar:
            self.pbar.update(n)
            return True
        return False

    def write(self, msg: str) -> None:
        """输出消息（支持tqdm的环境）.

        Args:
            msg: 消息内容
        """
        if self.use_pbar and self.pbar:
            tqdm.write(msg)
        else:
            print(msg)

    def close(self) -> None:
        """关闭进度条."""
        if self.use_pbar and self.pbar:
            self.pbar.close()

    @staticmethod
    def simple_progress(current: int, total: int, desc: str = "") -> str:
        """生成简单的文本进度信息.

        Args:
            current: 当前进度
            total: 总数
            desc: 描述

        Returns:
            进度字符串，如 "进度: 10/20"
        """
        return f"{desc}: {current}/{total}"

    def log_progress(self, current: int, total: int, success: int = 0, fail: int = 0) -> None:
        """按阈值输出文本进度日志 (仅在非 tqdm 模式下生效).

        阈值: 每 100 个片段一次, 加上最后一次 (完成时).

        Args:
            current: 已完成的片段数
            total: 总片段数
            success: 成功数
            fail: 失败数
        """
        if self.use_pbar:
            # tqdm 正在显示, 不重复输出
            return
        if current % 100 != 0 and current != total:
            return
        if self.quiet:
            # 静默模式: 粗粒度, 不暴露成功/失败细节
            self.write(f"  片段 {current}/{total}")
        else:
            self.write(
                f"  [*] 进度: {current}/{total} (成功: {success}, 失败: {fail})"
            )

# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------


@dataclass
class KeyInfo:
    """AES-128 加密密钥信息."""

    method: str                             # "AES-128" / "NONE"
    key_uri: str = ""                       # 密钥下载地址
    key_data: bytes = b""                   # 下载后的密钥 (16字节)
    iv: Optional[bytes] = None              # 初始化向量 (16字节), None 表示用序号推导
    key_format: Optional[str] = None        # 密钥格式, 如 "media-drm-token"


@dataclass
class M3U8Segment:
    """单个视频片段."""

    uri: str
    duration: float = 0.0
    key: Optional[KeyInfo] = None           # 加密信息, None 表示明文


@dataclass
class M3U8Variant:
    """Master playlist 中的一个变体流 (不同清晰度/带宽)."""
    # #...
    # #EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=842x480
    # 842x480/video.m3u8
    # #EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720
    # 1280x720/video.m3u8
    uri: str
    bandwidth: int = 0
    width: Optional[int] = None
    height: Optional[int] = None
    codecs: Optional[str] = None
    resolution: Optional[str] = None


@dataclass
class M3U8Playlist:
    """解析后的 M3U8 播放列表."""

    segments: list[M3U8Segment] = field(default_factory=list)
    variants: list[M3U8Variant] = field(default_factory=list)  # master playlist 变体
    media_sequence: int = 0
    target_duration: float = 0.0
    version: int = 3

    @property
    def is_encrypted(self) -> bool:
        return any(seg.key is not None for seg in self.segments)

    @property
    def is_master(self) -> bool:
        """是否为 master playlist (含变体流列表, 无分片)."""
        return bool(self.variants) and not self.segments

    def __len__(self) -> int:
        return len(self.segments)

    def to_file_content(self, seg_format = SEGMENT_FILENAME_TPL) -> str:
        lines = []
        lines.append('#EXTM3U')
        lines.append(f'#EXT-X-VERSION:{self.version}')
        lines.append(f'#EXT-X-TARGETDURATION:{self.target_duration}')
        lines.append("#EXT-X-MEDIA-SEQUENCE:0")
        lines.append("#EXT-X-PLAYLIST-TYPE:VOD")
        for index,segment in enumerate(self.segments):
            lines.append(f"#EXTINF:{segment.duration},")
            lines.append(seg_format.format(index))
           
        lines.append("#EXT-X-ENDLIST") 
        return os.linesep.join(lines)
# ---------------------------------------------------------------------------
# HTTPClient
# ---------------------------------------------------------------------------


class HTTPClient:
    """封装代理、自定义 Header、超时的 HTTP 客户端."""

    def __init__(
        self,
        headers: Optional[dict[str, str]] = None,
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
            print(f"[*] 使用代理: {proxy}")

        if self.headers:
            print(f"[*] 自定义请求头: {self.headers}")

    def get(self, url: str) -> bytes:
        """GET 请求, 返回响应体字节."""
        has_extra = (self._opener is not None) or bool(self.headers)
        if has_extra:
            req = urllib.request.Request(url, headers=self.headers)
            with (self._opener or urllib.request.build_opener()).open(req, timeout=self.timeout) as resp:
                return resp.read()
        else:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return resp.read()

    def get_text(self, url: str) -> str:
        """GET 请求, 返回响应体文本."""
        return self.get(url).decode("utf-8")

    @staticmethod
    def parse_headers(raw_headers: Optional[Union[dict[str, str], list[str], str]]) -> dict[str, str]:
        """将 headers 输入统一解析为 dict 格式.

        支持三种输入:
        - dict[str, str]: 直接返回
        - list[str]:  每项为 "Key: Value" 格式
        - str:        单行 "Key: Value" 格式
        """
        if raw_headers is None:
            return {}
        if isinstance(raw_headers, dict):
            return {k.strip(): v.strip() for k, v in raw_headers.items()}
        if isinstance(raw_headers, str):
            raw_headers = [raw_headers]

        result: dict[str, str] = {}
        for h in raw_headers:
            if ":" not in h:
                print(f"[!] 忽略无效的 Header 格式 (缺少冒号): {h}")
                continue
            key, _, value = h.partition(":")
            result[key.strip()] = value.strip()
        return result


# ---------------------------------------------------------------------------
# Hook system
# ---------------------------------------------------------------------------


class BaseHook(ABC):
    """Hook 基类 — 生命周期钩子/回调接口方法, 子类按需覆盖."""
    
    def on_playlist_parsed(self, playlist: M3U8Playlist) -> None:
        """m3u8 解析完成后调用 (在主线程中调用)."""

    def on_segment(self, index: int, segment: M3U8Segment, filepath: str) -> None:
        """单个片段下载(落地到文件)+解密成功时调用 (在 worker 线程中调用)."""

    def on_segment_data(self, index: int, segment: M3U8Segment, data: bytes, filename: str) -> None:
        """不落地模式: 直接接收字节数据 (在 worker 线程中调用)."""

    def on_all_downloaded(self, playlist: M3U8Playlist, tmp_dir: str) -> None:
        """所有片段下载完成后调用 (在主线程中调用)."""

    def on_finish(self, result_path: str) -> None:
        """整个流程结束后调用."""


class SegmentMergeHook(BaseHook):
    """默认合并钩子: 将下载的分片按顺序合并为单个文件, 或保留为目录.

    merge=True  → 合并为 output (默认)
    merge=False → 保留分片目录 + 拷贝 m3u8 到目录中
    keep-temp=True → 保留临时目录, 不删除
    keep-temp=False → 移动临时目录 (默认)到目标目录 ${output}_segments
    tmp_dir → hls媒体片段临时目录 
    """

    def __init__(
        self,
        output: str,
        merge: bool = True,
        keep_temp: bool = False,
        tmp_dir: Optional[str] = None,
        quiet: bool = False,
    ):
        self.output = output
        self.merge = merge
        self.keep_temp = keep_temp
        self.tmp_dir = tmp_dir
        self.quiet = quiet
        self._m3u8_file: Optional[str] = None

    def set_m3u8_source_path(self, path: str) -> None:
        """设置 m3u8 源文件路径, 用于 no-merge 模式下拷贝到分片目录."""
        self._m3u8_file = path

    def on_all_downloaded(self, playlist: M3U8Playlist, tmp_dir: str) -> None:
        if self.merge:
            self._do_merge(tmp_dir, len(playlist))
        else:
            self._keep_segments(tmp_dir)

    def _do_merge(self, tmp_dir: str, total: int) -> None:
        """按顺序合并片段."""
        print(f"[*] 合并片段到: {self.output}")

        with ProgressBar(total=total, desc="合并进度", unit="seg", quiet=self.quiet) as pbar:
            with open(self.output, "wb") as outfile:
                for i in range(total):
                    path = os.path.join(tmp_dir, SEGMENT_FILENAME_TPL.format(i))
                    if os.path.exists(path):
                        with open(path, "rb") as infile:
                            shutil.copyfileobj(infile, outfile)
                    pbar.update(1)

    def _keep_segments(self, tmp_dir: str) -> None:
        """保留分片: 将临时目录重命名并拷贝 m3u8文件."""
        #  m3u8 源文件路径通过 set_m3u8_source_path 注入
        m3u8_src: Optional[str] = self._m3u8_file
        m3u8_dest = os.path.join(tmp_dir, "playlist.m3u8")
        if m3u8_src and os.path.isfile(m3u8_src):
            os.makedirs(os.path.dirname(m3u8_dest), exist_ok=True)
            shutil.copy(m3u8_src, m3u8_dest)

        # 如果指定了keep_temp，直接使用该目录，不再移动到 {output}_segments
        if self.keep_temp:
            print(f"[*] 已使用指定临时目录: {tmp_dir}")
            return
        
        name, _ = os.path.splitext(self.output)
        output_dir = name + "_segments"
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir, ignore_errors=True)
        print(f'[*] 移动临时目录到: {output_dir}')
        shutil.move(tmp_dir, output_dir)

    def on_finish(self, result_path: str) -> None:
        if not self.keep_temp and self.merge:
            # 清理临时目录 (分片已合并到 output)
            pass  # 由外部处理


class UploadHook(BaseHook, ABC):
    """上传钩子抽象基类: 定义上传后端的统一接口.

    子类需实现 on_segment / on_segment_data 将分片数据上传到目标存储.
    """


class YunpanUploadHook(UploadHook):
    """百度云盘上传钩子: 每下载完一个分片就上传到百度云盘."""

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
        try:
            from pan_upload import upload_file,upload_bytes
            self.upload_file = upload_file
            self.upload_bytes = upload_bytes
        except ImportError:
            print(f"[!] 无法导入 yunpan_hls")
            raise

    def on_playlist_parsed(self, playlist: M3U8Playlist):
        content = playlist.to_file_content()
        print("[*] upload m3u8 to ", f"{self.remote_prefix}/playlist.m3u8")
        self._do_upload_bytes(content.encode("utf-8"), "playlist.m3u8", f"{self.remote_prefix}/playlist.m3u8")

    def on_segment(self, index: int, segment: M3U8Segment, filepath: str) -> None:
        remote_path = f"{self.remote_prefix}/{os.path.basename(filepath)}"
        self._do_upload(filepath, remote_path)

    def on_segment_data(self, index: int, segment: M3U8Segment, data: bytes, filename: str) -> None:
        """不落地模式: 直接上传 bytes."""
        remote_path = f"{self.remote_prefix}/{SEGMENT_FILENAME_TPL.format(index)}"
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
            result = self.upload_bytes(
                data=data,
                filename=filename,
                remote_path=remote_path,
                token=self.token,
                ondup=self.ondup,
                max_retries=self.max_retries,
            )
            #print(f"[*] 上传成功: {remote_path} (fs_id={result.get('fs_id', '?')})")
        except Exception as e:
            print(f"[!] 上传失败: {remote_path} — {e}")


def create_upload_hook(hook_type: str, **kwargs) -> UploadHook:
    """工厂函数: 根据类型创建对应的 upload hook.

    当前支持的 hook_type:
      - "yunpan" → YunpanUploadHook
    """
    if hook_type == "yunpan":
        return YunpanUploadHook(**kwargs)
    raise ValueError(f"Unknown upload hook type: {hook_type}")


# ---------------------------------------------------------------------------
# M3U8Downloader
# ---------------------------------------------------------------------------

_EXT_KEY_RE = re.compile(
    r"#EXT-X-KEY\s*:\s*"
    r"(?:.*?METHOD\s*=\s*([^,\s]+))?"
    r'(?:.*?URI\s*=\s*"([^"]*)")?'
    r"(?:.*?IV\s*=\s*(0x[0-9a-fA-F]+))?"
    r"(?:.*?KEYFORMAT\s*=\s*\"([^\"]+)\")?",
    re.IGNORECASE,
)

# 解析 #EXT-X-STREAM-INF 行内的属性 (BANDWIDTH / RESOLUTION / CODECS 等)
## #EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720
## 1280x720/video.m3u8
_STREAM_INF_ATTR_RE = re.compile(
    r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]+))',
    re.IGNORECASE,
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


@dataclass
class DownloadConfig:
    """下载配置: 将 M3U8Downloader 的配置参数聚合成一个 dataclass."""
    m3u8_source: str
    output: str = "output.ts"
    base_url: Optional[str] = None
    workers: int = 10
    retries: int = 3
    no_merge: bool = False
    tmp_dir: Optional[str] = None
    keep_temp: bool = False
    no_disk: bool = False
    key_param: Optional[str] = None
    key_format: Optional[str] = None
    quiet: bool = False


class M3U8Downloader:
    """M3U8 视频下载器: 获取 m3u8 → 解析 → 下载片段 → 解密 → [hooks].

    Hooks 说明:
      - on_segment:      每段下载成功后调用 (在 worker 线程)
      - on_all_downloaded: 全部下载完成后调用 (主线程)
      - on_finish:       流程结束调用
      默认注册 SegmentMergeHook 执行合并.
    """

    def __init__(
        self,
        config: DownloadConfig,
        http_client: Optional[HTTPClient] = None,
        hooks: Optional[list[BaseHook]] = None,
    ):
        self.config = config
        self.http = http_client or HTTPClient()

        # 默认注册合并钩子 (除非 no_merge)
        if hooks is None:
            hooks = [SegmentMergeHook(
                output=config.output,
                merge=not config.no_merge,
                keep_temp=config.keep_temp,
                tmp_dir=config.tmp_dir,
                quiet=config.quiet,
            )]
        self.hooks = hooks

        # 内部状态
        # 下载的m3u8文件存放的临时路径 (ps:完成后暂时不会被删除)
        self._tmp_m3u8_path: Optional[str] = None

    # -- public API ----------------------------------------------------------

    def run(self) -> str:
        """执行完整下载流程, 返回输出文件/目录路径."""
        playlist = self._fetch_playlist()

        # master playlist: 自动选择最高清晰度变体后, 重新解析其 m3u8 走正常流程
        if playlist.is_master:
            playlist = self._resolve_master_playlist(playlist)

        if not playlist.segments:
            raise M3U8DownloadError("[!] m3u8 文件中未找到任何视频片段")
        print(f"[*] 共发现 {len(playlist)} 个视频片段")

        self._resolve_base_url()
        self._load_keys(playlist)

        if playlist.is_encrypted:
            if not HAS_CRYPTO:
                raise M3U8DownloadError("[!] 检测到加密片段, 但未安装 pycryptodome. 请运行: pip install pycryptodome")
            print("[*] 检测到 AES-128 加密, 密钥已加载")
            
        for hook in self.hooks:
            hook.on_playlist_parsed(playlist)

        if not self.config.tmp_dir: #如果没有指定创建一个
            self.config.tmp_dir = tempfile.mkdtemp(prefix="m3u8_dl_")
        print(f"[*] 临时目录: {self.config.tmp_dir}")
        # -- no_disk 模式: 下载后直接 on_segment_data, 不写磁盘 -------------------
        if self.config.no_disk:
            self._download_segments_no_disk(playlist)
            result = self.config.output
            for hook in self.hooks:
                hook.on_finish(result)
            self._cleanup()
            return result

        # -- 正常流程 ---------------------------------------------------------

        # 注入 m3u8 文件路径到 merge hook (no-merge 模式需要)
        self._inject_m3u8_context()

        segment_paths = self._download_segments(playlist)

        # 调用 on_all_downloaded
        for hook in self.hooks:
            hook.on_all_downloaded(playlist, self.config.tmp_dir)

        result_path = self._finalize()
        return result_path

    def _inject_m3u8_context(self) -> None:
        """将 m3u8 源文件路径注入到 SegmentMergeHook."""
        m3u8_file = self._tmp_m3u8_path or (
            self.config.m3u8_source if os.path.isfile(self.config.m3u8_source) else None
        )
        if m3u8_file is None:
            return
        for hook in self.hooks:
            if isinstance(hook, SegmentMergeHook):
                hook.set_m3u8_source_path(m3u8_file)

    def _finalize(self) -> str:
        """收尾: 调用 on_finish, 清理临时文件, 打印结果."""
        if self.config.no_merge:
            # 如果指定了keep-temp ，直接使用该目录
            if self.config.keep_temp:
                result = self.config.tmp_dir
                print(f"[*] 完成! 切片目录: {result}")
            else:
                name, _ = os.path.splitext(self.config.output)
                result = name + "_segments"
                print(f"[*] 完成! 切片目录: {result}")
        else:
            result = self.config.output
            if os.path.exists(self.config.output):
                total_size_mb = os.path.getsize(self.config.output) / (1024 * 1024)
                print(f"[*] 完成! 输出文件: {self.config.output} ({total_size_mb:.1f} MB)")

        for hook in self.hooks:
            hook.on_finish(result)

        self._cleanup()
        return result

    # -- fetch & parse -------------------------------------------------------

    def _fetch_playlist(self) -> M3U8Playlist:
        """获取 m3u8 内容 (支持 URL 或本地文件) 并解析."""
        if urlparse(self.config.m3u8_source).scheme in ("http", "https"):
            print(f"[*] 下载 m3u8 文件: {self.config.m3u8_source}")
            content = self.http.get_text(self.config.m3u8_source)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".m3u8", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(content)
                self._tmp_m3u8_path = tmp.name
            return self._parse_playlist(content)
        else:
            print(f"[*] 解析 m3u8 文件: {self.config.m3u8_source}")
            with open(self.config.m3u8_source, "r", encoding="utf-8") as f:
                return self._parse_playlist(f.read())

    def _resolve_master_playlist(self, master: M3U8Playlist) -> M3U8Playlist:
        """从 master playlist 选择最高清晰度变体, 拉取并解析其 m3u8."""
        best = self._select_best_variant(master.variants)
        if best.resolution:
            print(f"[*] master playlist 含 {len(master.variants)} 个变体, "
                  f"选择: {best.resolution} (带宽: {best.bandwidth})")
        else:
            print(f"[*] master playlist 含 {len(master.variants)} 个变体, "
                  f"选择带宽最高: {best.bandwidth}")

        # 先确保 base_url 已解析 (用于拼接变体 URI)
        self._resolve_base_url()
        variant_url = self._resolve_url(best.uri)
        print(f"[*] 获取变体 m3u8: {variant_url}")

        content = self.http.get_text(variant_url)

        # 用变体 m3u8 内容替换 _tmp_m3u8_path (供 no-merge 模式拷贝 playlist.m3u8)
        if self._tmp_m3u8_path and os.path.exists(self._tmp_m3u8_path):
            try:
                os.unlink(self._tmp_m3u8_path)
            except OSError:
                pass
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".m3u8", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(content)
            self._tmp_m3u8_path = tmp.name

        # 更新源: 后续 _resolve_base_url 会基于变体 URL 重新推导 base_url
        # 重置 base_url, 确保变体 m3u8 内的相对分片 URL 能正确解析到变体所在目录
        self.config.m3u8_source = variant_url
        self.config.base_url = None

        return self._parse_playlist(content)

    def _parse_playlist(self, content: str) -> M3U8Playlist:
        """解析 m3u8 文本内容 (支持 master playlist 的 #EXT-X-STREAM-INF)."""
        playlist = M3U8Playlist()
        current_key: Optional[KeyInfo] = None
        current_duration = 0.0
        current_variant: Optional[M3U8Variant] = None  # master playlist 当前变体元信息

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
            elif line.startswith("#EXT-X-KEY"):  # 密钥 iv ,....
                current_key = self._parse_key_line(line)
            elif line.startswith("#EXT-X-STREAM-INF"):
                # master playlist: 解析变体元信息, 下一行是该变体的 m3u8 URI #各种清晰度选择 
                current_variant = self._parse_stream_inf_line(line)
            elif not line.startswith("#"):
                if current_variant is not None:
                    # master playlist 中的变体 URI 行
                    current_variant.uri = line
                    playlist.variants.append(current_variant)
                    current_variant = None
                else:
                    seg = M3U8Segment(uri=line, duration=current_duration, key=current_key)
                    playlist.segments.append(seg)

        return playlist

    @staticmethod
    def _parse_key_line(line: str) -> Optional[KeyInfo]:
        """解析 #EXT-X-KEY 行."""
        m = _EXT_KEY_RE.match(line)
        if not m:
            return None
        method = (m.group(1) or "").upper()
        if method == "NONE":
            return None
        if method == "AES-128":
            iv_str = m.group(3)
            key_format = m.group(4) if m.group(4) else None
            return KeyInfo(
                method=method,
                key_uri=m.group(2) or "",
                iv=bytes.fromhex(iv_str[2:]) if iv_str else None,
                key_format=key_format,
            )
        print(f"[!] 不支持的加密方法: {method}, 按明文处理")
        return None

    @staticmethod
    def _parse_stream_inf_line(line: str) -> M3U8Variant:
        """解析 #EXT-X-STREAM-INF 行, 返回不含 uri 的变体 (uri 由下一行填充)."""
        attrs: dict[str, str] = {}
        for m in _STREAM_INF_ATTR_RE.finditer(line):
            key = m.group(1).upper()
            attrs[key] = m.group(2) if m.group(2) is not None else m.group(3).strip()

        bandwidth = int(attrs.get("BANDWIDTH", "0") or 0)
        resolution = attrs.get("RESOLUTION")
        width = height = None
        if resolution and "x" in resolution:
            parts = resolution.lower().split("x")
            if len(parts) == 2:
                try:
                    width, height = int(parts[0]), int(parts[1])
                except ValueError:
                    pass

        return M3U8Variant(
            uri="",
            bandwidth=bandwidth,
            width=width,
            height=height,
            codecs=attrs.get("CODECS"),
            resolution=resolution,
        )

    @staticmethod
    def _select_best_variant(variants: list[M3U8Variant]) -> M3U8Variant:
        """选择最佳变体: 优先按分辨率像素数, 其次按带宽."""
        def sort_key(v: M3U8Variant) -> tuple[int, int]:
            pixels = (v.width or 0) * (v.height or 0)
            return (pixels, v.bandwidth)
        return max(variants, key=sort_key)

    # -- base_url ------------------------------------------------------------

    def _resolve_base_url(self) -> None:
        """确定 base_url, 用于拼接相对路径."""
        if self.config.base_url is not None:
            self.config.base_url = self.config.base_url.rstrip("/") + "/"
            print(f"[*] 根 URL (--base_url): {self.config.base_url}")
        elif self._tmp_m3u8_path is not None:
            self.config.base_url = os.path.dirname(self.config.m3u8_source) + "/"
            print(f"[*] 根 URL (自动): {self.config.base_url}")
        elif self.config.m3u8_source and urlparse(self.config.m3u8_source).scheme in ("http", "https"):
            self.config.base_url = os.path.dirname(self.config.m3u8_source) + "/"
            print(f"[*] 根 URL (自动): {self.config.base_url}")

    def _resolve_url(self, uri: str) -> str:
        """将相对 URI 拼接为完整 URL."""
        if urlparse(uri).netloc:
            return uri
        if self.config.base_url is None:
            raise M3U8DownloadError(f"[!] 无法拼接相对路径 '{uri}', 未提供 base_url")
        return urljoin(self.config.base_url, uri)

    def _resolve_url_with_token(self, uri: str) -> str:
        """将相对 URI 拼接为完整 URL 并按需 附加其他参数."""
        from urllib.parse import parse_qs, parse_qsl,urlencode, urlparse, urlunparse

        url = self._resolve_url(uri)
        if not self.config.key_param:
            return url

        # 解析 URL 和查询参数
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query) if parsed.query else {}

        # 解析 key_param 为字典（parse_qsl 返回键值对元组列表）
        new_params = dict(parse_qsl(self.config.key_param))
        # 合并（新参数覆盖旧参数）
        query_params.update(new_params)

        # 重建 URL
        new_query = urlencode(query_params, doseq=True)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                             parsed.params, new_query, parsed.fragment))

    # -- encryption ----------------------------------------------------------

    def _load_keys(self, playlist: M3U8Playlist) -> None:
        """下载加密密钥并计算每段的 IV."""
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

    # -- download ------------------------------------------------------------

    def _download_segments(self, playlist: M3U8Playlist) -> list[str]:
        """并发下载所有片段 (含解密 + on_segment hook)."""
        urls = [self._resolve_url(seg.uri) for seg in playlist.segments]
        print(f"[*] 开始下载 (并发数: {self.config.workers})...")

        paths: list[Optional[str]] = [None] * len(playlist)
        success = 0
        fail = 0
        total = len(playlist)

        with ProgressBar(total=total, desc="下载进度", unit="seg", quiet=self.config.quiet) as pbar:
            with ThreadPoolExecutor(max_workers=self.config.workers) as executor:
                futures = {}
                for i, url in enumerate(urls):
                    #统一为6位的seg片段标识
                    dest = os.path.join(self.config.tmp_dir, SEGMENT_FILENAME_TPL.format(i))
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

                    if not pbar.update(1):
                        pbar.log_progress(success + fail, total, success, fail)

        if fail > 0:
            print(f"[!] {fail} 个片段下载失败, 合并结果可能不完整")

        return [p for p in paths if p is not None]

    def _download_segment_task(
        self,
        url: str,
        dest: str,
        index: int,
        segment: M3U8Segment,
        key_info: Optional[KeyInfo] = None,
    ) -> tuple[int, str, str]:
        """下载单个片段, 失败自动重试, 支持解密 + on_segment hook."""
        for attempt in range(self.config.retries):
            try:
                raw = self.http.get(url)
                if key_info is not None:
                    raw = self._decrypt(raw, key_info)
                with open(dest, "wb") as f:
                    f.write(raw)
                # 调用 on_segment hooks (在 worker 线程中)
                for hook in self.hooks:
                    hook.on_segment(index, segment, dest)
                return (index, dest, "ok")
            except Exception as e:
                if attempt == self.config.retries - 1:
                    return (index, "", f"失败: {e}")
                time.sleep(1 * (attempt + 1))
        return (index, "", "unknown error")

    def _download_segments_no_disk(self, playlist: M3U8Playlist) -> None:
        """不落地下载: 下载解密后直接 on_segment_data, 不写磁盘."""
        urls = [self._resolve_url(seg.uri) for seg in playlist.segments]
        print(f"[*] 开始不落地下载 (并发数: {self.config.workers})...")
        total = len(playlist)
        success = 0
        fail = 0

        with ProgressBar(total=total, desc="下载&上传", unit="seg", quiet=self.config.quiet) as pbar:
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
                        success += 1
                    else:
                        fail += 1
                        pbar.write(f"  [!] 片段 {idx} ({playlist.segments[idx].uri}): {status}")

                    if not pbar.update(1):
                        pbar.log_progress(success + fail, total, success, fail)

        if fail > 0:
            print(f"[!] {fail} 个片段失败")

    def _download_and_feed(
        self,
        url: str,
        index: int,
        segment: M3U8Segment,
        key_info: Optional[KeyInfo] = None,
    ) -> tuple[int, str]:
        """下载 + 解密 + on_segment_data (不写磁盘)."""
        for attempt in range(self.config.retries):
            try:
                raw = self.http.get(url)
                if key_info is not None:
                    raw = self._decrypt(raw, key_info)
                filename = os.path.basename(urlparse(url).path) or SEGMENT_FILENAME_TPL.format(index)
                for hook in self.hooks:
                    hook.on_segment_data(index, segment, raw, filename)
                return (index, "ok")
            except Exception as e:
                if attempt == self.config.retries - 1:
                    return (index, f"失败: {e}")
                time.sleep(1 * (attempt + 1))
        return (index, "unknown error")

    # -- decrypt -------------------------------------------------------------

    @staticmethod
    def _decrypt(data: bytes, key_info: KeyInfo) -> bytes:
        """AES-128-CBC 解密并去除 PKCS7 填充."""
        cipher = AES.new(key_info.key_data, AES.MODE_CBC, iv=key_info.iv or b"\x00" * 16)
        decrypted = cipher.decrypt(data)
        pad_len = decrypted[-1]
        if 1 <= pad_len <= 16:
            decrypted = decrypted[:-pad_len]
        return decrypted

    # -- cleanup -------------------------------------------------------------

    def _cleanup(self) -> None:
        """清理临时文件."""
        if self.config.keep_temp:
            if self.config.tmp_dir:
                print(f"[*] 临时文件保留在: {self.config.tmp_dir}")
        else:
            print("[*] 清理临时文件...")
            if self.config.tmp_dir:
                shutil.rmtree(self.config.tmp_dir, ignore_errors=True)
                
        if self._tmp_m3u8_path and os.path.exists(self._tmp_m3u8_path):
            os.unlink(self._tmp_m3u8_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_token(token_arg: Optional[str]) -> Optional[str]:
    """获取 ACCESS_TOKEN: 命令行 > .env."""
    if token_arg:
        return token_arg
    try:
        from dotenv import load_dotenv
        load_dotenv()
        return os.getenv("ACCESS_TOKEN")
    except ImportError:
        return os.getenv("ACCESS_TOKEN")


def main() -> None:
    parser = argparse.ArgumentParser(description="M3U8 视频下载 & 合并工具")
    parser.add_argument("m3u8_input", help="m3u8 文件路径或 URL")
    parser.add_argument("--base_url", default=None, help="视频片段的根 URL (片段为相对路径时需要)")
    parser.add_argument("--output", "-o", default="output.ts", help="输出文件名 (默认: output.ts)")
    parser.add_argument("--workers", "-w", type=int, default=10, help="并发下载线程数 (默认: 10)")
    parser.add_argument("--tmp-dir", default=None, help="显式指定临时下载目录")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时下载目录不删除")
    parser.add_argument("--proxy", "-p", default=None, help="代理地址, 例如 http://127.0.0.1:7890")
    parser.add_argument("--header", "-H", action="append", default=None,
                        help="自定义请求头, 格式 Key:Value, 可多次指定. 例如 -H 'Referer:https://example.com'")
    parser.add_argument("--timeout", "-t", type=int, default=5, help="HTTP 请求超时秒数 (默认: 5)")
    parser.add_argument("--retries", "-r", type=int, default=3, help="下载失败重试次数 (默认: 3)")
    parser.add_argument("--key-param", default=None, help="EXT-X-KEY 请求时附加的参数")
    parser.add_argument("--key-format", default=None, help="EXT-X-KEY 返回的格式")
    parser.add_argument("--no-merge", action="store_true", help="不合并片段, 仅下载到目录并保留 m3u8 文件")
    parser.add_argument("--no-disk", action="store_true", help="不落磁盘, 下载后直接传给上传 hook 上传到云盘")

    # --upload 选项组 需要根据 hls_yunpan的upload模块的参数来做传递/适配
    parser.add_argument("--upload", "-u", default=None, metavar="REMOTE_DIR",
                        help="上传到百度云盘, 后跟远程目录路径, 如 /apps/movies/my_video/")
    parser.add_argument("--upload-token", default=None, help="百度 ACCESS_TOKEN (默认从 .env 读取)")
    parser.add_argument("--upload-ondup", default="overwrite", choices=["fail", "overwrite", "newcopy"],
                        help="云端冲突策略 (默认: overwrite)")
    parser.add_argument("--upload-retries", type=int, default=5, help="上传重试次数 (默认: 3)")
    parser.add_argument("--upload-delete", action="store_true", default=True,
                        help="上传成功后删除本地分片 (默认)")
    parser.add_argument("--no-upload-delete", action="store_false", dest="upload_delete",
                        help="上传后保留本地分片")

    args = parser.parse_args()

    # --no-disk 校验
    if args.no_disk:
        if not args.upload:
            print("[!] --no-disk 需要配合 --upload 使用")
            sys.exit(1)
        if not args.no_merge:
            print("[*] --no-disk 自动开启 --no-merge 模式 (数据不入磁盘无法合并)")
            args.no_merge = True

    # 构建 hooks
    hooks: list[BaseHook] = [SegmentMergeHook(
        output=args.output,
        merge=not args.no_merge,
        keep_temp=args.keep_temp,
    )]

    if args.upload:
        token = _load_token(args.upload_token)
        if not token:
            print("[!] 启用 --upload 但未提供 ACCESS_TOKEN, 请用 --upload-token 指定或写入 .env")
            sys.exit(1)
        hooks.append(create_upload_hook(
            "yunpan",
            token=token,
            remote_prefix=args.upload,
            ondup=args.upload_ondup,
            max_retries=args.upload_retries,
            delete_after_upload=args.upload_delete,
        ))

    headers = HTTPClient.parse_headers(args.header)
    http = HTTPClient(headers=headers, proxy=args.proxy, timeout=args.timeout)

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
    downloader = M3U8Downloader(
        config=config,
        http_client=http,
        hooks=hooks,
    )
    downloader.run()


if __name__ == "__main__":
    main()
