"""
M3U8 视频下载 & 合并工具 (支持 AES-128 解密)

用法:
    python m3u8_downloader.py <m3u8文件路径或URL> [选项]

示例:
    # 本地 m3u8 文件
    python m3u8_downloader.py video.m3u8 --base_url https://example.com/video/ -o merged.ts
    # m3u8 URL
    python m3u8_downloader.py https://example.com/playlist.m3u8 -o merged.ts
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
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import urllib.request

try:
    # pip3 install pycryptodome
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# 匹配 #EXT-X-KEY 行, 提取 METHOD / URI / IV
_EXT_KEY_RE = re.compile(
    r'#EXT-X-KEY\s*:\s*'
    r'(?:.*?METHOD\s*=\s*([^,\s]+))?'
    r'(?:.*?URI\s*=\s*"([^"]*)")?'
    r'(?:.*?IV\s*=\s*(0x[0-9a-fA-F]+))?',
    re.IGNORECASE,
)


def parse_m3u8(filepath: str) -> tuple[list[str], list[Optional[dict]]]:
    """
    解析 m3u8 文件, 返回 (片段URI列表, 每段的加密信息列表).

    key_info 为 None 表示不加密, 否则为 {"method": "AES-128", "key": bytes, "iv": bytes}.
    """
    segments: list[str] = []
    segment_keys: list[Optional[dict]] = []

    media_seq = 0
    current_key: Optional[dict] = None  # {"method", "key_uri", "iv": bytes | None}

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                media_seq = int(line.split(":")[1])

            elif line.startswith("#EXT-X-KEY"):
                m = _EXT_KEY_RE.match(line)
                if not m:
                    continue
                method = (m.group(1) or "").upper()
                if method == "NONE":
                    current_key = None
                elif method == "AES-128":
                    key_uri = m.group(2) or ""
                    iv_str = m.group(3)
                    iv = bytes.fromhex(iv_str[2:]) if iv_str else None
                    current_key = {"method": method, "key_uri": key_uri, "iv": iv}
                else:
                    print(f"[!] 不支持的加密方法: {method}, 跳过加密")

            elif not line.startswith("#"):
                segments.append(line)
                segment_keys.append(current_key)

    return segments, segment_keys, media_seq


def resolve_url(uri: str, base_url: Optional[str]) -> str:
    """将相对 URI 拼接为完整 URL."""
    if urlparse(uri).netloc:
        return uri
    if base_url is None:
        sys.exit(f"[!] 无法拼接相对路径 '{uri}', 未提供 base_url")
    return urljoin(base_url, uri)


def download_key(
    key_uri: str,
    base_url: Optional[str],
    headers: dict[str, str],
    opener: Optional[urllib.request.OpenerDirector],
    timeout: int,
) -> bytes:
    """下载并返回 AES-128 解密密钥."""
    key_url = resolve_url(key_uri, base_url)
    print(f"[*] 下载密钥: {key_url}")
    has_extra = (opener is not None) or headers
    if has_extra:
        req = urllib.request.Request(key_url, headers=headers)
        with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as resp:
            return resp.read()
    else:
        return urllib.request.urlopen(key_url, timeout=timeout).read()


def decrypt_aes128_cbc(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 解密并去除 PKCS7 填充."""
    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    decrypted = cipher.decrypt(data)
    # 去除 PKCS7 填充
    pad_len = decrypted[-1]
    if 1 <= pad_len <= 16:
        decrypted = decrypted[:-pad_len]
    return decrypted


def download_segment(
    url: str,
    dest: str,
    index: int,
    headers: Optional[dict[str, str]] = None,
    opener: Optional[urllib.request.OpenerDirector] = None,
    timeout: int = 5,
    retries: int = 3,
    key_info: Optional[dict] = None,
) -> tuple[int, str]:
    """下载单个片段, 失败时自动重试, 支持解密."""
    has_extra = (opener is not None) or (headers is not None)
    for attempt in range(retries):
        try:
            # 下载
            if has_extra:
                req = urllib.request.Request(url, headers=headers or {})
                with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as resp:
                    raw = resp.read()
            else:
                with urllib.request.urlopen(url, timeout=timeout) as resp:
                    raw = resp.read()

            # 解密
            if key_info is not None:
                raw = decrypt_aes128_cbc(raw, key_info["key"], key_info["iv"])

            with open(dest, "wb") as f:
                f.write(raw)
            return (index, "ok")
        except Exception as e:
            if attempt == retries - 1:
                return (index, f"失败: {e}")
            time.sleep(1 * (attempt + 1))
    return (index, "unknown error")

default_ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

def main():
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
    parser.add_argument("--retries", "-r", type=int, default=3, help="下载失败重试次数 (默认: 3)")
    args = parser.parse_args()

    # 0. 解析请求头
    headers: dict[str, str] = {}
    if args.header:
        for h in args.header:
            if ":" not in h:
                print(f"[!] 忽略无效的 Header 格式 (缺少冒号): {h}")
                continue
            key, _, value = h.partition(":")
            headers[key.strip()] = value.strip()
        print(f"[*] 自定义请求头: {headers}")

    # 1. 配置代理
    opener = None
    if args.proxy:
        proxy_handler = urllib.request.ProxyHandler({
            "http": args.proxy,
            "https": args.proxy,
        })
        opener = urllib.request.build_opener(proxy_handler)
        urllib.request.install_opener(opener)
        print(f"[*] 使用代理: {args.proxy}")

    # 2. 获取 m3u8 内容 (支持 URL 或本地文件)
    m3u8_path = args.m3u8_input
    m3u8_base = None  # 用于解析密钥 / 片段的相对路径
    if urlparse(m3u8_path).scheme in ("http", "https"):
        print(f"[*] 下载 m3u8 文件: {m3u8_path}")
        m3u8_base = m3u8_path
        with tempfile.NamedTemporaryFile(mode="w", suffix=".m3u8", delete=False, encoding="utf-8") as tmp:
            try:
                has_extra = (opener is not None) or headers
                if has_extra:
                    req = urllib.request.Request(m3u8_path, headers=headers)
                    with (opener or urllib.request.build_opener()).open(req, timeout=args.timeout) as resp:
                        tmp.write(resp.read().decode("utf-8"))
                else:
                    tmp.write(urllib.request.urlopen(m3u8_path, timeout=args.timeout).read().decode("utf-8"))
            except Exception as e:
                print(f"[!] 下载 m3u8 文件失败: {e}")
                sys.exit(1)
            m3u8_path = tmp.name

    # 3. 解析 m3u8 文件
    print(f"[*] 解析 m3u8 文件: {m3u8_path}")
    segments, segment_keys, media_seq = parse_m3u8(m3u8_path)
    if not segments:
        print("[!] m3u8 文件中未找到任何视频片段")
        sys.exit(1)
    print(f"[*] 共发现 {len(segments)} 个视频片段")

    # 4. 确定 base_url
    base_url = args.base_url
    if base_url is not None:
        base_url = base_url.rstrip("/") + "/"
        print(f"[*] 根 URL (--base_url): {base_url}")
        m3u8_base = base_url
    elif m3u8_base is not None:
        base_url = os.path.dirname(m3u8_base) + "/"
        print(f"[*] 根 URL (自动): {base_url}")
        m3u8_base = base_url

    # 5. 处理加密: 下载密钥, 计算每段的 IV
    has_encryption = False
    for i, key_tmpl in enumerate(segment_keys):
        if key_tmpl is None:
            continue
        has_encryption = True
        if key_tmpl["method"] != "AES-128":
            continue
        if "key" not in key_tmpl:
            raw_key = download_key(key_tmpl["key_uri"], base_url, headers, opener, args.timeout)
            key_tmpl["key"] = raw_key
        # 补全 IV: 没显式指定就用序号 (big-endian u64 -> 128bit)
        if key_tmpl["iv"] is None:
            seq = media_seq + i
            key_tmpl["iv"] = struct.pack(">Q", seq).rjust(16, b"\x00")
        # 将模板复制到当前段 (避免所有段共享同一个 dict)
        segment_keys[i] = dict(key_tmpl)

    if has_encryption:
        if not HAS_CRYPTO:
            print("[!] 检测到加密片段, 但未安装 pycryptodome. 请运行: pip install pycryptodome")
            sys.exit(1)
        print(f"[*] 检测到 AES-128 加密, 密钥已加载")

    # 6. 构造完整下载 URL
    urls = []
    for seg in segments:
        if urlparse(seg).netloc:
            urls.append(seg)
        elif base_url is not None:
            urls.append(urljoin(base_url, seg))
        else:
            print(f"[!] 片段 '{seg}' 是相对路径, 但未提供 base_url")
            sys.exit(1)

    # 7. 下载 / 解密所有片段到临时目录
    tmp_dir = tempfile.mkdtemp(prefix="m3u8_dl_")
    print(f"[*] 临时目录: {tmp_dir}")
    print(f"[*] 开始下载 (并发数: {args.workers})...")

    success_count = 0
    fail_count = 0
    total = len(segments)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for i, url in enumerate(urls):
            dest = os.path.join(tmp_dir, f"seg_{i:06d}")
            key_info = segment_keys[i]
            futures[executor.submit(
                download_segment, url, dest, i,
                headers, opener, args.timeout, args.retries, key_info,
            )] = i

        if HAS_TQDM:
            pbar = tqdm(total=total, desc="下载进度", unit="seg")
        else:
            pbar = None

        for future in as_completed(futures):
            idx, status = future.result()
            if status == "ok":
                success_count += 1
            else:
                fail_count += 1
                tqdm.write(f"  [!] 片段 {idx} ({segments[idx]}): {status}")

            if pbar is not None:
                pbar.update(1)
            elif (success_count + fail_count) % 100 == 0 or (success_count + fail_count) == total:
                print(f"  [*] 进度: {success_count + fail_count}/{total} "
                      f"(成功: {success_count}, 失败: {fail_count})")

        if pbar is not None:
            pbar.close()

    if fail_count > 0:
        print(f"[!] {fail_count} 个片段下载失败, 合并结果可能不完整")

    # 8. 按顺序合并所有片段
    print(f"[*] 合并片段到: {args.output}")
    merge_iter = range(len(segments))
    if HAS_TQDM:
        merge_iter = tqdm(merge_iter, desc="合并进度", unit="seg")
    with open(args.output, "wb") as outfile:
        for i in merge_iter:
            seg_path = os.path.join(tmp_dir, f"seg_{i:06d}")
            if os.path.exists(seg_path):
                with open(seg_path, "rb") as infile:
                    shutil.copyfileobj(infile, outfile)
    if HAS_TQDM and isinstance(merge_iter, tqdm):
        merge_iter.close()

    # 9. 清理临时文件
    if not args.keep_temp:
        print("[*] 清理临时文件...")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if m3u8_path != args.m3u8_input and os.path.exists(m3u8_path):
            os.unlink(m3u8_path)
    else:
        print(f"[*] 临时文件保留在: {tmp_dir}")

    total_size_mb = os.path.getsize(args.output) / (1024 * 1024)
    print(f"[*] 完成! 输出文件: {args.output} ({total_size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
