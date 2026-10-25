"""任务失败可见性的回归用例 (issue 01: 失败原因必须进服务端日志).

现象: 批量导入的任务秒变 error, 但 run/server.log 里什么都没有 ——
因为 _run() 的失败分支只写 task.error, 从不 log(). 这里锁住新契约:

  - 每个失败出口都输出 "[!] 任务 <id> 失败..." 且带上面向用户的原因
  - 未预期异常额外输出完整 traceback
  - str(e) 为空时, 任务错误文本退回异常类型名 (前端不会显示空白错误)

不触网、不写 downloads/: DOWNLOAD_ROOT 被替换成临时目录, 并直接用 __new__
构造 TaskManager (不启动线程池 / 清理线程) 调用 _run().

在仓库根目录运行 (需要 requirements.txt 里的 fastapi/pydantic):

    .venv/bin/python -m unittest tests.test_task_failure_logging -v
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

try:
    import api_server
    from api_server import DownloadRequest, Task, TaskStatus, UploadConfig

    API_IMPORT_ERROR = ""
except Exception as e:  # pragma: no cover - 缺依赖时整文件跳过
    API_IMPORT_ERROR = f"{type(e).__name__}: {e}"


@unittest.skipIf(API_IMPORT_ERROR, f"无法导入 api_server: {API_IMPORT_ERROR}")
class FailTaskLoggingTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(api_server, "DOWNLOAD_ROOT", tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        # __new__ 跳过 __init__: 只测 _run 的失败出口, 不需要线程池/清理线程
        self.mgr = api_server.TaskManager.__new__(api_server.TaskManager)

    def _task(self, **kwargs) -> Task:
        req = DownloadRequest(
            m3u8_input="https://example.com/a.m3u8", output="a.ts", **kwargs
        )
        return Task(id="0123456789ab", req=req)

    def _run_capturing(self, task: Task) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.dict(os.environ, {"ACCESS_TOKEN": ""}):
            self.mgr._run(task)
        return buf.getvalue()

    def test_missing_upload_token_is_logged(self):
        """缺 token 秒失败时, 日志里必须有 task id 和原因."""
        task = self._task(upload=UploadConfig(type="yunpan", path="电影/测试.mp4"))
        out = self._run_capturing(task)

        self.assertIs(task.status, TaskStatus.ERROR)
        self.assertIn("缺少上传 token", task.error)
        self.assertIn(task.id, out)
        self.assertIn(task.error, out)

    def test_unexpected_exception_logs_traceback(self):
        """未预期异常必须带 traceback, 否则排查只能靠猜."""
        task = self._task()
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(
            api_server.M3U8Downloader, "run", side_effect=RuntimeError("boom")
        ):
            self.mgr._run(task)
        out = buf.getvalue()

        self.assertIs(task.status, TaskStatus.ERROR)
        self.assertEqual(task.error, "boom")
        self.assertIn("Traceback", out)
        self.assertIn("RuntimeError: boom", out)
        self.assertIn(task.id, out)

    def test_empty_exception_message_falls_back_to_type_name(self):
        """无参异常的 str() 为空, 不能把空字符串当作前端错误文本."""
        task = self._task()
        with redirect_stdout(io.StringIO()), mock.patch.object(
            api_server.M3U8Downloader, "run", side_effect=ConnectionResetError()
        ):
            self.mgr._run(task)

        self.assertIs(task.status, TaskStatus.ERROR)
        self.assertIn("ConnectionResetError", task.error)

    def test_no_disk_without_upload_is_logged(self):
        """--no-disk 缺 upload 的早退分支同样要走统一日志出口."""
        task = self._task(no_disk=True)
        out = self._run_capturing(task)

        self.assertIs(task.status, TaskStatus.ERROR)
        self.assertIn("--no-disk 需要 --upload", task.error)
        self.assertIn(task.id, out)


if __name__ == "__main__":
    unittest.main()
