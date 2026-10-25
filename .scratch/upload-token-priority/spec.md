# 上传 token 优先级 / 兜底语义 (upload token priority)

## 背景

用户在 Termux 上批量导入时看到「缺少上传 token」（原文案 `upload 需要 upload.token 或 .env ACCESS_TOKEN`），
但设计契约是：**前端填的 token 优先，`.env` 的 `ACCESS_TOKEN` 只是兜底**。

查出两个问题：

1. **前端把用户填的 token 丢了** —— `collectRequest()` 只在「远程目录」非空时才构造
   `req.upload`；批量导入时「远程目录」通常留空（远程路径由每行的「文件名」决定），于是
   `importTasks()` 里的 `task.upload` 不存在，走到 `else task.upload = {type, path}` 分支，
   表单里的 `upload_token` / `ondup` / `max_retries` / `delete_after_upload` 全部被静默丢弃。
   任务于是只能回退 `.env`，而 `.env` 没配 → 秒失败。
2. **文案把兜底值写成了必需品** —— 报错只说「需要 upload.token 或 .env ACCESS_TOKEN」，
   没有说明谁优先、`.env` 只是兜底，容易被读成「启用上传就必须配置 ACCESS_TOKEN」。

## 契约

- `upload.token`（请求 / 前端表单）> `.env` `ACCESS_TOKEN`（仅当任务未提供 token 时使用）
- `.env` 永远不会覆盖请求里明确指定的 token，也不是「必须配置」的项
- 缺 token 的报错必须同时点名两个来源 + 兜底语义
- 必然失败的上传前置条件（缺 token / `no_disk` 缺 upload）在**创建任务前**就拦下，不进入注册表

## 验收

- `tests/test_upload_token_priority.py`：任务 token 胜出 / 无 token 才用 env / 文案含两来源与「兜底」/
  `validate_task_upload()` 用例表 / 两个创建入口的 400 与逐行 `errors` 契约
- 批量导入时表单里的 token 会带进每个任务（远程目录留空也生效）
- README 级文档（CLAUDE.md / .env.example / web 表单 placeholder / CLI 提示）口径一致
