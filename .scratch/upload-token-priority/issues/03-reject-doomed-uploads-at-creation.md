# 03 注定失败的上传前置条件改为创建前拦截

Status: resolved
Type: task

## 问题

缺 token / `no_disk` 缺 upload 这两类组合在 `_run()` 里是**必然失败**的（不触网也立刻报错），
但 `/api/download` 和 `/api/download/batch` 都会先把任务建进注册表 —— 于是批量导入出现
「一堆任务秒变 error」，任务列表被注定失败的记录污染（修好可见性后才看得见原因）。

## 变更

- 新增 `api_server.validate_task_upload(upload, *, no_disk=False) -> Optional[str]`，
  作为这两类前置条件的**唯一定义**；失败原因抽成常量 `UPLOAD_TOKEN_HINT` / `NO_DISK_NEEDS_UPLOAD`，
  保证「创建前 400 的 detail」与「`_run()` 兜底的 `task.error`」是同一句话
- `/api/download`：校验失败 → 400（中文 detail），不创建任务
- `/api/download/batch`：先构造 `DownloadRequest`（pydantic 校验后），再逐行校验 → 该行进 `errors`，
  同批其他行照常创建；错误顺序不变（缺 URL / 缺文件名 / 参数非法仍在前）
- `_run()`：在建目录之前统一调一次 `validate_task_upload()` 兜底（直接 `manager.create()` 的场景），
  删掉两处内联的重复判断
- `tests/test_m3u8_source_validation.py` / `tests/test_resolution_limit.py` 的批量用例固定注入
  兜底 token，避免新的前置校验遮蔽它们本来要测的行为（URL / 清晰度 / 去重）

## 验证

- 用例表 `VALIDATE_CASES`（10 组：是否上传 × no_disk × task token × env token）
- `UploadEndpointValidationTest`：单条 400（缺 token、no_disk 缺 upload）、env 兜底放行、
  批量逐行 errors + 合法行照常 created、env 兜底批量全放行
- 手动 E2E：批量导入 1 行缺 token → `errors[0].line == 1`，另一行带 token 正常创建；
  单条缺 token → 400，`/api/tasks` 不新增记录
- 全量回归 63 例通过
