// m3u8 来源校验: 与后端 api_server.validate_m3u8_source 保持同一语义
// - 带 scheme 的: 只接受 http/https 且必须有主机名 (hhttps:// / ftp:// 一律拒绝)
// - 不带 scheme 的: 视为本地文件路径, 语法放行 (存在性由服务端下载阶段判断)
//
// 用例表 (共同契约, 改规则时与后端一起改):
// tests/test_m3u8_source_validation.py

const SCHEME_RE = /^([A-Za-z][A-Za-z0-9+.-]*):\/\//;
const MALFORMED_HTTP_RE = /^https?:\S/i;

// 合法返回 null, 否则返回可读的中文原因
export function validateM3u8Source(raw) {
  const src = String(raw == null ? "" : raw).trim();
  if (!src) return "缺少 URL";

  const m = SCHEME_RE.exec(src);
  if (!m) {
    // 形如 https:/host / https:host 的残缺写法
    if (MALFORMED_HTTP_RE.test(src)) return "URL 格式错误: 缺少 // (应为 http:// 或 https://)";
    return null;  // 本地路径
  }

  const scheme = m[1];
  if (scheme.toLowerCase() !== "http" && scheme.toLowerCase() !== "https") {
    return `URL 协议不支持: ${scheme}:// (仅支持 http/https)`;
  }
  if (/\s/.test(src)) return "URL 不能包含空白字符";
  if (!hostOf(src)) return "URL 缺少主机名";
  return null;
}

// 取 scheme:// 之后到第一个 / ? # 之前的主机名
function hostOf(src) {
  const rest = src.replace(SCHEME_RE, "");
  return rest.split(/[/?#]/)[0].trim();
}

// 清晰度上限校验: 与后端 m3u8_downloader.parse_resolution_limit() /
// api_server.validate_resolution_limit() 保持同一语义
// - 空 / 未指定: 合法 (不限制, 选最高清晰度变体)
// - "480p" / "720P": 只限高度;  "854x480" / "854X480": 宽高双限
// - 其余一律拒绝 (0p / 854x / -480p / abc / 854x480x1 ...)
//
// 用例表 (共同契约, 改规则时与后端一起改):
// tests/test_resolution_limit.py

const RESOLUTION_DIM_RE = /^(\d+)x(\d+)$/;
const RESOLUTION_HEIGHT_RE = /^(\d+)p$/;

// 合法返回 null, 否则返回可读的中文原因
export function validateResolutionLimit(raw) {
  const spec = String(raw == null ? "" : raw).trim();
  if (!spec) return null;

  const text = spec.toLowerCase();
  let m = RESOLUTION_DIM_RE.exec(text);
  if (m) {
    if (Number(m[1]) > 0 && Number(m[2]) > 0) return null;
  } else {
    m = RESOLUTION_HEIGHT_RE.exec(text);
    if (m && Number(m[1]) > 0) return null;
  }
  return `清晰度上限格式无效: ${spec} (应为 480p 或 854x480)`;
}
