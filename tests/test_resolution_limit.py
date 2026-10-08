"""清晰度上限 (--resolution) 的回归用例.

覆盖三层:
  1. m3u8_downloader.parse_resolution_limit()  —— 解析/校验本体
  2. m3u8_downloader.M3U8Downloader 的变体选择   —— 宽高双限过滤 + 无候选报错 + 无 RESOLUTION 回退
  3. api_server.validate_resolution_limit() + 两个接口的契约 —— 单条 400, 批量逐行 errors

前端 web/js/utils/validate.js 的 validateResolutionLimit() 是同一套语义的 JS 镜像,
下面的 CASES 表就是两边共同遵守的契约; 改规则时两边和本表要一起改.

在仓库根目录运行 (需要 requirements.txt 里的 fastapi/pydantic):

    .venv/bin/python -m unittest
"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

from m3u8_downloader import (
    DownloadConfig,
    M3U8DownloadError,
    M3U8Downloader,
    M3U8Variant,
    format_resolution_limit,
    parse_resolution_limit,
)

try:
    from fastapi.testclient import TestClient

    import api_server
    from api_server import validate_resolution_limit

    API_IMPORT_ERROR = ""
except Exception as e:  # pragma: no cover - 缺依赖时整文件跳过
    API_IMPORT_ERROR = f"{type(e).__name__}: {e}"


# (输入, 期望解析结果) —— None 表示不限制; (宽度, 高度) 表示上限, 宽度为 None 表示只限高度
PARSE_CASES: list[tuple["str | None", "tuple[int | None, int] | None"]] = [
    # -- 未指定: 不限制 -------------------------------------------------------
    (None, None),
    ("", None),
    ("   ", None),
    ("\t\n", None),
    # -- NNNp: 只限高度 -------------------------------------------------------
    ("480p", (None, 480)),
    ("720p", (None, 720)),
    ("1080p", (None, 1080)),
    ("720P", (None, 720)),          # 大小写不敏感
    (" 480p ", (None, 480)),        # 两侧空白忽略
    # -- WxH: 宽高双限 --------------------------------------------------------
    ("854x480", (854, 480)),
    ("1280x720", (1280, 720)),
    ("854X480", (854, 480)),        # 大小写不敏感
    (" 854x480\t", (854, 480)),
]

# (输入, 期望原因片段) —— 全部为格式非法
INVALID_CASES: list[tuple[str, str]] = [
    ("480", "格式无效"),            # 缺 p / 缺 x
    ("480i", "格式无效"),
    ("abc", "格式无效"),
    ("p", "格式无效"),
    ("p480", "格式无效"),
    ("0p", "格式无效"),             # 0 不是有效高度
    ("0x480", "格式无效"),
    ("854x0", "格式无效"),
    ("-480p", "格式无效"),
    ("854x", "格式无效"),
    ("x480", "格式无效"),
    ("854x480x1", "格式无效"),
    ("854 x 480", "格式无效"),
    ("854*480", "格式无效"),
    ("1080p60", "格式无效"),
]


class ParseResolutionLimitTest(unittest.TestCase):
    """解析本体: 合法返回 (宽, 高), 未指定返回 None, 非法抛 ValueError."""

    def test_valid_cases(self):
        for raw, expected in PARSE_CASES:
            with self.subTest(raw=raw):
                self.assertEqual(parse_resolution_limit(raw), expected)

    def test_invalid_cases_raise(self):
        for raw, fragment in INVALID_CASES:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as ctx:
                    parse_resolution_limit(raw)
                self.assertIn(fragment, str(ctx.exception))
                self.assertIn(repr(raw)[1:-1], str(ctx.exception), "报错应回显原始输入")

    def test_format_limit_round_trip(self):
        """format_resolution_limit 是日志/报错里展示上限的来源."""
        self.assertEqual(format_resolution_limit(parse_resolution_limit("480p")), "480p")
        self.assertEqual(format_resolution_limit(parse_resolution_limit("854x480")), "854x480")


# ---------------------------------------------------------------------------
# 变体选择 (宽高双限)
# ---------------------------------------------------------------------------

MASTER_URL = "https://example.com/master.m3u8"

# 与需求里的真实 master 一致: 360p / 480p / 720p
MASTER_TEXT = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1416417,CODECS="avc1.64001e,mp4a.40.2",RESOLUTION=640x360
360p/video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2444189,CODECS="avc1.64001e,mp4a.40.2",RESOLUTION=854x480
480p/video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4726829,CODECS="avc1.64001f,mp4a.40.2",RESOLUTION=1280x720
720p/video.m3u8
"""

# 超宽变体 (1920x480): 只限高度会被选中, 宽高双限不会 — 用于锁定 B 语义
ULTRAWIDE_MASTER_TEXT = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2444189,RESOLUTION=854x480
480p/video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x480
wide/video.m3u8
"""

# 变体列表里只有 BANDWIDTH, 没有 RESOLUTION
NO_RESOLUTION_MASTER_TEXT = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1000000
low/video.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000
high/video.m3u8
"""

VARIANT_TEXT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg0.ts
#EXT-X-ENDLIST
"""


class _FakeHTTPClient:
    """只回答变体 m3u8 的请求, 不触网."""

    def __init__(self, text: str = VARIANT_TEXT):
        self.text = text
        self.requested: list[str] = []

    def get_text(self, url: str, max_bytes=None) -> str:
        self.requested.append(url)
        return self.text

    def get(self, url: str, max_bytes=None) -> bytes:  # pragma: no cover - 选择变体不下载分片
        self.requested.append(url)
        return self.text.encode("utf-8")


class SelectVariantByLimitTest(unittest.TestCase):
    """_resolve_master_playlist 在给了上限时的选变体行为."""

    def _resolve(self, master_text: str, resolution):
        """构建下载器并解析 master, 返回 (选择的变体 URL, stdout 日志)."""
        http = _FakeHTTPClient()
        downloader = M3U8Downloader(
            DownloadConfig(m3u8_source=MASTER_URL, resolution=resolution),
            http_client=http,
            hooks=[],
        )
        master = downloader._parse_playlist(master_text)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            downloader._resolve_master_playlist(master)
        if downloader._tmp_m3u8_path:
            self.addCleanup(lambda p=downloader._tmp_m3u8_path: os.path.exists(p) and os.unlink(p))
        return http.requested[-1], buf.getvalue()

    def test_not_specified_picks_highest(self):
        """未指定上限: 行为与改动前一致 (最高清晰度)."""
        url, _ = self._resolve(MASTER_TEXT, None)
        self.assertEqual(url, "https://example.com/720p/video.m3u8")

    def test_height_only_cap(self):
        for spec, expected in (("480p", "480p/video.m3u8"),
                               ("360p", "360p/video.m3u8"),
                               ("720p", "720p/video.m3u8"),
                               ("1080p", "720p/video.m3u8")):
            with self.subTest(spec=spec):
                url, _ = self._resolve(MASTER_TEXT, spec)
                self.assertEqual(url, f"https://example.com/{expected}")

    def test_dual_dim_cap(self):
        """854x480 既要挡掉 720p, 也要挡掉比上限更宽的 1920x480."""
        url, _ = self._resolve(MASTER_TEXT, "854x480")
        self.assertEqual(url, "https://example.com/480p/video.m3u8")

        url, _ = self._resolve(ULTRAWIDE_MASTER_TEXT, "854x480")
        self.assertEqual(url, "https://example.com/480p/video.m3u8")

    def test_picks_highest_within_limit(self):
        """上限内取最高: 640x360 + 854x480 都合格时选 854x480."""
        url, _ = self._resolve(MASTER_TEXT, "1280x720")
        self.assertEqual(url, "https://example.com/720p/video.m3u8")

    def test_all_above_limit_raises_with_available(self):
        """全部超限: 报错终止 (不偷偷降级), 并列出可用清晰度."""
        with self.assertRaises(M3U8DownloadError) as ctx:
            self._resolve(MASTER_TEXT, "240p")
        msg = str(ctx.exception)
        self.assertIn("无满足清晰度上限 240p 的变体", msg)
        self.assertIn("640x360", msg)
        self.assertIn("1280x720", msg)

    def test_cap_checked_before_downloading(self):
        """超过上限时不请求变体 m3u8 (失败要发生在下载之前)."""
        http = _FakeHTTPClient()
        downloader = M3U8Downloader(
            DownloadConfig(m3u8_source=MASTER_URL, resolution="240p"),
            http_client=http,
            hooks=[],
        )
        with self.assertRaises(M3U8DownloadError):
            with contextlib.redirect_stdout(io.StringIO()):
                downloader._resolve_master_playlist(downloader._parse_playlist(MASTER_TEXT))
        self.assertEqual(http.requested, [])
        self.assertIsNone(downloader._tmp_m3u8_path)

    def test_variants_without_resolution_fall_back_to_bandwidth(self):
        """变体没有 RESOLUTION 时无从判断: 告警 + 退回按带宽选 (不报错)."""
        url, logs = self._resolve(NO_RESOLUTION_MASTER_TEXT, "480p")
        self.assertEqual(url, "https://example.com/high/video.m3u8")
        self.assertIn("未提供 RESOLUTION", logs)
        self.assertIn("退回按带宽选择", logs)

    def test_invalid_spec_fails_at_construction(self):
        """格式非法在构造下载器时即失败, 不必等到下载阶段."""
        with self.assertRaises(ValueError):
            M3U8Downloader(
                DownloadConfig(m3u8_source=MASTER_URL, resolution="abc"),
                http_client=_FakeHTTPClient(),
                hooks=[],
            )

    def test_select_variant_by_limit_unit(self):
        """直接对变体列表调用: 只限高度 / 双限 / 无候选报错."""
        variants = [
            M3U8Variant(uri="a", bandwidth=100, width=640, height=360, resolution="640x360"),
            M3U8Variant(uri="b", bandwidth=200, width=854, height=480, resolution="854x480"),
            M3U8Variant(uri="c", bandwidth=300, width=1280, height=720, resolution="1280x720"),
        ]
        downloader = M3U8Downloader(
            DownloadConfig(m3u8_source=MASTER_URL), http_client=_FakeHTTPClient(), hooks=[]
        )
        self.assertEqual(downloader._select_variant_by_limit(variants, (None, 480)).uri, "b")
        self.assertEqual(downloader._select_variant_by_limit(variants, (854, 480)).uri, "b")
        self.assertEqual(downloader._select_variant_by_limit(variants, (1280, 720)).uri, "c")
        with self.assertRaises(M3U8DownloadError):
            downloader._select_variant_by_limit(variants, (640, 240))


# ---------------------------------------------------------------------------
# API 契约
# ---------------------------------------------------------------------------

# (输入, 期望原因片段) —— None 表示合法; 与 PARSE/INVALID 表同源, 只是断言的是中文原因
CASES: list[tuple["str | None", "str | None"]] = [
    (None, None),
    ("", None),
    ("   ", None),
    ("480p", None),
    ("720P", None),
    ("854x480", None),
    (" 854X480 ", None),
    ("480", "格式无效"),
    ("0p", "格式无效"),
    ("854x", "格式无效"),
    ("abc", "格式无效"),
]


class _FakeTask:
    _seq = 0

    def __init__(self, req):
        _FakeTask._seq += 1
        self.id = f"fake{_FakeTask._seq:04d}"
        self.status = "pending"
        self.req = req


class _FakeManager:
    """替换真实 TaskManager: 只记录 create 调用, 避免测试触网 / 产生 downloads 目录."""

    def __init__(self):
        self.created: list = []

    def create(self, req):
        self.created.append(req)
        return _FakeTask(req)

    def registered_io_keys(self):
        return set(), set()


@unittest.skipIf(API_IMPORT_ERROR, f"无法导入 api_server: {API_IMPORT_ERROR}")
class ValidateResolutionLimitTest(unittest.TestCase):
    """纯函数行为: 合法 (含未指定) 返回 None, 非法返回原因片段."""

    def test_cases(self):
        for raw, expected in CASES:
            with self.subTest(raw=raw):
                reason = validate_resolution_limit(raw)
                if expected is None:
                    self.assertIsNone(reason, f"{raw!r} 应为合法, 实际: {reason}")
                else:
                    self.assertIsNotNone(reason, f"{raw!r} 应被拒绝")
                    self.assertIn(expected, reason)

    def test_none_input_is_valid(self):
        # 批量接口里 resolution 可能是 None, 不能抛异常
        self.assertIsNone(validate_resolution_limit(None))

    def test_message_matches_downloader(self):
        """API 侧原因文案与下载器一致 (同源: parse_resolution_limit)."""
        for raw, _ in INVALID_CASES:
            with self.subTest(raw=raw):
                try:
                    parse_resolution_limit(raw)
                    self.fail(f"{raw!r} 应被拒绝")
                except ValueError as e:
                    self.assertEqual(validate_resolution_limit(raw), str(e))


@unittest.skipIf(API_IMPORT_ERROR, f"无法导入 api_server: {API_IMPORT_ERROR}")
class DownloadEndpointResolutionTest(unittest.TestCase):
    """两个入口的 resolution 校验契约 (校验必须在 create 之前)."""

    def setUp(self):
        self.client = TestClient(api_server.app)
        self.fake = _FakeManager()
        self._patch = mock.patch.object(api_server, "manager", self.fake)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_single_rejects_bad_resolution_with_400(self):
        for bad in ("480", "0p", "854x", "abc"):
            with self.subTest(bad=bad):
                r = self.client.post("/api/download", json={
                    "m3u8_input": "https://example.com/1.m3u8",
                    "output": "a.ts",
                    "resolution": bad,
                })
                self.assertEqual(r.status_code, 400)
                self.assertIn("格式无效", r.json()["detail"])
        self.assertEqual(self.fake.created, [], "校验失败时不应创建任务")

    def test_single_accepts_resolution_and_passes_it_through(self):
        r = self.client.post("/api/download", json={
            "m3u8_input": "https://example.com/1.m3u8",
            "output": "a.ts",
            "resolution": "480p",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fake.created[0].resolution, "480p")

    def test_single_without_resolution_still_works(self):
        r = self.client.post("/api/download", json={
            "m3u8_input": "https://example.com/1.m3u8", "output": "a.ts",
        })
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.fake.created[0].resolution)

    def test_batch_reports_bad_resolution_per_line(self):
        r = self.client.post("/api/download/batch", json={"tasks": [
            {"m3u8_input": "https://example.com/1.m3u8", "upload": "a.ts",
             "resolution": "480p", "line": 1},
            {"m3u8_input": "https://example.com/2.m3u8", "upload": "b.ts",
             "resolution": "0p", "line": 2},
            {"m3u8_input": "https://example.com/3.m3u8", "upload": "c.ts", "line": 3},
        ]})
        self.assertEqual(r.status_code, 200, "单行非法不应让整批 422")
        body = r.json()
        self.assertEqual([e["line"] for e in body["errors"]], [2])
        self.assertIn("格式无效", body["errors"][0]["reason"])
        self.assertEqual(body["errors"][0]["input"], "https://example.com/2.m3u8")
        self.assertEqual([c["upload"] for c in body["created"]], ["a.ts", "c.ts"])
        self.assertEqual(body["skipped"], [])


if __name__ == "__main__":
    unittest.main()
