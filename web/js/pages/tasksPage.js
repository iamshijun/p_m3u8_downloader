// 业务编排: 表单收集 + 任务操作 (添加/取消/删除/刷新/清除)
import { $ } from "../utils/dom.js";
import { saveForm } from "../utils/storage.js";
import { createTask, listTasks, cancelTask, removeTask } from "../api/client.js";
import { taskCard, TERMINAL } from "../components/taskCard.js";
import { subscribe, unsubscribe } from "../components/eventStream.js";

function collectRequest() {
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
  return req;
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

// 启动时绑定按钮事件
export function bindPage() {
  $("btnAdd").addEventListener("click", addTask);
  $("btnRefresh").addEventListener("click", refresh);
  $("btnClearDone").addEventListener("click", clearDone);
}
