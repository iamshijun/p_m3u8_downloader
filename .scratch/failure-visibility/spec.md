# 失败可见性 (failure visibility)

## 背景

批量导入的任务会**立即**变成 `error`，但现象是「没有任何错误信息」：

- `run/server.log` 里只有 `HTTPClient.__init__` 打的 `[*] 自定义请求头` 一行，之后什么都没有
- 前端卡片只有 `error` 徽章，日志面板空白

复现出来的真实原因（与 Termux 无关）：批量导入的每一行都会带 `upload` 配置
（`tasksPage.js` 把行首「文件名」塞成 `upload.path`，`/api/download/batch` 也强制要求非空），
因此每个任务都会走 upload 前置检查；缺少 `upload.token` / `.env ACCESS_TOKEN` 时
`_run()` 在 `M3U8Downloader.run()` 之前就 `set_status(ERROR)` 了。

两者都是**可见性**缺陷，不改变下载行为：

1. `_run()` 的所有失败分支只写 `task.error`，从不调用 `log()` → 服务端日志无真相
2. `taskCard.applySnapshot()` 完全不读 `snap.error`，而 `refresh()` 只订阅 `running` 任务
   → 秒失败的任务永远订阅不到事件流，错误文本无处显示

## 范围 (本 spec)

只做可见性（issue 01 + 02）。**不做**：创建前就拦下缺 token 的批次、
「批量导入是否该强制 upload」的产品决策 —— 留作后续 issue。

## 验收

- 任务进入 `error` 时，`run/server.log` 出现带 task id 与原因的日志；意外异常附带 traceback
- 任务进入 `error` 时，卡片内联显示错误文本，且同一文本不会因轮询/刷新重复刷屏
- 不改变任何成功路径的行为
