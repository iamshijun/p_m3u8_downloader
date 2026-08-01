// 入口: 恢复表单 + 健康检查 + 拉取任务列表 + 绑定页面
import { $ } from "./utils/dom.js";
import { restoreForm } from "./utils/storage.js";
import { health } from "./api/client.js";
import { refresh, bindPage } from "./pages/tasksPage.js";

async function loadHealth() {
  try {
    const d = await health();
    $("concBadge").textContent = "并发上限 " + d.max_concurrent;
  } catch { $("concBadge").textContent = "离线"; }
}

restoreForm();
bindPage();
loadHealth();
refresh();
