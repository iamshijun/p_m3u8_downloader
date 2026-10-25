# 02 报错文案与文档写清「token 优先级 / .env 仅兜底」

Status: resolved
Type: task

## 问题

原文案 `upload 需要 upload.token 或 .env ACCESS_TOKEN` 把兜底值写成了并列的必需品，
用户读完的结论是「必须配置 ACCESS_TOKEN」。

## 变更

- `api_server._run()`：`缺少上传 token: 请求未提供 upload.token, 且 .env 未配置 ACCESS_TOKEN (ACCESS_TOKEN 仅作兜底)`（常量 `UPLOAD_TOKEN_HINT`，与创建前的 400 detail 共用同一句）
- `api_server` 模块 docstring：`ACCESS_TOKEN  上传 token 兜底值: 仅当任务未提供 upload.token 时使用`
- `m3u8_downloader` CLI：`启用 --upload 但未提供 token: 请用 --upload-token 指定, 或写入 .env 的 ACCESS_TOKEN 作为兜底`
- `.env.example` / `CLAUDE.md` 配置表 / `web/index.html` 的 token input placeholder：统一口径
- `tests/test_task_failure_logging.py` 与 `tests/test_upload_token_priority.py` 锁住文案与优先级
