# 01 批量导入不要把表单里的 token 丢掉

Status: resolved
Type: bug

## 问题

`web/js/pages/tasksPage.js` 的 `collectRequest()` 仅在「远程目录」非空时构造 `req.upload`；
`importTasks()` 在 `task.upload` 为空时执行 `task.upload = { type: "yunpan", path: c.upload }`，
表单里的 `upload_token` / `ondup` / `max_retries` / `delete_after_upload` 被静默丢弃。
批量导入时「远程目录」通常留空（远程路径来自每行「文件名」），因此用户填的 token 100% 丢失，
任务只能回退 `.env` 的 `ACCESS_TOKEN`，未配置就秒失败。

## 变更

- 抽出 `collectUploadOptions()`（不含 `path`：token / ondup / max_retries / delete_after_upload）
- `collectRequest()` = `collectUploadOptions()` + `path`（远程目录决定「是否上传」）
- `importTasks()` = `Object.assign({}, task.upload || collectUploadOptions(), { path: c.upload })`
  —— 远程目录留空时也能把表单 token 带进每个任务

## 验证

node `--check` / 模块 import 通过；端到端：批量导入带 `upload.token` 的任务不再走
「缺少 token」分支，而是用请求里的 token 走预检（伪造 token → `上传预检失败: HTTP 400 …`）。
