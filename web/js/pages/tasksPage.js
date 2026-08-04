// 业务编排: 表单收集 + 任务操作 (添加/取消/删除/刷新/清除)
import { $ } from "../utils/dom.js";
import { saveForm } from "../utils/storage.js";
import { createTask, createBatchTasks, listTasks, cancelTask, removeTask } from "../api/client.js";
import { taskCard, TERMINAL } from "../components/taskCard.js";
import { subscribe, unsubscribe } from "../components/eventStream.js";

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
    no_merge: $("no_merge").checked,
    no_disk: $("no_disk").checked,
  };
  const upload = $("upload").value.trim();
  if (upload) {
    req.upload = upload;
    req.upload_token = $("upload_token").value.trim() || null;
    req.upload_ondup = $("upload_ondup").value;
    req.upload_retries = parseInt($("upload_retries").value) || 5;
    req.upload_delete = $("upload_delete").checked;
  }
  return Object.assign(req, overrides);
}

async function addTask() {
  const req = collectRequest();
  if (!req.m3u8_input) { alert("请输入 M3U8 URL 或路径"); return; }

  $("btnAdd").disabled = true;
  try {
    const { task_id } = await createTask(req);
    saveForm();  // 提交成功后记住本次表单
    const t = taskCard.ensure({
      id: task_id, input: req.m3u8_input,
      output: req.output, upload: req.upload, status: "pending",
    });
    taskCard.bindControls(task_id, doCancel, doRemove);
    subscribe(task_id);
  } catch (e) {
    alert("请求异常: " + e.message);
  } finally {
    $("btnAdd").disabled = false;
  }
}

async function doCancel(id) {
  if (!confirm("确定要取消吗?")) return;
  try { await cancelTask(id); } catch (e) { /* ignore */ }
}

async function doRemove(id) {
  unsubscribe(id);
  try {
    const resp = await removeTask(id);
    if (!resp.ok) { alert("删除失败(任务未结束?)"); return; }
  } catch (e) { return; }
  taskCard.remove(id);
}

export async function refresh() {
  try {
    const { tasks: list } = await listTasks();
    for (const snap of list) {
      taskCard.apply(snap);
      taskCard.bindControls(snap.id, doCancel, doRemove);
      if (!TERMINAL.has(snap.status)) subscribe(snap.id);
    }
  } catch (e) { /* ignore */ }
}

async function clearDone() {
  for (const id of taskCard.all().slice()) {
    if (taskCard.isTerminal(id)) await doRemove(id);
  }
}

// ---- 批量导入 ----

// 解析导入文本: 每行 "文件名 URL" (空白分隔), 格式不合法计入 errors
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
    candidates.push({ line, upload, m3u8_input: url });
  });
  return { candidates, errors };
}

// 更新弹窗中的有效/错误统计 + 错误明细
function updateImportPreview() {
  const { candidates, errors } = parseImportText($("importText").value);
  const bits = [];
  if (candidates.length) bits.push(candidates.length + " 条有效");
  if (errors.length) bits.push(errors.length + " 条格式错误");
  $("importPreview").textContent = bits.length ? bits.join("，") : "";
  const errBox = $("importErrors");
  if (errors.length) {
    errBox.innerHTML = errors.slice(0, 30)
      .map(e => `<div>第 ${e.line} 行: ${e.reason}</div>`).join("");
    errBox.style.display = "";
  } else {
    errBox.innerHTML = "";
    errBox.style.display = "none";
  }
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
  const { candidates } = updateImportPreview();
  if (!candidates.length) { alert("没有可导入的有效任务"); return; }

  // 合并当前表单配置 + 每行解析出的 文件名/URL
  const shared = collectRequest();
  const tasks = candidates.map(c => Object.assign({}, shared, {
    m3u8_input: c.m3u8_input,
    upload: c.upload,
    line: c.line,
  }));

  $("btnImportOk").disabled = true;
  try {
    const { created = [], skipped = [], errors = [] } = await createBatchTasks(tasks);
    const out = $("importResult");
    const lines = [];
    lines.push(`<div class="ok">成功创建 ${created.length} 个任务</div>`);
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
  $("btnAdd").addEventListener("click", addTask);
  $("btnRefresh").addEventListener("click", refresh);
  $("btnClearDone").addEventListener("click", clearDone);
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
