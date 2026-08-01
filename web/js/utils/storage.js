// 表单记忆 (localStorage)
import { $ } from "./dom.js";

// 需要持久化的表单字段 id
export const FORM_FIELDS = [
  "m3u8_input", "output", "base_url", 
  //下载相关配置 
  "workers", "retries", "timeout", "proxy", "key_param", "headers",
  "no_merge", "keep_temp", "no_disk",
  //upload相关
  "upload", "upload_token", "upload_ondup", "upload_retries", "upload_delete",
];

const FORM_STORE_KEY = "m3u8_last_form";

// 读取单个字段的值 (兼容 checkbox / 普通输入)
function getFieldValue(id) {
  const el = $(id);
  if (!el) return undefined;
  return el.type === "checkbox" ? el.checked : el.value;
}

// 写入单个字段的值
function setFieldValue(id, value) {
  const el = $(id);
  if (!el || value === undefined || value === null) return;
  if (el.type === "checkbox") {
    el.checked = !!value;
  } else {
    el.value = value;
  }
}

// 保存当前表单到 localStorage
export function saveForm() {
  const data = {};
  for (const id of FORM_FIELDS) {
    const v = getFieldValue(id);
    if (v !== undefined) data[id] = v;
  }
  try { 
    localStorage.setItem(FORM_STORE_KEY, JSON.stringify(data)); 
  } catch (e) { /* 隐私模式/超配额时忽略 */ }
}

// 从 localStorage 恢复表单
export function restoreForm() {
  let data;
  try { 
    data = JSON.parse(localStorage.getItem(FORM_STORE_KEY) || "null"); 
  }catch (e) { 
    return; 
  }
  if (!data) return;
  for (const id of FORM_FIELDS) {
    setFieldValue(id, data[id]);
  }
}
