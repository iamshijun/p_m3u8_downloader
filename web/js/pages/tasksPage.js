// 业务编排: 表单收集 + 任务操作 (添加/取消/删除/刷新/清除)
import { $, escapeHtml } from "../utils/dom.js";
import { saveForm } from "../utils/storage.js";
import { validateM3u8Source, validateResolutionLimit } from "../utils/validate.js";
import { createTask, createBatchTasks, listTasks, cancelTask, cancelPendingTasks, removeTask } from "../api/client.js";
import { taskCard } from "../components/taskCard.js";
import { subscribe, unsubscribe, isSubscribed, setOnStreamClosed, setOnStatusChange } from "../components/eventStream.js";

// 已知任务状态缓存 (id -> status), 用来判断是否需要轮询
const knownStatuses = new Map();
let pollTimer = null;
const POLL_INTERVAL_MS = 2000;

// 更新顶部任务统计总览
function updateSummary() {
  let running = 0, pending = 0, total = 0;
  for (const id of taskCard.all()) {
    total++;
    const s = knownStatuses.get(id) || (taskCard.get(id)?.statusEl?.textContent);
    if (s === "running") running++;
    else if (s === "pending") pending++;
  }
  $("sumRunning").textContent = running;
  $("sumPending").textContent = pending;
  $("sumTotal").textContent = total;
}

// 根据当前任务状态决定是否启动/停止轮询:
// - 有 pending 但没有任何 running 订阅: 开启轮询, 等 pending 转为 running 后补订 stream
// - 有 running 订阅: stream 关闭时会主动触发 refresh, 不需要轮询
// - 都没有 (全终态): 停掉轮询
function syncPoll() {
  let hasRunning = false;
  let hasPending = false;
  for (const s of knownStatuses.values()) {
    if (s === "running") hasRunning = true;
    else if (s === "pending") hasPending = true;
  }
  if (hasPending && !hasRunning) {
    if (!pollTimer) pollTimer = setInterval(refresh, POLL_INTERVAL_MS);
  } else if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

// 上传选项 (不含 path): path 决定"是否上传"、以及批量导入时每行的远程文件名,
// 其余选项 (token/ondup/重试/删分片) 一律跟着表单走.
// token 优先级: 这里的值 (前端) > .env 的 ACCESS_TOKEN (仅兜底)
function collectUploadOptions() {
  return {
    type: "yunpan",
    token: $("upload_token").value.trim() || null,
    ondup: $("upload_ondup").value,
    max_retries: parseInt($("upload_retries").value) || 5,
    delete_after_upload: $("upload_delete").checked,
  };
}

function collectRequest(overrides = {}) {
  const headersRaw = $("headers").value.trim();
  const headers = headersRaw
    ? headersRaw.split(/\r?\n/).map(s => s.trim()).filter(Boolean)
    : null;

  const req = {
    m3u8_input: $("m3u8_input").value.trim(),
    output: $("output").value.trim() || "output.ts",
    base_url: $("base_url").value.trim() || null,
    workers: parseInt($("workers").value) || 10,
    retries: parseInt($("retries").value) || 3,
    timeout: parseInt($("timeout").value) || 5,
    proxy: $("proxy").value.trim() || null,
    keep_temp: $("keep_temp").checked,
    headers: headers,
    key_param: $("key_param").value.trim() || null,
    segments: $("segments").value.trim() || null,
    resolution: $("resolution").value.trim() || null,
    no_merge: $("no_merge").checked,
    no_disk: $("no_disk").checked,
  };
  const uploadPath = $("upload").value.trim();
  if (uploadPath) {
    // path 决定"是否上传": 远程目录留空则本次任务不上传
    req.upload = Object.assign(collectUploadOptions(), { path: uploadPath });
  }
  return Object.assign(req, overrides);
}

async function addTask() {
  const req = collectRequest();
  if (!req.m3u8_input) { alert("请输入 M3U8 URL 或路径"); return; }
  const urlError = validateM3u8Source(req.m3u8_input);
  if (urlError) {
    alert("M3U8 地址无效: " + urlError);
    $("m3u8_input").focus();
    return;
  }
  const resError = validateResolutionLimit(req.resolution);
  if (resError) {
    alert("清晰度上限无效: " + resError);
    $("resolution").focus();
    return;
  }

  $("btnAdd").disabled = true;
  try {
    const { task_id } = await createTask(req);
    saveForm();  // 提交成功后记住本次表单
    taskCard.ensure({
      id: task_id, input: req.m3u8_input,
      output: req.output,
      upload: req.upload ? req.upload.path : null,
      status: "pending",
    });
    taskCard.bindControls(task_id, doCancel, doRemove);
    // 不直接订阅: pending 任务不浪费 stream 连接,
    // 由下一轮 refresh (立即或轮询) 在任务转 running 后补订
    knownStatuses.set(task_id, "pending");
    updateSummary();
    syncPoll();
    refresh();  // 立即拉一次, 大多数情况下任务此刻已经 running
  } catch (e) {
    alert("请求异常: " + e.message);
  } finally {
    $("btnAdd").disabled = false;
  }
}

async function doCancel(id) {
  if (!confirm("确定要取消吗?")) return;
  try {
    // 后端 cancel() 会立即把状态推进到 cancelled (若非终态),
    // 并在响应里返回最新 status. 直接用响应里的 status 更新卡片,
    // 不必再发一次 listTasks. pending 任务无 stream 订阅者, 也能
    // 立即看到终态; running 任务 stream 仍开着, 后续进度会覆盖 UI.
    const { status } = await cancelTask(id);
    if (!status) return;
    taskCard.setStatus(id, status);
    knownStatuses.set(id, status);
    updateSummary();
    syncPoll();
  } catch (e) { /* ignore */ }
}

async function doRemove(id) {
  unsubscribe(id);
  try {
    const resp = await removeTask(id);
    if (!resp.ok) { alert("删除失败(任务未结束?)"); return; }
  } catch (e) { return; }
  taskCard.remove(id);
  updateSummary();
}

export async function refresh() {
  try {
    const { tasks: list } = await listTasks();
    for (const snap of list) {
      taskCard.apply(snap);
      taskCard.bindControls(snap.id, doCancel, doRemove);
      knownStatuses.set(snap.id, snap.status);
      // 只订阅 running 任务: pending 任务的 stream 只会空闲等待,
      // 等任务真正转 running 后再补订即可
      if (snap.status === "running" && !isSubscribed(snap.id)) {
        subscribe(snap.id);
      }
    }
    syncPoll();
    updateSummary();
  } catch (e) { /* ignore */ }
}

// 任意 stream 关闭 (任务进入终态 / 流异常 / 主动取消) 时主动拉一次列表,
// 把已经从 pending 转入 running 的任务补订上 stream
setOnStreamClosed(() => { refresh(); });

async function clearDone() {
  for (const id of taskCard.all().slice()) {
    if (taskCard.isTerminal(id)) await doRemove(id);
  }
  updateSummary();
}

// ---- 等待中 (pending) 任务的两项批量操作: 导出 / 全部取消 ----

// 拉一次最新的任务列表; 失败时提示并返回 null
async function fetchTasksOrAlert() {
  try {
    const { tasks = [] } = await listTasks();
    return tasks;
  } catch (e) {
    alert("获取任务列表失败: " + e.message);
    return null;
  }
}

// 单行导出文本: 与批量导入格式一致 ("文件名 URL")
// 文件名优先用上传路径, 没配上传时退回输出文件名; 空白折叠为 _ 以免破坏两段式格式
function pendingLine(t) {
  const name = String(t.upload || t.output || "").trim().replace(/\s+/g, "_") || "output.ts";
  return `${name} ${t.input}`;
}

// 导出当前 pending 任务, 下载为 .txt (可直接用批量导入的"选择 .txt 文件"导回)
async function exportPending() {
  const list = await fetchTasksOrAlert();
  if (!list) return;
  const lines = list.filter(t => t.status === "pending").map(pendingLine);
  if (!lines.length) { alert("没有等待中的任务"); return; }

  const blob = new Blob([lines.join("\n") + "\n"], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `pending-${Date.now()}.txt`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// 取消所有排队中的任务 (running / 已终态不受影响), 由后端一次性处理
async function cancelAllPending() {
  const list = await fetchTasksOrAlert();
  if (!list) return;
  const pending = list.filter(t => t.status === "pending");
  if (!pending.length) { alert("没有等待中的任务"); return; }
  if (!confirm(`确定要取消 ${pending.length} 个等待中的任务吗?`)) return;

  const btn = $("btnCancelPending");
  btn.disabled = true;
  try {
    const { cancelled = [] } = await cancelPendingTasks();
    for (const id of cancelled) {
      if (taskCard.has(id)) taskCard.setStatus(id, "cancelled");
      knownStatuses.set(id, "cancelled");
    }
    refresh();  // 未渲染的卡片走一次列表刷新补齐终态
  } catch (e) {
    alert("批量取消失败: " + e.message);
  } finally {
    btn.disabled = false;
    updateSummary();
    syncPoll();
  }
}

// ---- 批量导入 ----

// 解析导入文本: 每行 "文件名 URL" (空白分隔), 格式/URL 不合法计入 errors
// 有任何 errors 时 importTasks 不会提交请求 (必须先由用户修正)
function parseImportText(text) {
  const candidates = [];
  const errors = [];
  const lines = text.split(/\r?\n/);
  lines.forEach((raw, idx) => {
    const line = idx + 1;
    const trimmed = raw.trim();
    if (!trimmed) return;  // 空行跳过
    const parts = trimmed.split(/\s+/);
    if (parts.length !== 2) {
      errors.push({ line, reason: "格式应为: 文件名 URL", text: trimmed });
      return;
    }
    const [upload, url] = parts;
    const urlError = validateM3u8Source(url);
    if (urlError) {
      errors.push({ line, field: "url", reason: urlError, text: trimmed });
      return;
    }
    candidates.push({ line, upload, m3u8_input: url });
  });
  return { candidates, errors };
}

// 更新弹窗中的有效/错误统计 + 错误明细, 并按校验结果开关"导入"按钮
function updateImportPreview() {
  const { candidates, errors } = parseImportText($("importText").value);
  const bits = [];
  if (candidates.length) {
    bits.push(candidates.length + " 条有效");
    // 批量导入的每行都会走上传 (远程路径 = 行首文件名): 提前告知 token 从哪来,
    // 表单没填就只剩 .env 的 ACCESS_TOKEN 兜底 (没配则每行都会失败).
    bits.push($("upload_token").value.trim() ? "token: 表单" : "token: 仅 .env 兜底");
  }
  if (errors.length) bits.push(errors.length + " 条校验未通过");
  $("importPreview").textContent = bits.length ? bits.join("，") : "";
  const errBox = $("importErrors");
  if (errors.length) {
    errBox.innerHTML = errors.slice(0, 30)
      .map(e => `<div>第 ${e.line} 行: ${escapeHtml(e.reason)}`
        + (e.text ? ` — <code>${escapeHtml(e.text)}</code>` : "") + `</div>`).join("")
      + (errors.length > 30 ? `<div>... 另有 ${errors.length - 30} 条</div>` : "");
    errBox.style.display = "";
  } else {
    errBox.innerHTML = "";
    errBox.style.display = "none";
  }
  // 有校验错误时禁用导入按钮, 修好后自动恢复
  $("btnImportOk").disabled = errors.length > 0;
  $("btnImportOk").title = errors.length ? "请先修正校验未通过的行" : "";
  return { candidates, errors };
}

function loadImportFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    $("importText").value = String(reader.result || "");
    updateImportPreview();
  };
  reader.readAsText(file);
}

function openImportModal() {
  $("importResult").style.display = "none";
  $("importResult").innerHTML = "";
  updateImportPreview();
  $("importModal").style.display = "flex";
  $("importText").focus();
}

function closeImportModal() {
  $("importModal").style.display = "none";
  $("importFile").value = "";
}

async function importTasks() {
  const { candidates, errors } = updateImportPreview();
  // 硬闸: 只要有校验错误就不请求接口, 先让用户修正
  if (errors.length) {
    alert(`有 ${errors.length} 行校验未通过，请先修正后再导入`);
    $("importText").focus();
    return;
  }
  if (!candidates.length) { alert("没有可导入的有效任务"); return; }

  // 合并当前表单配置 + 每行解析出的 文件名/URL
  const shared = collectRequest();
  // 表单级参数 (清晰度上限) 不在逐行校验范围内, 这里单独把关
  const resError = validateResolutionLimit(shared.resolution);
  if (resError) {
    alert("清晰度上限无效: " + resError);
    closeImportModal();
    $("resolution").focus();
    return;
  }
  const tasks = candidates.map(c => {
    const task = Object.assign({}, shared);
    task.m3u8_input = c.m3u8_input;
    task.line = c.line;
    // 保留表单中的上传配置 (token/ondup/max_retries/delete_after_upload),
    // 仅将 path 替换为导入的文件名.
    // 每次都用 collectUploadOptions() 重新读表单 (不用 shared.upload 继承),
    // 避免"远程目录留空时 shared 里没有 upload" / "继承了旧对象" 这类丢 token 的坑;
    // 表单真没填 token 时交给后端用 .env 的 ACCESS_TOKEN 兜底.
    if (c.upload) {
      task.upload = Object.assign({}, collectUploadOptions(), { path: c.upload });
    }
    return task;
  });

  $("btnImportOk").disabled = true;
  try {
    const { created = [], skipped = [], errors = [] } = await createBatchTasks(tasks);
    const out = $("importResult");
    const lines = [];
    lines.push(`<div class="ok">成功创建 ${created.length} 个任务</div>`);
    // 回显服务端实际使用的 token 来源: 表单/请求 > .env 兜底. 直接告诉你
    // "我填的 token 用上了没", 不用再去挖请求体.
    const sources = [...new Set(created.map(c => c.token_source).filter(Boolean))];
    if (sources.length) {
      const label = sources.map(s => s === "task" ? "表单/请求里的 token" : ".env 的 ACCESS_TOKEN (兜底)").join(" / ");
      lines.push(`<div class="hint" style="margin-top:4px">上传 token 来源: ${escapeHtml(label)}</div>`);
    }
    if (skipped.length) {
      lines.push(`<div class="warn">跳过 ${skipped.length} 个:</div>`);
      for (const s of skipped.slice(0, 50))
        lines.push(`<div class="warn">第 ${s.line} 行: ${s.reason} ${s.input || s.output || ""}</div>`);
    }
    if (errors.length) {
      lines.push(`<div class="err">失败 ${errors.length} 个:</div>`);
      for (const e of errors.slice(0, 50))
        lines.push(`<div class="err">第 ${e.line} 行: ${e.reason}</div>`);
    }
    out.innerHTML = lines.join("");
    out.style.display = "";
    saveForm();
    refresh();  // 渲染新任务卡片并订阅进度
  } catch (e) {
    alert("批量导入请求异常: " + e.message);
  } finally {
    $("btnImportOk").disabled = false;
  }
}

// 启动时绑定按钮事件
export function bindPage() {
  setOnStatusChange(updateSummary);
  $("btnAdd").addEventListener("click", addTask);
  $("btnRefresh").addEventListener("click", refresh);
  $("btnClearDone").addEventListener("click", clearDone);
  $("btnExportPending").addEventListener("click", exportPending);
  $("btnCancelPending").addEventListener("click", cancelAllPending);
  $("btnImport").addEventListener("click", openImportModal);
  $("btnImportCancel").addEventListener("click", closeImportModal);
  $("btnImportOk").addEventListener("click", importTasks);
  $("btnImportFile").addEventListener("click", () => $("importFile").click());
  $("importFile").addEventListener("change", (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) loadImportFile(f);
    e.target.value = "";  // 允许再次选择同一文件
  });
  $("importText").addEventListener("input", updateImportPreview);
  $("importModal").addEventListener("click", (e) => {
    if (e.target === $("importModal")) closeImportModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeImportModal();
  });
}
