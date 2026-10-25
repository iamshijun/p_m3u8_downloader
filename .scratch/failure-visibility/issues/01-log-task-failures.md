# 01 任务失败写入服务端日志

Status: resolved
Type: task

## 问题

`api_server.TaskManager._run()` 的失败分支只做 `task.error = ...` + `set_status(ERROR)`，
没有任何 `log()` 调用。于是 `run/server.log` 里查不到任务失败原因 —— 用户只能看到
「日志也没有具体的错误日志」。

## 需求

- 新增统一的失败出口（模块级函数 `_fail_task`），一次性做三件事：
  写 `task.error`、`log()` 出原因（含 task id 和 stage）、`set_status(ERROR, ...)`
- 意外异常（`except Exception`）额外 `log(traceback.format_exc())`
- `str(e)` 为空时退回异常类型名，避免前端出现空白错误文本
- 覆盖 `_run()` 里全部 5 个失败出口：no_disk 缺 upload、缺 token、预检失败、
  `SystemExit`、未预期异常

## 验收

- `tests/test_task_failure_logging.py`：缺 token → 日志含 task id 与原因；
  未预期异常 → 日志含 `Traceback` 与异常类型；空消息异常 → `task.error` 不空

## Comments

已实现并验证（`_fail_task()` / `taskCard.setError()`），回归用例见 `tests/test_task_failure_logging.py`。
