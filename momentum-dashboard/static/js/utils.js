/* 启动轨迹上报：每一步都会写到 server.log，便于定位卡在哪一步 */
function trace(msg) {
  try {
    fetch("/api/trace?msg=" + encodeURIComponent(String(msg).slice(0, 300)), {
      keepalive: true,
    }).catch(() => {});
  } catch (err) {
    /* 忽略：trace 失败不影响主流程 */
  }
}
trace("前端模块已加载 (v1)");

/* ============================================================
 * 工具函数
 * ========================================================== */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(v) {
  return String(v ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtNum(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return Number(v).toLocaleString("zh-CN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

function fmtNum3(v) {
  return fmtNum(v, 3);
}

function fmtPct(v, d = 2, sign = false) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const prefix = sign && Number(v) > 0 ? "+" : "";
  return prefix + Number(v).toFixed(d) + "%";
}

function pctCls(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "";
  return Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "";
}

function shortName(name, code) {
  if (!name || name === code) return code;
  return name.length > 8 ? name.slice(0, 8) : name;
}

function statusMeta(status) {
  return {
    ok: ["正常", "ok"],
    provisional: ["盘中 / 非正式", "provisional"],
    no_signal: ["无信号", "no_signal"],
    unknown: ["异常", "unknown"],
  }[status] || [status || "—", "unknown"];
}

function strategyLabel(key) {
  return {
    momentum: "动量轮动",
    momentum_cash: "动量现金",
    grid: "网格核心",
    grid_observe: "网格观察",
    grid_sell_only: "网格只卖",
    base: "底仓",
    core: "网格核心",
    observe: "网格观察",
    sell_only: "网格只卖",
    reduce_base: "减仓后底仓",
    cash: "现金储备",
  }[key] || key || "其他";
}

const STRATEGY_COLORS = {
  动量轮动: "#4c9aff",
  网格核心: "#2ebd85",
  网格观察: "#f0b90b",
  网格只卖: "#f6465d",
  底仓: "#9d8cff",
  减仓后底仓: "#e58bff",
  动量现金: "#5f6c7d",
  现金储备: "#8b98a9",
};

function strColor(label) {
  return STRATEGY_COLORS[label] || "#8b98a9";
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      const comma = result.indexOf(",");
      resolve({
        name: file.name,
        mime: file.type || "image/jpeg",
        data_b64: comma >= 0 ? result.slice(comma + 1) : result,
      });
    };
    reader.onerror = () => reject(new Error(`读取图片失败: ${file.name}`));
    reader.readAsDataURL(file);
  });
}

const DEFAULT_COMBO_KEY = "momentum.defaultCombo";

function loadDefaultCombo() {
  try {
    const raw = localStorage.getItem(DEFAULT_COMBO_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (err) {
    return null;
  }
}

function saveDefaultCombo(combo) {
  try {
    localStorage.setItem(DEFAULT_COMBO_KEY, JSON.stringify(combo));
  } catch (err) {
    /* 忽略：localStorage 不可用时仅不记忆默认组合 */
  }
}

function setSelectValue(sel, value) {
  if (typeof sel === "string") sel = document.querySelector(sel);
  if (!sel || value === null || value === undefined) return;
  const v = String(value);
  if ([...sel.options].some((o) => o.value === v)) sel.value = v;
}

export {
  $,
  $$,
  STRATEGY_COLORS,
  esc,
  fileToBase64,
  fmtNum,
  fmtNum3,
  fmtPct,
  loadDefaultCombo,
  pctCls,
  saveDefaultCombo,
  setSelectValue,
  shortName,
  statusMeta,
  strColor,
  strategyLabel,
  trace,
};
