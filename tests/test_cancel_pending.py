"""取消全部 pending 任务的回归用例.

覆盖两层:
  1. TaskManager.cancel_pending() —— 只取消排队中的任务, running / 终态不受影响
  2. POST /api/tasks/cancel-pending 的契约 —— 返回 {cancelled: [id], count}

测试用假 executor 顶替 ThreadPoolExecutor, 任务永远不会真正执行 (不触网、不写 downloads/).

在仓库根目录运行 (需要 requirements.txt 里的 fastapi/pydantic):

    .venv/bin/python -m unittest tests.test_cancel_pending -v
"""

from __future__ import annotations

import unittest
from unittest import mock

try:
    from fastapi.testclient import TestClient

    import api_server
    from api_server import DownloadRequest, TaskManager, TaskStatus

    API_IMPORT_ERROR = ""
except Exception as e:  # pragma: no cover - 缺依赖时整文件跳过
    API_IMPORT_ERROR = f"{type(e).__name__}: {e}"


class _FakeFuture:
    def __init__(self) -> None:
        self.cancel_calls = 0

    def cancel(self) -> bool:
        self.cancel_calls += 1
        return True


class _NoRunExecutor:
    """submit 只记录不执行: 任务保持 pending, 由用例手动推进状态."""

    def __init__(self) -> None:
        self.futures: list[_FakeFuture] = []

    def submit(self, fn, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        fut = _FakeFuture()
        self.futures.append(fut)
        return fut


@unittest.skipIf(API_IMPORT_ERROR, f"无法导入 api_server: {API_IMPORT_ERROR}")
class CancelPendingTest(unittest.TestCase):
    def setUp(self):
        self.mgr = TaskManager(max_concurrent=1)
        self.executor = _NoRunExecutor()
        self.mgr._executor = self.executor  # 替换掉真实线程池, 避免任务真的跑起来

    def _create(self, name: str):
        return self.mgr.create(DownloadRequest(m3u8_input=f"https://example.com/{name}.m3u8"))

    def test_only_pending_tasks_are_cancelled(self):
        pending_a = self._create("a")
        pending_b = self._create("b")
        running = self._create("c")
        done = self._create("d")
        running.set_status(TaskStatus.RUNNING)
        done.set_status(TaskStatus.DONE)

        cancelled = self.mgr.cancel_pending()

        self.assertEqual(sorted(cancelled), sorted([pending_a.id, pending_b.id]))
        self.assertIs(pending_a.status, TaskStatus.CANCELLED)
        self.assertIs(pending_b.status, TaskStatus.CANCELLED)
        self.assertIs(running.status, TaskStatus.RUNNING, "running 任务不应被批量取消")
        self.assertIs(done.status, TaskStatus.DONE, "终态任务不应被批量取消")

    def test_idempotent_when_no_pending(self):
        running = self._create("a")
        running.set_status(TaskStatus.RUNNING)
        self.assertEqual(self.mgr.cancel_pending(), [])

    def test_running_tasks_are_left_to_single_cancel(self):
        running = self._create("a")
        running.set_status(TaskStatus.RUNNING)
        self.assertEqual(self.mgr.cancel_pending(), [])
        self.assertIs(running.status, TaskStatus.RUNNING)
        self.assertTrue(self.mgr.cancel(running.id), "单个取消仍应处理 running 任务")
        self.assertIs(running.status, TaskStatus.CANCELLED)


@unittest.skipIf(API_IMPORT_ERROR, f"无法导入 api_server: {API_IMPORT_ERROR}")
class CancelPendingEndpointTest(unittest.TestCase):
    """TestClient 契约: 批量取消接口的响应形状 + 不遮挡 /api/tasks/{id} 路由."""

    def setUp(self):
        self.client = TestClient(api_server.app)
        self.mgr = TaskManager(max_concurrent=1)
        self.mgr._executor = _NoRunExecutor()
        self._patch = mock.patch.object(api_server, "manager", self.mgr)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_endpoint_returns_cancelled_ids(self):
        a = self.mgr.create(DownloadRequest(m3u8_input="https://example.com/a.m3u8"))
        b = self.mgr.create(DownloadRequest(m3u8_input="https://example.com/b.m3u8"))
        b.set_status(TaskStatus.RUNNING)

        r = self.client.post("/api/tasks/cancel-pending")

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["cancelled"], [a.id])
        self.assertEqual(body["count"], 1)
        self.assertIs(a.status, TaskStatus.CANCELLED)
        self.assertIs(b.status, TaskStatus.RUNNING)
        self.assertEqual(
            self.client.get(f"/api/tasks/{b.id}").status_code, 200,
            "/api/tasks/cancel-pending 不应抢走 /api/tasks/{task_id} 路由",
        )

    def test_endpoint_on_empty_registry(self):
        r = self.client.post("/api/tasks/cancel-pending")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"cancelled": [], "count": 0})


if __name__ == "__main__":
    unittest.main()
