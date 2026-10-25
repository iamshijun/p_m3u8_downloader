"""上传 token 优先级 / 上传前置条件校验的回归用例.

设计契约:
  1. 任务自带的 `upload.token` (前端表单 / API 请求) 永远优先;
     `.env` 的 `ACCESS_TOKEN` **只是兜底**, 仅在任务没有提供 token 时使用 ——
     既不能被解读成"启用上传就必须配置 ACCESS_TOKEN", 也不能被前端在批量导入时丢掉;
  2. 必然失败的上传前置条件 (`缺 token` / `no_disk 缺 upload`) 在**创建任务前**就拦下:
     单条 400, 批量逐行进 errors, 不制造"注定失败的任务记录".

覆盖三层:
  - `_run()` 里的 token 选择 (_UploadTokenPriorityTest) + 文案
  - `api_server.validate_task_upload()` 用例表 (_ValidateTaskUploadTest)
  - 两个入口的契约 (_UploadEndpointValidationTest)

在仓库根目录运行 (需要 requirements.txt 里的 fastapi/pydantic):

    .venv/bin/python -m unittest tests.test_upload_token_priority -v
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

try:
    from fastapi.testclient import TestClient

    import api_server
    from api_server import (
        NO_DISK_NEEDS_UPLOAD,
        UPLOAD_TOKEN_HINT,
        DownloadRequest,
        Task,
        TaskStatus,
        UploadConfig,
        validate_task_upload,
    )

    API_IMPORT_ERROR = ""
except Exception as e:  # pragma: no cover - 缺依赖时整文件跳过
    API_IMPORT_ERROR = f"{type(e).__name__}: {e}"


def _upload(token=None) -> "UploadConfig":
    return UploadConfig(type="yunpan", path="电影/a.mp4", token=token)


def _make_task(token=None) -> "Task":
    return Task(id="0123456789ab", req=DownloadRequest(
        m3u8_input="https://example.com/a.m3u8",
        output="a.ts",
        upload=_upload(token),
    ))


# (upload, no_disk, ACCESS_TOKEN, 期望原因) —— 期望 None 表示合法
VALIDATE_CASES = [
    # -- 不上传 ---------------------------------------------------------------
    (None, False, "", None),
    (None, False, "env-token", None),
    # -- no_disk 必须配合上传 --------------------------------------------------
    (None, True, "", NO_DISK_NEEDS_UPLOAD),
    (None, True, "env-token", NO_DISK_NEEDS_UPLOAD),
    # -- 任务自带 token: 与 .env 无关 ---------------------------------------
    (_upload("task-token"), False, "", None),
    (_upload("task-token"), True, "", None),
    # -- 任务无 token: 回退 .env 兜底 ---------------------------------------
    (_upload(None), False, "env-token", None),
    (_upload(None), True, "env-token", None),
    # -- 两边都没有: 必然失败 -------------------------------------------------
    (_upload(None), False, "", UPLOAD_TOKEN_HINT),
    (_upload(None), True, "", UPLOAD_TOKEN_HINT),
]


@unittest.skipIf(API_IMPORT_ERROR, f"无法导入 api_server: {API_IMPORT_ERROR}")
class UploadTokenPriorityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(api_server, "DOWNLOAD_ROOT", tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        # __new__ 跳过 __init__: 不需要线程池 / 清理线程
        self.mgr = api_server.TaskManager.__new__(api_server.TaskManager)

    def _task(self, token=None) -> Task:
        return _make_task(token)

    def _run(self, task: Task) -> mock.MagicMock:
        """跑一次 _run, 返回记录下来的 _run_preflight mock (用它看实际使用的 token).

        预检通过后下载器被 mock 成立即失败, 避免触网/落盘.
        """
        preflight = mock.MagicMock(return_value={})
        with redirect_stdout(io.StringIO()), \
                mock.patch.object(api_server, "_run_preflight", preflight), \
                mock.patch.object(api_server, "create_upload_hook", return_value=object()), \
                mock.patch.object(api_server.M3U8Downloader, "run", side_effect=RuntimeError("boom")):
            self.mgr._run(task)
        return preflight

    def test_task_token_wins_over_env_fallback(self):
        """前端指定了 token 时, .env 的 ACCESS_TOKEN 不能覆盖它."""
        task = self._task(token="frontend-token")
        with mock.patch.dict(os.environ, {"ACCESS_TOKEN": "env-token"}):
            preflight = self._run(task)

        self.assertIs(task.status, TaskStatus.ERROR)  # 仅因 mock 的 run 抛错
        self.assertEqual(preflight.call_args.args[1], "frontend-token")

    def test_env_token_used_only_when_task_has_none(self):
        """前端没给 token 时才回退到 .env 的 ACCESS_TOKEN."""
        task = self._task(token=None)
        with mock.patch.dict(os.environ, {"ACCESS_TOKEN": "env-token"}):
            preflight = self._run(task)

        self.assertEqual(preflight.call_args.args[1], "env-token")

    def test_missing_token_message_names_both_sources(self):
        """两边都没有时, 报错文案要同时点出 upload.token / ACCESS_TOKEN 和兜底语义."""
        task = self._task(token=None)
        with mock.patch.dict(os.environ, {"ACCESS_TOKEN": ""}):
            preflight = self._run(task)

        preflight.assert_not_called()
        self.assertIs(task.status, TaskStatus.ERROR)
        self.assertIn("upload.token", task.error)
        self.assertIn("ACCESS_TOKEN", task.error)
        self.assertIn("兜底", task.error)


@unittest.skipIf(API_IMPORT_ERROR, f"无法导入 api_server: {API_IMPORT_ERROR}")
class ValidateTaskUploadTest(unittest.TestCase):
    """纯函数用例表: 合法返回 None, 否则返回可读原因 (与 _run 共用同一定义)."""

    def test_cases(self):
        for upload, no_disk, env_token, expected in VALIDATE_CASES:
            with self.subTest(upload=upload, no_disk=no_disk, env=env_token):
                with mock.patch.dict(os.environ, {"ACCESS_TOKEN": env_token}):
                    reason = validate_task_upload(upload, no_disk=no_disk)
                if expected is None:
                    self.assertIsNone(reason)
                else:
                    self.assertEqual(reason, expected)

    def test_missing_token_message_is_shared_with_run(self):
        """创建前的拦截文案与 _run 兜底文案必须是同一份 (用户看得到的那句)."""
        task = _make_task(None)
        mgr = api_server.TaskManager.__new__(api_server.TaskManager)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        buf = io.StringIO()
        with mock.patch.object(api_server, "DOWNLOAD_ROOT", tmp.name), \
                mock.patch.dict(os.environ, {"ACCESS_TOKEN": ""}), redirect_stdout(buf):
            mgr._run(task)
        self.assertEqual(task.error, UPLOAD_TOKEN_HINT)
        self.assertIn(UPLOAD_TOKEN_HINT, buf.getvalue())


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
    """替换真实 TaskManager: 只记录 create, 避免测试触网 / 写 downloads/."""

    def __init__(self):
        self.created: list = []

    def create(self, req):
        self.created.append(req)
        return _FakeTask(req)

    def registered_io_keys(self):
        return set(), set()


@unittest.skipIf(API_IMPORT_ERROR, f"无法导入 api_server: {API_IMPORT_ERROR}")
class UploadEndpointValidationTest(unittest.TestCase):
    """两个入口的契约: 必然失败的上传前置条件在 create 之前就被拦下."""

    def setUp(self):
        self.client = TestClient(api_server.app)
        self.fake = _FakeManager()
        patch = mock.patch.object(api_server, "manager", self.fake)
        patch.start()
        self.addCleanup(patch.stop)

    def _env(self, token: str):
        env = mock.patch.dict(os.environ, {"ACCESS_TOKEN": token})
        env.start()
        self.addCleanup(env.stop)

    # -- 单条 ----------------------------------------------------------------

    def test_single_missing_token_is_400(self):
        self._env("")
        r = self.client.post("/api/download", json={
            "m3u8_input": "https://example.com/1.m3u8", "output": "a.ts",
            "upload": {"type": "yunpan", "path": "电影/a.mp4"},
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("缺少上传 token", r.json()["detail"])
        self.assertEqual(self.fake.created, [], "校验失败时不应创建任务")

    def test_single_env_fallback_is_accepted(self):
        self._env("env-token")
        r = self.client.post("/api/download", json={
            "m3u8_input": "https://example.com/1.m3u8", "output": "a.ts",
            "upload": {"type": "yunpan", "path": "电影/a.mp4"},
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.fake.created), 1)

    def test_single_no_disk_without_upload_is_400(self):
        self._env("")
        r = self.client.post("/api/download", json={
            "m3u8_input": "https://example.com/1.m3u8", "output": "a.ts", "no_disk": True,
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], NO_DISK_NEEDS_UPLOAD)
        self.assertEqual(self.fake.created, [])

    # -- 批量 ----------------------------------------------------------------

    def test_batch_reports_upload_errors_per_line(self):
        self._env("")
        r = self.client.post("/api/download/batch", json={"tasks": [
            {"m3u8_input": "https://example.com/1.m3u8", "upload": "a.ts", "line": 1},
            # 批量导入的行必须带文件名 (即 upload.path), 所以 no_disk 缺 upload 这种
            # 组合在批量入口会先被「缺少文件名」拦下; 单独覆盖见单条入口用例.
            {"m3u8_input": "https://example.com/3.m3u8", "line": 3,
             "upload": {"type": "yunpan", "path": "c.ts", "token": "task-token"}},
        ]})
        self.assertEqual(r.status_code, 200, "单行非法不应让整批 422")
        body = r.json()
        reasons = {e["line"]: e["reason"] for e in body["errors"]}
        self.assertEqual(reasons[1], UPLOAD_TOKEN_HINT)
        self.assertEqual([c["upload"] for c in body["created"]], ["c.ts"])
        self.assertIsNotNone(body["created"][0]["task_id"])
        # 回显 token 来源, 前端用它告诉用户"你填的 token 到底用上了没"
        self.assertEqual(body["created"][0]["token_source"], "task")

    def test_batch_env_fallback_creates_all_lines(self):
        self._env("env-token")
        r = self.client.post("/api/download/batch", json={"tasks": [
            {"m3u8_input": "https://example.com/1.m3u8", "upload": "a.ts", "line": 1},
            {"m3u8_input": "https://example.com/2.m3u8", "upload": "b.ts", "line": 2},
        ]})
        body = r.json()
        self.assertEqual(body["errors"], [])
        self.assertEqual(len(body["created"]), 2)
        self.assertEqual([c["token_source"] for c in body["created"]], ["env", "env"])


if __name__ == "__main__":
    unittest.main()
