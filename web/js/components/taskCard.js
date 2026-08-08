// 任务卡片组件: 渲染 / 更新单个任务的所有 UI 状态
import { $, humanSize } from "../utils/dom.js";

export const TERMINAL = new Set(["done", "error", "cancelled"]);

// 已渲染任务: id -> card 状态对象
const tasks = {};

function ensureCard(snap) {
  let t = tasks[snap.id];
  if (t) return t;

  const card = document.createElement("div");
  card.className = "task-card";
  card.id = "task-" + snap.id;
  card.innerHTML = `
    <div class="task-head">
      <div class="task-title">
        <div class="in"></div>
        <div class="sub"></div>
      </div>
      <div class="task-ctl">
        <span class="badge status">pending</span>
        <button class="secondary small btn-cancel">取消</button>
        <button class="secondary small btn-remove" style="display:none">删除</button>
      </div>
    </div>
    <div class="progress-wrap"><div class="progress-bar">0%</div></div>
    <div class="stats">
      <div>片段: <b class="done">0</b>/<b class="total">0</b></div>
      <div>加密: <b class="enc">-</b></div>
      <div>大小: <b class="size">-</b></div>
      <div class="result-wrap" style="display:none">结果: <b class="result"></b></div>
    </div>
    <div class="times">
      <div>开始: <b class="started-at">-</b></div>
      <div>结束: <b class="finished-at">-</b></div>
      <div>耗时: <b class="elapsed">-</b></div>
    </div>
    <details><summary>日志</summary><div class="task-log"></div></details>
  `;
  $("taskList").prepend(card);
  $("emptyHint").style.display = "none";

  t = {
    id: snap.id,
    card,
    bar: card.querySelector(".progress-bar"),
    statusEl: card.querySelector(".status"),
    doneEl: card.querySelector(".done"),
    totalEl: card.querySelector(".total"),
    encEl: card.querySelector(".enc"),
    sizeEl: card.querySelector(".size"),
    resultWrap: card.querySelector(".result-wrap"),
    resultEl: card.querySelector(".result"),
    startedAtEl: card.querySelector(".started-at"),
    finishedAtEl: card.querySelector(".finished-at"),
    elapsedEl: card.querySelector(".elapsed"),
    logEl: card.querySelector(".task-log"),
    btnCancel: card.querySelector(".btn-cancel"),
    btnRemove: card.querySelector(".btn-remove"),
  };
  card.querySelector(".task-title .in").textContent = snap.input || snap.id;
  card.querySelector(".task-title .sub").textContent =
    (snap.upload || snap.output || "") + "  ·  #" + snap.id;

  tasks[snap.id] = t;
  return t;
}

function tlog(t, msg, cls = "") {
  const line = document.createElement("div");
  line.className = cls;
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  t.logEl.appendChild(line);
  t.logEl.scrollTop = t.logEl.scrollHeight;
}

function setStatusBadge(t, status) {
  t.statusEl.textContent = status;
  t.statusEl.className = "badge status " + status;
  const terminal = TERMINAL.has(status);
  t.btnCancel.style.display = terminal ? "none" : "";
  t.btnRemove.style.display = terminal ? "" : "none";
}

function setProgress(t, done, total) {
  const p = total > 0 ? (done / total * 100) : 0;
  t.bar.style.width = p.toFixed(1) + "%";
  t.bar.textContent = p.toFixed(1) + "%";
  t.doneEl.textContent = done;
  t.totalEl.textContent = total;
}

function fmtTime(ts) {
  if (ts == null) return "-";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtElapsed(sec) {
  if (sec == null || sec < 0) return "-";
  if (sec < 1) return (sec * 1000).toFixed(0) + " ms";
  if (sec < 60) return sec.toFixed(1) + " 秒";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  if (m < 60) return `${m}分${s}秒`;
  const h = Math.floor(m / 60);
  return `${h}小时${m % 60}分${s}秒`;
}

function startElapsedTicker(t, startedAt) {
  if (t._elapsedTimer) {
    if (t._elapsedTimer.startedAt === startedAt) return;
    clearInterval(t._elapsedTimer.id);
  }
  t.elapsedEl.classList.add("live");
  const id = setInterval(() => {
    if (t.card.isConnected) {
      t.elapsedEl.textContent = fmtElapsed(Date.now() / 1000 - startedAt);
    }
  }, 1000);
  t._elapsedTimer = { id, startedAt };
}

function stopElapsedTicker(t, finishedAt = null) {
  if (t._elapsedTimer) {
    clearInterval(t._elapsedTimer.id);
    t._elapsedTimer = null;
    if (finishedAt == null) t.finishedAtEl.textContent = fmtTime(Date.now());
  }
  if (t.elapsedEl) t.elapsedEl.classList.remove("live");
}

function setTimes(t, snap) {
  if (snap.started_at != null) t.startedAtEl.textContent = fmtTime(snap.started_at);
  if (snap.finished_at != null) t.finishedAtEl.textContent = fmtTime(snap.finished_at);
  if (snap.started_at != null) {
    if (snap.status === "running") {
      // 进行中: 启动 1s 刷新, 直接读浏览器本地时间避免时钟偏差
      startElapsedTicker(t, snap.started_at);
    } else {
      // 已结束: 静态显示
      stopElapsedTicker(t, snap.finished_at);
      if (snap.finished_at != null) {
        t.elapsedEl.textContent = fmtElapsed(snap.finished_at - snap.started_at);
      }
    }
  } else {
    stopElapsedTicker(t);
  }
}

function applySnapshot(t, snap) {
  setStatusBadge(t, snap.status);
  setProgress(t, snap.done || 0, snap.total || 0);
  if (snap.encrypted != null) t.encEl.textContent = snap.encrypted ? "AES-128" : "否";
  if (snap.size != null) t.sizeEl.textContent = humanSize(snap.size);
  if (snap.result) { t.resultWrap.style.display = ""; t.resultEl.textContent = snap.result; }
  setTimes(t, snap);
}

// 公共 API
export const taskCard = {
  get(id) { return tasks[id]; },
  all() { return Object.keys(tasks); },

  ensure(snap) { return ensureCard(snap); },
  apply(snap) { applySnapshot(ensureCard(snap), snap); },

  on(id, ev) { return tlog(ensureCard({ id }), ev); },
  log(id, msg, cls) { return tlog(tasks[id], msg, cls); },
  setStatus(id, status) { return setStatusBadge(tasks[id], status); },
  setProgress(id, done, total) { return setProgress(tasks[id], done, total); },

  // 绑定取消/删除按钮回调 (仅首次, 避免重复绑定)
  bindControls(id, onCancel, onRemove) {
    const t = tasks[id];
    if (!t || t._controlsBound) return;
    t.btnCancel.addEventListener("click", () => onCancel(id));
    t.btnRemove.addEventListener("click", () => onRemove(id));
    t._controlsBound = true;
  },

  remove(id) {
    const t = tasks[id];
    if (t) { stopElapsedTicker(t); t.card.remove(); delete tasks[id]; }
    if (Object.keys(tasks).length === 0) $("emptyHint").style.display = "";
  },

  has(id) { return !!tasks[id]; },
  isTerminal(id) { return TERMINAL.has(tasks[id].statusEl.textContent); },

  stopElapsedTicker(id) {
    const t = tasks[id];
    if (t) stopElapsedTicker(t);
  },

  setTimes(id, started_at, finished_at, status) {
    const t = tasks[id];
    if (!t) return;
    if (started_at != null) {
      t.startedAtEl.textContent = fmtTime(started_at);
      // 进行中: 启动 1s 刷新的实时计时器
      if (status === "running") {
        startElapsedTicker(t, started_at);
      }
    }
    if (finished_at != null) {
      t.finishedAtEl.textContent = fmtTime(finished_at);
    }
    if (status !== "running") {
      stopElapsedTicker(t, finished_at);
      // 修正计时
      if (finished_at != null && started_at != null) {
        t.elapsedEl.textContent = fmtElapsed(finished_at - started_at);
      }
    }
  },
};
