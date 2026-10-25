// NDJSON 进度流订阅
import { openTaskStream } from "../api/client.js";
import { humanSize } from "../utils/dom.js";
import { taskCard } from "./taskCard.js";

// 单个任务的订阅句柄: { aborter, streaming }
const subs = new Map();

// 任意订阅关闭时的回调 (任务进入终态/流异常/手动取消)
// 由 tasksPage 注册, 用于在订阅关闭后主动拉取任务列表,
// 把已经从 pending 转入 running 的任务补订上 stream
let onStreamClosed = null;
export function setOnStreamClosed(fn) {
  onStreamClosed = fn;
}

// 状态变更回调, 用于通知外部更新任务统计总览
let onStatusChange = null;
export function setOnStatusChange(fn) {
  onStatusChange = fn;
}

function isHandledEvent(ev) {
  if (!ev || typeof ev !== "object") return false;
  return ["connected", "start", "playlist", "segment",
          "all_downloaded", "status", "heartbeat",
          "preflight", "preflight_retry", "preflight_ok"].includes(ev.type);
}

// 预检事件里的配额信息 → 可读文本 (字段缺失时返回空串)
function quotaText(ev) {
  if (ev.total == null) return "";
  const used = ev.used != null ? humanSize(ev.used) : "?";
  return ` (网盘已用 ${used} / 总 ${humanSize(ev.total)})`;
}

function dispatch(t, ev) {
  switch (ev.type) {
    case "connected":
      break;
    case "start":
      taskCard.log(t.id, "任务启动 → " + ev.output);
      break;
    case "playlist":
      t.encEl.textContent = ev.encrypted ? "AES-128" : "否";
      t.bar.style.width = "0%";
      t.bar.textContent = "0%";
      t.doneEl.textContent = "0";
      t.totalEl.textContent = ev.total;
      taskCard.log(t.id, `解析完成: ${ev.total} 片段, 加密=${ev.encrypted}`);
      break;
    case "segment":
      t.bar.style.width = (ev.done / ev.total * 100).toFixed(1) + "%";
      t.bar.textContent = (ev.done / ev.total * 100).toFixed(1) + "%";
      t.doneEl.textContent = ev.done;
      t.totalEl.textContent = ev.total;
      if (ev.done === 1 || ev.done % 25 === 0 || ev.done === ev.total)
        taskCard.log(t.id, `片段 ${ev.done}/${ev.total}`);
      break;
    case "all_downloaded":
      taskCard.log(t.id, "全部下载完成, 合并/收尾...", "l-ok");
      break;
    case "status":
      taskCard.setStatus(t.id, ev.status);
      taskCard.setTimes(t.id, ev.started_at, ev.finished_at, ev.status);
      if (ev.status === "done") {
        taskCard.log(t.id, "✔ 完成: " + (ev.result || "") + " (" + humanSize(ev.size) + ")", "l-ok");
        if (ev.result) { t.resultWrap.style.display = ""; t.resultEl.textContent = ev.result; }
        if (ev.size != null) t.sizeEl.textContent = humanSize(ev.size);
      } else if (ev.status === "error") {
        // 内联错误块 + 日志行统一由 taskCard.setError 输出 (幂等)
        taskCard.setError(t.id, ev.message || "未知错误");
      } else if (ev.status === "cancelled") {
        taskCard.log(t.id, "已取消 (" + (ev.reason || "") + ")", "l-warn");
      } else if (ev.status === "running") {
        taskCard.log(t.id, "开始运行");
      }
      if (onStatusChange) onStatusChange(ev.status);
      break;
    case "heartbeat":
      break;
    case "preflight":
      taskCard.log(t.id, `上传预检中... (${ev.backend || "yunpan"} → ${ev.upload || ""})`);
      break;
    case "preflight_retry":
      taskCard.log(
        t.id,
        `预检失败: ${ev.message || ""} → ${ev.delay}s 后重试 (${ev.attempt}/${ev.total})`,
        "l-warn",
      );
      break;
    case "preflight_ok":
      taskCard.log(t.id, "✔ 上传预检通过" + quotaText(ev), "l-ok");
      break;
  }
}

// 订阅任务: 若已订阅则跳过, 返回是否新建了订阅
export function subscribe(id) {
  if (subs.has(id)) return false;
  const t = taskCard.get(id);
  if (!t) return false;

  const handle = { aborter: null, streaming: true };
  subs.set(id, handle);
  handle.aborter = new AbortController();

  (async () => {
    try {
      const resp = await openTaskStream(id);
      handle.streaming = true;
      const { responsePromise } = resp;
      const realResp = await responsePromise;
      if (!realResp.ok) {
        taskCard.log(id, "订阅失败 HTTP " + realResp.status, "l-err");
        return;
      }
      const reader = realResp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, idx).trim();
          buffer = buffer.slice(idx + 1);
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line); } catch { continue; }
          if (isHandledEvent(ev)) dispatch(t, ev);
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") taskCard.log(id, "流异常: " + e.message, "l-err");
    } finally {
      handle.streaming = false;
      subs.delete(id);
      if (onStreamClosed) {
        try { onStreamClosed(id); } catch { /* 回调异常不影响订阅清理 */ }
      }
    }
  })();

  return true;
}

export function unsubscribe(id) {
  const h = subs.get(id);
  if (h && h.aborter) h.aborter.abort();
  subs.delete(id);
}

export function isSubscribed(id) {
  return subs.has(id);
}
