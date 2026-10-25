"""web/ 静态资源的缓存契约: 必须带 ``Cache-Control: no-cache`` (可缓存但每次校验).

背景: 改了前端 JS/Tab 里已打开的页面仍跑旧代码 —— ES 模块只在页面加载时拉一次,
而 StaticFiles 默认不发 Cache-Control, 浏览器按启发式规则 (约 last-modified 至今的
10%) 直接复用缓存. 现象就是"前端改了但页面还是旧行为", 很容易误判成后端没生效.

契约:
  - 静态资源 (/, /js/**, /css/**) 都带 ``Cache-Control: no-cache``
  - 不是 no-store: ETag / Last-Modified 仍在, 内容未变时回源得到 304 (开销极小)

在仓库根目录运行 (需要 requirements.txt 里的 fastapi/pydantic):

    .venv/bin/python -m unittest tests.test_static_assets -v
"""

from __future__ import annotations

import unittest

try:
    from fastapi.testclient import TestClient

    import api_server

    API_IMPORT_ERROR = ""
except Exception as e:  # pragma: no cover - 缺依赖时整文件跳过
    API_IMPORT_ERROR = f"{type(e).__name__}: {e}"


@unittest.skipIf(API_IMPORT_ERROR, f"无法导入 api_server: {API_IMPORT_ERROR}")
class StaticAssetCacheTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api_server.app)

    def test_assets_send_no_cache(self):
        for path in (
            "/",
            "/js/main.js",
            "/js/pages/tasksPage.js",
            "/js/components/taskCard.js",
            "/css/components.css",
        ):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200, f"{path} 应可访问")
                self.assertEqual(r.headers.get("cache-control"), "no-cache")

    def test_unchanged_asset_still_returns_304(self):
        """no-cache 不是 no-store: 校验器还在, 未改动时不重复传输内容."""
        first = self.client.get("/js/pages/tasksPage.js")
        etag = first.headers.get("etag")
        self.assertTrue(etag or first.headers.get("last-modified"),
                        "静态资源应保留 ETag / Last-Modified")
        again = self.client.get("/js/pages/tasksPage.js", headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)


if __name__ == "__main__":
    unittest.main()
