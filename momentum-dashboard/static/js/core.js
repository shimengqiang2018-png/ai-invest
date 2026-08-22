import { $, $$, esc, trace } from "./utils.js";

/* ============================================================
 * 全局状态
 * ========================================================== */

const state = {
  pools: null,
  activeTab: "overview",
  signals: null,
  overview: null,
  backtest: null,
  audit: null,
  screener: null,
  etfScan: null,
  positions: null,
  enumData: null,
  kline: new Map(),
  realtimeTimer: null,
  signalsItems: null,
  signalsSelected: null,
  signalsRt: null,
  signalsSort: { key: null, dir: "desc" },
  parseResult: null,
  parseImages: [],
  gridConfigImages: [],
  gridParseImages: [],
  dbOffset: 0,
};

let toastTimer = null;

function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " err" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 3600);
}

let loadingTick = null;

function setLoading(on, text = "加载中…") {
  const box = $("#loading");
  box.hidden = !on;
  const el = $("#loading-text");
  el.textContent = text;
  clearInterval(loadingTick);
  if (on) {
    const t0 = Date.now();
    loadingTick = setInterval(() => {
      const secs = Math.round((Date.now() - t0) / 1000);
      if (secs >= 5) {
        el.textContent =
          `${text}… 已等待 ${secs}s（首次联网刷新可能需要 1-3 分钟）`;
      }
    }, 1000);
  }
}

async function api(path, { quiet = false, progress = null, timeoutMs = 0, method = "GET", body = null } = {}) {
  // 默认超时：任何请求都不能无限等待（服务重启/网络断开时页面才不会卡死）
  if (!timeoutMs) timeoutMs = quiet ? 45000 : 90000;
  const ctrl = timeoutMs > 0 ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
  if (!quiet) setLoading(true, progress || "请求数据…");
  try {
    const res = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl ? ctrl.signal : undefined,
    });
    let payload;
    try {
      payload = await res.json();
    } catch {
      throw new Error("服务返回了无法解析的内容");
    }
    if (!res.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${res.status}`);
    return payload;
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw new Error("等待超时（请求仍在后台处理，可稍后刷新查看结果）");
    }
    trace(`api error ${path}: ${err && err.message ? err.message : String(err)}`);
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
    if (!quiet) setLoading(false);
  }
}

function setServerStatus(ok, text) {
  const dot = $("#serverDot");
  dot.className = "dot " + (ok ? "ok" : "err");
  $("#serverStatus").textContent = text;
}

function renderFatalError(err) {
  setLoading(false);
  setServerStatus(false, "初始化失败");
  $("#dataAsOf").textContent = "加载失败: " + (err && err.message ? err.message : String(err));
  let box = $("#fatalBox");
  if (!box) {
    box = document.createElement("div");
    box.id = "fatalBox";
    box.className = "fatal-box";
    document.querySelector(".main").prepend(box);
  }
  box.innerHTML = `
    <div class="fatal-title">页面初始化失败</div>
    <div class="fatal-msg">${esc(err && err.message ? err.message : String(err))}</div>
    <button class="btn" id="fatalReload">重新加载</button>`;
  $("#fatalReload").addEventListener("click", () => location.reload());
}

window.addEventListener("error", (ev) => {
  const msg = ev && ev.error && ev.error.message ? ev.error.message : (ev.message || "未知脚本错误");
  trace("window error: " + msg);
  renderFatalError(new Error(msg));
});
window.addEventListener("unhandledrejection", (ev) => {
  const reason = ev && ev.reason;
  trace("unhandled rejection: " + (reason && reason.message ? reason.message : String(reason)));
  renderFatalError(reason instanceof Error ? reason : new Error(String(reason)));
});

function applyEnvelope(body) {
  if (body.cached) {
    const badge = $("#cacheBadge");
    badge.hidden = false;
    badge.textContent = body.stale ? "使用旧缓存（网络失败）" : "已缓存";
    badge.className = "badge" + (body.stale ? " stale" : "");
  } else {
    $("#cacheBadge").hidden = true;
  }
  if (body.server_time) {
    const t = new Date(body.server_time);
    $("#lastUpdate").textContent = "更新于 " + t.toLocaleString("zh-CN");
  }
  return body.data;
}

/* ============================================================
 * 页面切换
 * ========================================================== */

const PAGE_TITLES = {
  overview: "总览",
  signals: "信号扫描",
  grid: "网格",
  backtest: "回测分析",
  screener: "动量选品池",
  "grid-screener": "网格选品池",
  positions: "持仓",
  db: "数据存储",
  audit: "风险审计",
};

function switchTab(name) {
  if (state.realtimeTimer) {
    clearInterval(state.realtimeTimer);
    state.realtimeTimer = null;
  }
  trace("action: 切换页面 " + name);
  state.activeTab = name;
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  $("#pageTitle").textContent = PAGE_TITLES[name];
  $("#cacheBadge").hidden = true;
  history.replaceState(null, "", "#" + name);
  renderTab(name);
}

/* 各页面渲染器注册表：由 main.js 统一注册，避免 core 反向依赖业务模块 */
const TAB_RENDERERS = {};

export function registerTabRenderer(name, render) {
  TAB_RENDERERS[name] = render;
}

async function renderTab(name, force = false) {
  const render = TAB_RENDERERS[name];
  if (!render) return;
  try {
    await render(force);
  } catch (err) {
    toast("加载失败: " + err.message, true);
    setServerStatus(false, "接口异常");
  }
}

export {
  PAGE_TITLES,
  api,
  applyEnvelope,
  renderFatalError,
  renderTab,
  setLoading,
  setServerStatus,
  state,
  switchTab,
  toast,
};
