"""
M3U8 视频下载 & 合并工具 (支持 AES-128 解密)

用法:
    python m3u8_downloader.py <m3u8文件路径或URL> [选项]

示例:
    # 本地 m3u8 文件, 下载并合并
    python m3u8_downloader.py video.m3u8 --base_url https://example.com/video/ -o merged.ts
    # m3u8 URL, 仅下载不合并 (保留切片文件和 m3u8)
    python m3u8_downloader.py https://example.com/playlist.m3u8 --no-merge -o my_video
"""

import argparse
import os
import re
import shutil
import struct
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional
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


@dataclass
class M3U8Segment:
    """单个视频片段."""

    uri: str
    duration: float = 0.0
    key: Optional[KeyInfo] = None           # 加密信息, None 表示明文


@dataclass
class M3U8Playlist:
    """解析后的 M3U8 播放列表."""

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
            urllib.request.install_opener(self._opener)
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


# ---------------------------------------------------------------------------
# M3U8Downloader
# ---------------------------------------------------------------------------

_EXT_KEY_RE = re.compile(
    r"#EXT-X-KEY\s*:\s*"
    r"(?:.*?METHOD\s*=\s*([^,\s]+))?"
    r'(?:.*?URI\s*=\s*"([^"]*)")?'
    r"(?:.*?IV\s*=\s*(0x[0-9a-fA-F]+))?",
    re.IGNORECASE,
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class M3U8Downloader:
    """M3U8 视频下载器: 获取 m3u8 → 解析 → 下载片段 → 解密 → 合并."""

    def __init__(
        self,
        m3u8_source: str,
        output: str = "output.ts",
        base_url: Optional[str] = None,
        workers: int = 10,
        retries: int = 3,
        no_merge: bool = False,
        keep_temp: bool = False,
        http_client: Optional[HTTPClient] = None,
    ):
        self.m3u8_source = m3u8_source
        self.output = output
        self.base_url = base_url
        self.workers = workers
        self.retries = retries
        self.no_merge = no_merge
        self.keep_temp = keep_temp
        self.http = http_client or HTTPClient()

        # 内部状态
        self._tmp_m3u8_path: Optional[str] = None
        self._tmp_dir: Optional[str] = None

    # -- public API ----------------------------------------------------------

    def run(self) -> str:
        """执行完整下载流程, 返回输出文件/目录路径."""
        playlist = self._fetch_playlist()
        if not playlist.segments:
            print("[!] m3u8 文件中未找到任何视频片段")
            sys.exit(1)
        print(f"[*] 共发现 {len(playlist)} 个视频片段")

        self._resolve_base_url()
        self._load_keys(playlist)

        if playlist.is_encrypted:
            if not HAS_CRYPTO:
                print("[!] 检测到加密片段, 但未安装 pycryptodome. 请运行: pip install pycryptodome")
                sys.exit(1)
            print("[*] 检测到 AES-128 加密, 密钥已加载")

        self._tmp_dir = tempfile.mkdtemp(prefix="m3u8_dl_")
        print(f"[*] 临时目录: {self._tmp_dir}")

        segment_paths = self._download_segments(playlist)

        if self.no_merge:
            return self._finish_no_merge(playlist)
        else:
            self._merge(segment_paths)
            self._cleanup()
            total_size_mb = os.path.getsize(self.output) / (1024 * 1024)
            print(f"[*] 完成! 输出文件: {self.output} ({total_size_mb:.1f} MB)")
            return self.output

    def _finish_no_merge(self, playlist: M3U8Playlist) -> str:
        """不合并模式: 将 m3u8 拷贝到下载目录, 移动目录到指定名称, 返回路径."""
        # 拷贝 / 写入 m3u8 文件到下载目录
        m3u8_dest = os.path.join(self._tmp_dir, os.path.basename(self.output) or "index.m3u8")
        if self._tmp_m3u8_path:
            shutil.copy(self._tmp_m3u8_path, m3u8_dest)
        elif os.path.isfile(self.m3u8_source):
            shutil.copy(self.m3u8_source, m3u8_dest)
        else:
            # m3u8 内容是从 URL 获取的, 另存一份
            with open(self._tmp_m3u8_path or m3u8_dest, "r", encoding="utf-8") as src:
                with open(m3u8_dest, "w", encoding="utf-8") as dst:
                    dst.write(src.read())

        # 将临时目录移动到用户指定的输出名 (目录)
        output_dir = self.output
        # 如果 output 以 .ts/.mp4 等结尾, 去掉扩展名当做目录
        name, _ = os.path.splitext(output_dir)
        output_dir = name + "_segments"
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir, ignore_errors=True)
        shutil.move(self._tmp_dir, output_dir)
        self._tmp_dir = output_dir

        # 清理可能下载的临时 m3u8
        if self._tmp_m3u8_path and os.path.exists(self._tmp_m3u8_path):
            os.unlink(self._tmp_m3u8_path)

        print(f"[*] 完成! 切片目录: {output_dir}")
        return output_dir

    # -- fetch & parse -------------------------------------------------------

    def _fetch_playlist(self) -> M3U8Playlist:
        """获取 m3u8 内容 (支持 URL 或本地文件) 并解析."""
        if urlparse(self.m3u8_source).scheme in ("http", "https"):
            print(f"[*] 下载 m3u8 文件: {self.m3u8_source}")
            content = self.http.get_text(self.m3u8_source)
            # 写临时文件, 后续键钥/片段相对路径以此为基准
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".m3u8", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(content)
                self._tmp_m3u8_path = tmp.name
            return self._parse_playlist(content)
        else:
            print(f"[*] 解析 m3u8 文件: {self.m3u8_source}")
            with open(self.m3u8_source, "r", encoding="utf-8") as f:
                return self._parse_playlist(f.read())

    def _parse_playlist(self, content: str) -> M3U8Playlist:
        """解析 m3u8 文本内容."""
        playlist = M3U8Playlist()
        current_key: Optional[KeyInfo] = None

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
                duration = float(line.split(":")[1].rstrip(","))
                current_duration = duration
            elif line.startswith("#EXT-X-KEY"):
                current_key = self._parse_key_line(line)
            elif not line.startswith("#"):
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
            return KeyInfo(
                method=method,
                key_uri=m.group(2) or "",
                iv=bytes.fromhex(iv_str[2:]) if iv_str else None,
            )
        print(f"[!] 不支持的加密方法: {method}, 按明文处理")
        return None

    # -- base_url ------------------------------------------------------------

    def _resolve_base_url(self) -> None:
        """确定 base_url, 用于拼接相对路径."""
        if self.base_url is not None:
            self.base_url = self.base_url.rstrip("/") + "/"
            print(f"[*] 根 URL (--base_url): {self.base_url}")
        elif self._tmp_m3u8_path is not None:
            # 从 m3u8 URL 自动推导
            self.base_url = os.path.dirname(self.m3u8_source) + "/"
            print(f"[*] 根 URL (自动): {self.base_url}")
        elif self.m3u8_source and urlparse(self.m3u8_source).scheme in ("http", "https"):
            self.base_url = os.path.dirname(self.m3u8_source) + "/"
            print(f"[*] 根 URL (自动): {self.base_url}")

    def _resolve_url(self, uri: str) -> str:
        """将相对 URI 拼接为完整 URL."""
        if urlparse(uri).netloc:
            return uri
        if self.base_url is None:
            sys.exit(f"[!] 无法拼接相对路径 '{uri}', 未提供 base_url")
        return urljoin(self.base_url, uri)

    # -- encryption ----------------------------------------------------------

    def _load_keys(self, playlist: M3U8Playlist) -> None:
        """下载加密密钥并计算每段的 IV."""
        key_cache: dict[str, bytes] = {}  # key_uri -> key_data, 避免重复下载相同密钥

        for i, seg in enumerate(playlist.segments):
            if seg.key is None:
                continue

            # 下载密钥 (同一 URI 只下载一次)
            if seg.key.key_uri not in key_cache:
                key_url = self._resolve_url(seg.key.key_uri)
                print(f"[*] 下载密钥: {key_url}")
                key_cache[seg.key.key_uri] = self.http.get(key_url)
            seg.key.key_data = key_cache[seg.key.key_uri]

            # 补全 IV
            if seg.key.iv is None:
                seq = playlist.media_sequence + i
                seg.key.iv = struct.pack(">Q", seq).rjust(16, b"\x00")

    # -- download ------------------------------------------------------------

    def _download_segments(self, playlist: M3U8Playlist) -> list[str]:
        """并发下载所有片段 (含解密), 返回按序号排列的临时文件路径列表."""
        urls = [self._resolve_url(seg.uri) for seg in playlist.segments]
        print(f"[*] 开始下载 (并发数: {self.workers})...")

        paths: list[Optional[str]] = [None] * len(playlist)
        success = 0
        fail = 0
        total = len(playlist)

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {}
            for i, url in enumerate(urls):
                dest = os.path.join(self._tmp_dir, f"seg_{i:06d}")
                seg_key = playlist.segments[i].key
                futures[executor.submit(self._download_single, url, dest, i, seg_key)] = i

            pbar = tqdm(total=total, desc="下载进度", unit="seg") if HAS_TQDM else None

            for future in as_completed(futures):
                idx, filepath, status = future.result()
                if status == "ok":
                    success += 1
                    paths[idx] = filepath
                else:
                    fail += 1
                    tqdm.write(f"  [!] 片段 {idx} ({playlist.segments[idx].uri}): {status}")

                if pbar is not None:
                    pbar.update(1)
                elif (success + fail) % 100 == 0 or (success + fail) == total:
                    print(f"  [*] 进度: {success + fail}/{total} (成功: {success}, 失败: {fail})")

            if pbar is not None:
                pbar.close()

        if fail > 0:
            print(f"[!] {fail} 个片段下载失败, 合并结果可能不完整")

        return [p for p in paths if p is not None]

    def _download_single(
        self, url: str, dest: str, index: int, key_info: Optional[KeyInfo] = None
    ) -> tuple[int, str, str]:
        """下载单个片段, 失败自动重试, 支持解密.

        Returns (index, file_path, status).
        """
        for attempt in range(self.retries):
            try:
                raw = self.http.get(url)
                if key_info is not None:
                    raw = self._decrypt(raw, key_info)
                with open(dest, "wb") as f:
                    f.write(raw)
                return (index, dest, "ok")
            except Exception as e:
                if attempt == self.retries - 1:
                    return (index, "", f"失败: {e}")
                time.sleep(1 * (attempt + 1))
        return (index, "", "unknown error")

    # -- decrypt -------------------------------------------------------------

    @staticmethod
    def _decrypt(data: bytes, key_info: KeyInfo) -> bytes:
        """AES-128-CBC 解密并去除 PKCS7 填充."""
        cipher = AES.new(key_info.key_data, AES.MODE_CBC, iv=key_info.iv or b"\x00" * 16)
        decrypted = cipher.decrypt(data)
        # PKCS7 去填充
        pad_len = decrypted[-1]
        if 1 <= pad_len <= 16:
            decrypted = decrypted[:-pad_len]
        return decrypted

    # -- merge ---------------------------------------------------------------

    def _merge(self, segment_paths: list[str]) -> None:
        """按顺序合并片段."""
        print(f"[*] 合并片段到: {self.output}")

        merge_iter: object = range(len(segment_paths))
        if HAS_TQDM:
            merge_iter = tqdm(merge_iter, desc="合并进度", unit="seg")

        with open(self.output, "wb") as outfile:
            for i in merge_iter:
                path = segment_paths[i] if i < len(segment_paths) else None
                if path and os.path.exists(path):
                    with open(path, "rb") as infile:
                        shutil.copyfileobj(infile, outfile)

        if HAS_TQDM and isinstance(merge_iter, tqdm):
            merge_iter.close()

    # -- cleanup -------------------------------------------------------------

    def _cleanup(self) -> None:
        """清理临时文件."""
        if self.keep_temp:
            if self._tmp_dir:
                print(f"[*] 临时文件保留在: {self._tmp_dir}")
            return
        print("[*] 清理临时文件...")
        if self._tmp_dir:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        if self._tmp_m3u8_path and os.path.exists(self._tmp_m3u8_path):
            os.unlink(self._tmp_m3u8_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_headers(raw_headers: Optional[list[str]]) -> dict[str, str]:
    """将命令行 -H 参数解析为 headers 字典."""
    headers: dict[str, str] = {}
    if not raw_headers:
        return headers
    for h in raw_headers:
        if ":" not in h:
            print(f"[!] 忽略无效的 Header 格式 (缺少冒号): {h}")
            continue
        key, _, value = h.partition(":")
        headers[key.strip()] = value.strip()
    return headers


def main() -> None:
    parser = argparse.ArgumentParser(description="M3U8 视频下载 & 合并工具")
    parser.add_argument("m3u8_input", help="m3u8 文件路径或 URL")
    parser.add_argument("--base_url", default=None, help="视频片段的根 URL (片段为相对路径时需要)")
    parser.add_argument("--output", "-o", default="output.ts", help="输出文件名 (默认: output.ts)")
    parser.add_argument("--workers", "-w", type=int, default=10, help="并发下载线程数 (默认: 10)")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时下载目录不删除")
    parser.add_argument("--proxy", "-p", default=None, help="代理地址, 例如 http://127.0.0.1:7890")
    parser.add_argument("--header", "-H", action="append", default=None,
                        help="自定义请求头, 格式 Key:Value, 可多次指定. 例如 -H 'Referer:https://example.com'")
    parser.add_argument("--timeout", "-t", type=int, default=5, help="HTTP 请求超时秒数 (默认: 5)")
    parser.add_argument("--retries", "-r", type=int, default=7, help="下载失败重试次数 (默认: 7)")
    parser.add_argument("--no-merge", action="store_true", help="不合并片段, 仅下载到目录并保留 m3u8 文件")
    args = parser.parse_args()

    headers = _parse_headers(args.header)
    http = HTTPClient(headers=headers, proxy=args.proxy, timeout=args.timeout)

    downloader = M3U8Downloader(
        m3u8_source=args.m3u8_input,
        output=args.output,
        base_url=args.base_url,
        workers=args.workers,
        retries=args.retries,
        no_merge=args.no_merge,
        keep_temp=args.keep_temp,
        http_client=http,
    )
    downloader.run()


if __name__ == "__main__":
    main()
