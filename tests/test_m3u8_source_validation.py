"""m3u8 来源校验 (URL / 本地路径) 的回归用例.

覆盖两层:
  1. api_server.validate_m3u8_source()  —— 纯函数, 规则本体
  2. POST /api/download 与 /api/download/batch 的契约 —— 单条 400, 批量逐行 errors

前端 web/js/utils/validate.js 的 validateM3u8Source() 是同一套语义的 JS 镜像,
下面的 CASE 表就是两边共同遵守的契约; 改规则时两边和本表要一起改.

在仓库根目录运行 (需要 requirements.txt 里的 fastapi/pydantic):

    .venv/bin/python -m unittest
"""

from __future__ import annotations

import unittest
from unittest import mock

try:
    from fastapi.testclient import TestClient

    import api_server
    from api_server import validate_m3u8_source

    API_IMPORT_ERROR = ""
except Exception as e:  # pragma: no cover - 缺依赖时整文件跳过
    API_IMPORT_ERROR = f"{type(e).__name__}: {e}"


# (输入, 期望结果) —— 期望 None 表示合法; 期望字符串表示错误原因中必须包含的片段
CASES: list[tuple[str, "str | None"]] = [
    # -- 协议白名单: hhttps 这类拼写错误 / 非 http(s) 协议一律拒绝 -------------
    ("hhttps://example.com/1.m3u8", "协议不支持: hhttps://"),
    ("htps://example.com/1.m3u8", "协议不支持: htps://"),
    ("httpx://example.com/1.m3u8", "协议不支持: httpx://"),
    ("ftp://example.com/1.m3u8", "协议不支持: ftp://"),
    ("file:///tmp/a.m3u8", "协议不支持: file://"),
    ("javascript://alert(1)", "协议不支持: javascript://"),
    # -- 大小写: 合法 scheme 不区分大小写 -------------------------------------
    ("HTTPS://Example.com/1.m3u8", None),
    ("Http://example.com/1.m3u8", None),
    # -- 缺 // 的残缺写法 -----------------------------------------------------
    ("http:/example.com/1.m3u8", "缺少 //"),
    ("https:example.com/1.m3u8", "缺少 //"),
    # -- 空主机名 -------------------------------------------------------------
    ("https://", "缺少主机名"),
    ("https:///p.m3u8", "缺少主机名"),
    # -- URL 里的空白 (本地路径允许, 见下) ------------------------------------
    ("http://exa mple.com/1.m3u8", "空白字符"),
    # -- 合法 URL -------------------------------------------------------------
    ("https://example.com/1.m3u8", None),
    ("http://example.com/1.m3u8", None),
    ("https://example.com:8443/a/b/1.m3u8?token=x&sign=y", None),
    ("http://127.0.0.1:8011/playlist.m3u8", None),
    ("https://example.com", None),
    # -- 无 scheme: 一律视为本地路径, 语法放行 --------------------------------
    ("a.m3u8", None),
    ("./a.m3u8", None),
    ("../x.m3u8", None),
    ("/data/a.m3u8", None),
    ("/data/my video/a.m3u8", None),  # 本地路径允许含空格
    ("/data/a:b/c.m3u8", None),       # 冒号不算 scheme
    (r"C:\videos\a.m3u8", None),      # Windows 盘符不是 scheme
    ("example.com/1.m3u8", None),     # 漏写协议: 按本地路径放行 (已知取舍)
    # -- 空输入 ---------------------------------------------------------------
    ("", "缺少 URL"),
    ("   ", "缺少 URL"),
    ("\t\n", "缺少 URL"),
]


@unittest.skipIf(API_IMPORT_ERROR, f"无法导入 api_server: {API_IMPORT_ERROR}")
class ValidateM3u8SourceTest(unittest.TestCase):
    """纯函数行为: 合法返回 None, 非法返回原因片段."""

    def test_cases(self):
        for raw, expected in CASES:
            with self.subTest(raw=raw):
                reason = validate_m3u8_source(raw)
                if expected is None:
                    self.assertIsNone(reason, f"{raw!r} 应为合法, 实际: {reason}")
                else:
                    self.assertIsNotNone(reason, f"{raw!r} 应被拒绝")
                    self.assertIn(expected, reason)

    def test_strips_surrounding_whitespace(self):
        self.assertIsNone(validate_m3u8_source("  https://example.com/1.m3u8  "))
        self.assertIn("协议不支持", validate_m3u8_source("  hhttps://example.com/1.m3u8  ") or "")

    def test_none_input_is_missing_url(self):
        # 批量接口里 m3u8_input 可能是 None, 不能抛异常
        self.assertEqual(validate_m3u8_source(None), "缺少 URL")


# ---------------------------------------------------------------------------
# API 契约
# ---------------------------------------------------------------------------


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
class DownloadEndpointValidationTest(unittest.TestCase):
    """通过 TestClient 验证两个入口的校验契约 (校验必须在 create 之前)."""

    def setUp(self):
        self.client = TestClient(api_server.app)
        self.fake = _FakeManager()
        self._patch = mock.patch.object(api_server, "manager", self.fake)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    # -- 单条 ----------------------------------------------------------------

    def test_single_rejects_bad_scheme_with_400(self):
        for bad in ("hhttps://example.com/1.m3u8", "ftp://example.com/1.m3u8",
                    "https:///p.m3u8", "http:/example.com/1.m3u8", ""):
            with self.subTest(bad=bad):
                r = self.client.post("/api/download", json={"m3u8_input": bad, "output": "a.ts"})
                self.assertEqual(r.status_code, 400)
                self.assertIsInstance(r.json()["detail"], str)  # 可读字符串, 不是 pydantic 数组
        self.assertEqual(self.fake.created, [], "校验失败时不应创建任务")

    def test_single_accepts_url_and_local_path(self):
        for good in ("https://example.com/1.m3u8", "./local/a.m3u8"):
            with self.subTest(good=good):
                r = self.client.post("/api/download", json={"m3u8_input": good, "output": "a.ts"})
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.json()["task_id"])
        self.assertEqual(len(self.fake.created), 2)

    # -- 批量 ----------------------------------------------------------------

    def test_batch_reports_bad_url_per_line(self):
        r = self.client.post("/api/download/batch", json={"tasks": [
            {"m3u8_input": "hhttps://example.com/1.m3u8", "upload": "a.ts", "line": 1},
            {"m3u8_input": "https://example.com/2.m3u8", "upload": "b.ts", "line": 2},
            {"m3u8_input": "ftp://example.com/3.m3u8", "upload": "c.ts", "line": 3},
            {"m3u8_input": "./local/d.m3u8", "upload": "d.ts", "line": 4},
        ]})
        self.assertEqual(r.status_code, 200, "单行非法不应让整批 422")
        body = r.json()
        self.assertEqual([e["line"] for e in body["errors"]], [1, 3])
        self.assertIn("协议不支持: hhttps://", body["errors"][0]["reason"])
        self.assertEqual(body["errors"][0]["input"], "hhttps://example.com/1.m3u8")
        self.assertEqual([c["upload"] for c in body["created"]], ["b.ts", "d.ts"])
        self.assertEqual(body["skipped"], [])

    def test_batch_duplicates_are_skipped_not_errors(self):
        # 重复 (skipped) 不算校验错误: 前端据此判断"是否阻断导入", 不能被当成 error
        r = self.client.post("/api/download/batch", json={"tasks": [
            {"m3u8_input": "https://example.com/1.m3u8", "upload": "a.ts", "line": 1},
            {"m3u8_input": "https://example.com/1.m3u8", "upload": "b.ts", "line": 2},
            {"m3u8_input": "https://example.com/3.m3u8", "upload": "a.ts", "line": 3},
        ]})
        body = r.json()
        self.assertEqual(body["errors"], [])
        self.assertEqual(len(body["created"]), 1)
        self.assertEqual([s["reason"] for s in body["skipped"]], ["URL 重复", "文件名重复"])

    def test_batch_keeps_existing_checks(self):
        """回归: 缺 URL / 缺文件名 / 参数非法 的原有行为不变."""
        r = self.client.post("/api/download/batch", json={"tasks": [
            {"m3u8_input": "", "upload": "a.ts", "line": 1},
            {"m3u8_input": "https://example.com/2.m3u8", "line": 2},
            {"m3u8_input": "https://example.com/3.m3u8", "upload": "c.ts",
             "workers": 999, "line": 3},
        ]})
        body = r.json()
        reasons = {e["line"]: e["reason"] for e in body["errors"]}
        self.assertEqual(reasons[1], "缺少 URL")
        self.assertEqual(reasons[2], "缺少文件名")
        self.assertIn("参数校验失败", reasons[3])
        self.assertEqual(body["created"], [])

    def test_batch_limit_still_reports_errors(self):
        with mock.patch.object(api_server, "BATCH_LIMIT", 2):
            r = self.client.post("/api/download/batch", json={"tasks": [
                {"m3u8_input": f"https://example.com/{i}.m3u8", "upload": f"{i}.ts", "line": i}
                for i in (1, 2, 3)
            ]})
        body = r.json()
        self.assertEqual(body["errors"],
                         [{"line": 3, "reason": "超出单次导入上限 2"}])
        self.assertEqual(len(body["created"]), 2)


if __name__ == "__main__":
    unittest.main()
