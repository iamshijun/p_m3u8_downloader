// 后端 API 封装: 统一 fetch, 错误转异常
//
// 路径解析: 基于 document.baseURI 派生, 兼容通过 nginx /<subpath>/ 反代部署。
// 即便当前页面从根路径访问, 派生结果仍是 /api/..., 行为不变。


async function jsonOrThrow(resp) {
  //resp - fetch返回的Response
  if (!resp.ok) {
    let detail = "";
    try { 
      detail = (await resp.json()).detail || ""; 
    } catch { /* ignore */ }
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

// 创建下载任务
export async function createTask(req) {
  const resp = await fetch("api/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  return jsonOrThrow(resp);
}

// 任务列表快照
export async function listTasks() {
  const resp = await fetch("api/tasks");
  return jsonOrThrow(resp);
}

// 健康检查
export async function health() {
  const resp = await fetch("api/health");
  return jsonOrThrow(resp);
}

// 取消任务 (尽力而为)
export async function cancelTask(id) {
  return fetch(`api/tasks/${id}/cancel`, { method: "POST" });
}

// 删除已结束的任务
export async function removeTask(id) {
  return fetch(`api/tasks/${id}`, { method: "DELETE" });
}

// 订阅进度流: 返回 { response, reader, abort }
// 调用方负责 reader.read() + 解析 NDJSON
export function openTaskStream(id) {
  const ac = new AbortController();
  const promise = fetch(`api/tasks/${id}/stream`, { signal: ac.signal });
  return {
    abort: () => ac.abort(),
    responsePromise: promise,
  };
}
