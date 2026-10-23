"""片段过滤 (--segments) 的回归用例.

覆盖:
  1. M3U8Downloader._parse_segment_ranges()  —— 表达式解析 (单个位置 / 闭区间 / 开区间)
  2. M3U8Downloader._apply_segment_filter()  —— 按解析结果过滤 playlist, 保留原始序号

支持的位置表达式 (0 起始):
    "0,3,5"   单个位置
    "10-20"   闭区间
    "1600-"   从 1600 到最后一个片段
    空/None   不限制 (全部下载)

在仓库根目录运行:

    .venv/bin/python -m unittest tests.test_segment_filter
"""

from __future__ import annotations

import contextlib
import io
import unittest

from m3u8_downloader import (
    DownloadConfig,
    M3U8DownloadError,
    M3U8Downloader,
    M3U8Playlist,
    M3U8Segment,
)

# (输入, 期望的区间列表) —— None 表示不限制; (start, end) 中 end 为 None 表示到末尾
PARSE_CASES: list[tuple["str | None", "list[tuple[int, int | None]] | None"]] = [
    # -- 未指定: 不限制 -------------------------------------------------------
    (None, None),
    ("", None),
    ("   ", None),
    (",", None),
    # -- 单个位置 ------------------------------------------------------------
    ("0", [(0, 0)]),
    ("5", [(5, 5)]),
    ("0,3,5", [(0, 0), (3, 3), (5, 5)]),
    (" 0 , 3 ", [(0, 0), (3, 3)]),          # 两侧空白忽略
    ("3,3", [(3, 3), (3, 3)]),              # 重复项后续按集合去重
    # -- 闭区间 --------------------------------------------------------------
    ("10-20", [(10, 20)]),
    ("10-10", [(10, 10)]),
    ("1-3,7,10-13", [(1, 3), (7, 7), (10, 13)]),
    # -- 开区间: 到末尾 ------------------------------------------------------
    ("1600-", [(1600, None)]),
    (" 1600 - ", [(1600, None)]),
    ("0-1,1600-", [(0, 1), (1600, None)]),
]

# 非法项: 告警后忽略 (其余合法项仍然生效)
INVALID_CASES: list[tuple[str, "list[tuple[int, int | None]] | None"]] = [
    ("abc", None),
    ("-", None),
    ("1.5", None),
    ("1..3", None),
    ("20-10", None),                        # 起点大于终点
    ("1--3", None),
    ("-5", None),                           # 不支持负数 / 尾部区间
    ("a-3", None),
    ("3-b", None),
    ("1-2-3", None),
    ("1,abc", [(1, 1)]),
    ("1,20-10,2", [(1, 1), (2, 2)]),
]


def _playlist(total: int) -> M3U8Playlist:
    """构造 total 个片段, 每个片段记录自己在原始列表中的位置."""
    return M3U8Playlist(segments=[M3U8Segment(uri=f"seg{idx}.ts", index=idx) for idx in range(total)])


def _filter(segments_spec: "str | None", total: int, quiet: bool = True) -> list[int]:
    """跑一遍过滤, 返回过滤后各片段的原始序号 (应该等于被选中的位置).

    quiet=False 时不吞掉日志, 便于用 contextlib.redirect_stdout 断言告警文案.
    """
    downloader = M3U8Downloader(
        DownloadConfig(m3u8_source="https://example.com/playlist.m3u8", segments=segments_spec, quiet=True)
    )
    playlist = _playlist(total)
    if quiet:
        with contextlib.redirect_stdout(io.StringIO()):
            downloader._apply_segment_filter(playlist)
    else:
        downloader._apply_segment_filter(playlist)
    return [seg.index for seg in playlist.segments]


class ParseSegmentRangesTest(unittest.TestCase):
    """表达式解析本体."""

    def test_valid_cases(self):
        for raw, expected in PARSE_CASES:
            with self.subTest(raw=raw):
                self.assertEqual(M3U8Downloader._parse_segment_ranges(raw), expected)

    def test_invalid_cases_are_dropped(self):
        for raw, expected in INVALID_CASES:
            with self.subTest(raw=raw):
                self.assertEqual(M3U8Downloader._parse_segment_ranges(raw), expected)


class ApplySegmentFilterTest(unittest.TestCase):
    """过滤结果 (原始序号保持不变, 不重排)."""

    def test_no_spec_keeps_everything(self):
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(_filter(raw, 5), [0, 1, 2, 3, 4])

    def test_single_positions(self):
        self.assertEqual(_filter("0,3,5", 10), [0, 3, 5])

    def test_closed_range(self):
        self.assertEqual(_filter("10-12", 20), [10, 11, 12])

    def test_open_range_to_end(self):
        self.assertEqual(_filter("1600-", 2000), list(range(1600, 2000)))

    def test_open_range_from_zero(self):
        self.assertEqual(_filter("0-", 4), [0, 1, 2, 3])

    def test_mixed_spec(self):
        self.assertEqual(_filter("0,2-3,7-", 10), [0, 2, 3, 7, 8, 9])

    def test_range_beyond_total_is_clipped(self):
        self.assertEqual(_filter("2-100", 5), [2, 3, 4])

    def test_start_beyond_total_is_ignored(self):
        """起点越界只告警, 不影响其它合法项."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            selected = _filter("1,1600-", 5, quiet=False)
        self.assertEqual(selected, [1])
        self.assertIn("超出范围", out.getvalue())

    def test_all_out_of_range_raises(self):
        for spec in ("10", "1600-", "20-30"):
            with self.subTest(spec=spec):
                with self.assertRaises(M3U8DownloadError):
                    _filter(spec, 5)

    def test_invalid_items_warn_but_keep_valid_ones(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            selected = _filter("abc,2,20-10", 5, quiet=False)
        self.assertEqual(selected, [2])
        self.assertIn("忽略无效的片段位置", out.getvalue())

    def test_original_index_preserved(self):
        """过滤后 M3U8Segment.index 仍是原序号 (分片命名/派生 IV 依赖它)."""
        downloader = M3U8Downloader(
            DownloadConfig(m3u8_source="https://example.com/playlist.m3u8", segments="100-", quiet=True)
        )
        playlist = _playlist(150)
        with contextlib.redirect_stdout(io.StringIO()):
            downloader._apply_segment_filter(playlist)
        self.assertEqual([seg.index for seg in playlist.segments], list(range(100, 150)))
        self.assertEqual(playlist.segments[0].uri, "seg100.ts")


if __name__ == "__main__":
    unittest.main()
