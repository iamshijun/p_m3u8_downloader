# 02 任务卡片内联显示错误文本

Status: resolved
Type: task

## 问题

`web/js/components/taskCard.js` 的 `applySnapshot()` 不读 `snap.error`；
`web/js/pages/tasksPage.js` 的 `refresh()` 只对 `status === "running"` 的任务
`subscribe()`。在 `refresh()` 之前就失败的任务（缺 token、预检失败）永远不会订阅事件流，
卡片因此只有 `error` 徽章，错误文本彻底丢失。

## 需求

- 卡片新增内联错误块 `.task-error`，`applySnapshot()` 在 `status === "error"` 时填充
- 幂等：同一文本只追加一行日志，轮询/刷新不刷屏；错误清空时隐藏该块
- 事件流路径（`eventStream.js` 的 `status` → `error`）走同一个出口，避免与快照路径重复
- `web/css/components.css` 加 `.task-error` 样式（用 `--err` token）

## 验收

- 秒失败的任务刷新列表后卡片内联显示原因，展开「日志」能看到同一行
- 重复 `refresh()` 不产生重复日志行

## Comments

已实现并验证（`_fail_task()` / `taskCard.setError()`），回归用例见 `tests/test_task_failure_logging.py`。
