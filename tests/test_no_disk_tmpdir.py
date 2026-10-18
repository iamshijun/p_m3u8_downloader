"""不落盘 (--no-disk) 的临时目录契约.

两条规则:
  1. no_disk=True  → 不创建临时目录, 也不在磁盘上留 m3u8 副本
  2. 需要临时目录时 (降级合并) → run() 结束后目录被清理

在仓库根目录运行:

    .venv/bin/python -m unittest tests.test_no_disk_tmpdir -v
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

import m3u8_downloader as md
from m3u8_downloader import BaseHook, DownloadConfig, HookContext, M3U8Downloader


PLAYLIST_URL = "https://example.com/hls/index.m3u8"

PLAYLIST_TEXT = "\n".join([
    "#EXTM3U",
    "#EXT-X-VERSION:3",
    "#EXT-X-TARGETDURATION:4",
    "#EXT-X-MEDIA-SEQUENCE:0",
    "#EXTINF:4.0,",
    "seg0.ts",
    "#EXTINF:4.0,",
    "seg1.ts",
    "#EXTENDED",
    "#EXT-X-ENDLIST",
])


class FakeHTTPClient:
    """只实现下载器用到的两个方法: get / get_text."""

    def __init__(self, playlist_text: str = PLAYLIST_TEXT, segment_data: bytes = b"segdata"):
        self.playlist_text = playlist_text
        self.segment_data = segment_data
        self.requested: list[str] = []

    def get(self, url: str) -> bytes:
        self.requested.append(url)
        return self.segment_data

    def get_text(self, url: str, max_bytes: int = 0) -> str:
        self.requested.append(url)
        return self.playlist_text


class DataCollectingHook(BaseHook):
    """收集不落盘模式下收到的分片数据."""

    def __init__(self) -> None:
        self.segments: list[tuple[int, bytes, str]] = []

    def on_segment_data(self, ctx: HookContext) -> None:
        self.segments.append((ctx.segment_index, ctx.data, ctx.filename))


class NoDiskNoTmpDirTest(unittest.TestCase):
    def test_no_disk_does_not_create_tmp_dir(self) -> None:
        hook = DataCollectingHook()
        config = DownloadConfig(
            m3u8_source=PLAYLIST_URL,
            output="unused.ts",
            no_merge=True,
            no_disk=True,
            quiet=True,
        )

        def _fail(*args, **kwargs):
            raise AssertionError("不落盘模式不应创建临时目录/临时文件")

        with mock.patch.object(md.tempfile, "mkdtemp", side_effect=_fail), \
                mock.patch.object(md.tempfile, "NamedTemporaryFile", side_effect=_fail):
            result = M3U8Downloader(
                config=config,
                http_client=FakeHTTPClient(),
                hooks=[hook],
            ).run()

        self.assertIsNone(config.tmp_dir)
        self.assertEqual(result, "unused.ts")
        self.assertEqual([idx for idx, _, _ in hook.segments], [0, 1])
        self.assertEqual(hook.segments[0][1], b"segdata")


class TmpDirCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = tempfile.mkdtemp(prefix="m3u8_test_")
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_tmp_dir_removed_after_run(self) -> None:
        created: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def _mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(path)
            return path

        config = DownloadConfig(
            m3u8_source=PLAYLIST_URL,
            output=os.path.join(self.work, "out.ts"),
            quiet=True,
        )
        with mock.patch.object(md.tempfile, "mkdtemp", side_effect=_mkdtemp):
            result = M3U8Downloader(config=config, http_client=FakeHTTPClient()).run()

        self.assertTrue(os.path.isfile(result))
        self.assertEqual(len(created), 1)
        # 分片已合并进输出文件, 临时目录 (以及其中的 m3u8 副本) 被清理
        self.assertFalse(os.path.exists(created[0]))

    def test_tmp_dir_cleanup_on_failure(self) -> None:
        created: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def _mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(path)
            return path

        class BoomHook(BaseHook):
            def on_all_downloaded(self, ctx: HookContext) -> None:
                raise RuntimeError("boom")

        config = DownloadConfig(
            m3u8_source=PLAYLIST_URL,
            output=os.path.join(self.work, "out.ts"),
            quiet=True,
        )
        with mock.patch.object(md.tempfile, "mkdtemp", side_effect=_mkdtemp):
            with self.assertRaises(RuntimeError):
                M3U8Downloader(
                    config=config,
                    http_client=FakeHTTPClient(),
                    hooks=[BoomHook()],
                ).run()

        self.assertEqual(len(created), 1)
        self.assertFalse(os.path.exists(created[0]), "异常退出也必须清理临时目录")


if __name__ == "__main__":
    unittest.main()
