/* 动量轮动策略台 — 前端逻辑（零依赖，原生 JS + Canvas） */
"use strict";

import { $, $$, fileToBase64, saveDefaultCombo, trace } from "./utils.js";
import {
  PAGE_TITLES,
  registerTabRenderer,
  renderFatalError,
  renderTab,
  setServerStatus,
  state,
  switchTab,
  toast,
} from "./core.js";
import { redrawCurrent } from "./charts.js";
import { renderOverview } from "./overview.js";
import { renderSignals, sortSignals } from "./signals.js";
import {
  addGridTrigger,
  bindMetricHints,
  loadGridTriggers,
  parseGridConfigs,
  parseGridTriggers,
  renderGrid,
  renderGridConfigList,
  renderGridOptimize,
  renderGridScreener,
  updateGridTriggerHint,
} from "./grid.js";
import {
  buildPresetPool,
  currentBacktestCombo,
  ensurePools,
  recalcEnum,
  recalcScan,
  recalcWalkForward,
  renderBacktest,
  renderScreener,
  syncBacktestMomentum,
  syncBacktestPreset,
  syncSignalMomentum,
} from "./backtest.js";
import {
  confirmPositionsUpdate,
  discardParseResult,
  renderPositions,
  runPositionsParse,
} from "./positions.js";
import { renderAudit, renderDb, renderDbLogs, renderDbTable } from "./audit.js";

registerTabRenderer("overview", renderOverview);
registerTabRenderer("signals", renderSignals);
registerTabRenderer("grid", renderGrid);
registerTabRenderer("backtest", renderBacktest);
registerTabRenderer("screener", renderScreener);
registerTabRenderer("grid-screener", renderGridScreener);
registerTabRenderer("positions", renderPositions);
registerTabRenderer("db", renderDb);
registerTabRenderer("audit", renderAudit);

/* ============================================================
 * 事件绑定
 * ========================================================== */

function bindEvents() {
  $$(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  $("#globalRefresh").addEventListener("click", async () => {
    trace("action: 刷新数据(" + state.activeTab + ")");
    try {
      $("#globalRefresh").disabled = true;
      await renderTab(state.activeTab, true);
      toast("已强制刷新");
    } catch (err) {
      toast("刷新失败: " + err.message, true);
    } finally {
      $("#globalRefresh").disabled = false;
    }
  });

  $("#sig-run").addEventListener("click", async () => {
    trace("action: 扫描信号");
    try {
      $("#sig-run").disabled = true;
      await renderSignals(true);
      toast("信号扫描完成");
    } catch (err) {
      toast("扫描失败: " + err.message, true);
    } finally {
      $("#sig-run").disabled = false;
    }
  });

  $("#bt-run").addEventListener("click", async () => {
    trace("action: 运行回测");
    try {
      $("#bt-run").disabled = true;
      $("#bt-note").textContent = "回测运行中，首次可能需要 1-3 分钟…";
      saveDefaultCombo(currentBacktestCombo());
      await renderBacktest(true);
      toast("回测完成，已设为默认组合");
    } catch (err) {
      toast("回测失败: " + err.message, true);
    } finally {
      $("#bt-run").disabled = false;
    }
  });

  $("#enum-recalc").addEventListener("click", recalcEnum);
  $("#walk-forward").addEventListener("click", recalcWalkForward);
  $("#sc-recalc").addEventListener("click", recalcScan);
  $("#pp-build").addEventListener("click", buildPresetPool);

  $("#sc-run-screener").addEventListener("click", async () => {
    trace("action: 四维选品");
    try {
      $("#sc-run-screener").disabled = true;
      $("#sc-note").textContent = "四维选品运行中…";
      await renderScreener(true);
      toast("选品完成");
    } catch (err) {
      toast("选品失败: " + err.message, true);
    } finally {
      $("#sc-run-screener").disabled = false;
      $("#sc-note").textContent = "";
    }
  });

  $("#au-run").addEventListener("click", async () => {
    trace("action: 运行审计");
    try {
      $("#au-run").disabled = true;
      $("#au-note").textContent = "审计运行中，首次可能需要 1-3 分钟…";
      await renderAudit(true);
      toast("审计完成");
    } catch (err) {
      toast("审计失败: " + err.message, true);
    } finally {
      $("#au-run").disabled = false;
    }
  });

  $("#sc-top").addEventListener("change", () => renderScreener(false));
  $("#sc-category").addEventListener("change", () => renderScreener(false));
  $("#sig-custom").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") $("#sig-run").click();
  });
  $("#sig-pool").addEventListener("change", () => {
    state.lastSignalsPool = $("#sig-pool").value;
    syncBacktestPreset(state.lastSignalsPool);
  });
  $("#sig-momentum").addEventListener("change", () => {
    syncBacktestMomentum($("#sig-momentum").value);
  });
  $("#bt-momentum").addEventListener("change", () => {
    syncSignalMomentum($("#bt-momentum").value);
  });
  // 持仓图片解析
  $("#pos-upload-btn").addEventListener("click", () => $("#pos-file").click());
  $("#pos-file").addEventListener("change", async (ev) => {
    const files = [...(ev.target.files || [])];
    if (!files.length) return;
    try {
      $("#pos-parse-status").textContent = `已选择 ${files.length} 张图片，读取中…`;
      state.parseImages = await Promise.all(files.map(fileToBase64));
      await runPositionsParse();
    } catch (err) {
      toast("图片读取失败: " + err.message, true);
      $("#pos-parse-status").textContent = "";
    }
  });
  $("#pos-confirm").addEventListener("click", confirmPositionsUpdate);
  $("#pos-reparse").addEventListener("click", runPositionsParse);
  $("#pos-discard").addEventListener("click", discardParseResult);
  // 网格
  $("#grid-run").addEventListener("click", async () => {
    trace("action: 刷新网格");
    try {
      $("#grid-run").disabled = true;
      await renderGrid(true);
      toast("网格分析已刷新");
    } catch (err) {
      toast("刷新失败: " + err.message, true);
    } finally {
      $("#grid-run").disabled = false;
    }
  });
  $("#grid-opt-run").addEventListener("click", async () => {
    trace("action: 网格参数优化");
    try {
      $("#grid-opt-run").disabled = true;
      await renderGridOptimize(true);
      toast("网格参数优化完成");
    } catch (err) {
      toast("优化失败: " + err.message, true);
    } finally {
      $("#grid-opt-run").disabled = false;
    }
  });
  $("#grid-trigger-add").addEventListener("click", addGridTrigger);
  $("#grid-upload-btn").addEventListener("click", () => $("#grid-trigger-files").click());
  $("#grid-config-upload").addEventListener("click", () => $("#grid-config-files").click());
  $("#grid-config-files").addEventListener("change", async (ev) => {
    const files = [...(ev.target.files || [])];
    if (!files.length) return;
    try {
      $("#grid-config-note").textContent = `已选择 ${files.length} 张图片，读取中…`;
      state.gridConfigImages = await Promise.all(files.map(fileToBase64));
      await parseGridConfigs();
    } catch (err) {
      toast("图片读取失败: " + err.message, true);
      $("#grid-config-note").textContent = "";
    }
  });
  $("#grid-config-parse").addEventListener("click", parseGridConfigs);
  $("#grid-configs-reload").addEventListener("click", renderGridConfigList);
  $("#grid-screener-run").addEventListener("click", renderGridScreener);
  $("#grid-trigger-files").addEventListener("change", async (ev) => {
    const files = [...(ev.target.files || [])];
    if (!files.length) return;
    try {
      $("#grid-trigger-note").textContent = `已选择 ${files.length} 张图片，读取中…`;
      state.gridParseImages = await Promise.all(files.map(fileToBase64));
      await parseGridTriggers();
    } catch (err) {
      toast("图片读取失败: " + err.message, true);
      $("#grid-trigger-note").textContent = "";
    }
  });
  $("#grid-trigger-parse").addEventListener("click", parseGridTriggers);
  $("#grid-trigger-parse-result").addEventListener("click", (ev) => {
    const verifyBtn = ev.target.closest("[data-verify]");
    if (verifyBtn) {
      const tr = verifyBtn.closest("tr");
      if (tr) tr.dataset.verified = "1";
      verifyBtn.textContent = "✓已核实";
      updateGridTriggerHint();
      return;
    }
    const delBtn = ev.target.closest("[data-del]");
    if (!delBtn) return;
    const tr = delBtn.closest("tr");
    if (tr) tr.remove();
    const box = $("#grid-trigger-parse-result");
    const count = box.querySelectorAll("tbody tr").length;
    const btn = $("#grid-trigger-confirm");
    if (btn) btn.textContent = `确认录入 ${count} 条`;
    updateGridTriggerHint();
  });
  $("#grid-trigger-parse-result").addEventListener("input", (ev) => {
    const tr = ev.target.closest("tr");
    if (!tr) return;
    tr.dataset.verified = "0";
    const btn = tr.querySelector("[data-verify]");
    if (btn) btn.textContent = "核实";
    updateGridTriggerHint();
  });
  $("#grid-triggers-toggle").addEventListener("click", () => {
    const collapsed = $("#grid-triggers").style.display === "none";
    $("#grid-triggers").style.display = collapsed ? "" : "none";
    $("#grid-triggers-filter").style.display = collapsed ? "" : "none";
    $("#grid-triggers-toggle").textContent = collapsed ? "收起" : "展开";
  });
  $("#grid-trigger-filter-btn").addEventListener("click", loadGridTriggers);
  $("#grid-trigger-filter-reset").addEventListener("click", () => {
    $("#grid-trigger-filter-code").value = "";
    $("#grid-trigger-filter-type").value = "";
    $("#grid-trigger-filter-start").value = "";
    $("#grid-trigger-filter-end").value = "";
    loadGridTriggers();
  });
  // 数据存储
  $("#db-load").addEventListener("click", renderDbTable);
  $("#db-prev").addEventListener("click", () => {
    state.dbOffset = Math.max(0, state.dbOffset - Number($("#db-limit").value));
    renderDbTable();
  });
  $("#db-next").addEventListener("click", () => {
    state.dbOffset += Number($("#db-limit").value);
    renderDbTable();
  });
  $("#db-table").addEventListener("change", () => {
    state.dbOffset = 0;
    renderDbTable();
  });
  $("#db-log-load").addEventListener("click", renderDbLogs);
  // 信号明细列排序（事件委托，表格每次重渲染后依然生效）
  $("#sig-table").addEventListener("click", (ev) => {
    const th = ev.target.closest("th[data-key]");
    if (th) sortSignals(th.dataset.key);
  });

  window.addEventListener("resize", () => {
    if (state.activeTab === "signals") redrawCurrent($("#sig-chart"));
    if (state.activeTab === "backtest" && state.backtest) redrawCurrent($("#bt-chart"));
  });
}

/* ============================================================
 * 启动
 * ========================================================== */

async function boot() {
  trace("boot start");
  $("#dataAsOf").textContent = "初始化中：连接服务…";
  bindEvents();
  bindMetricHints();
  try {
    await ensurePools();
    trace("pools ok");
    setServerStatus(true, "服务正常");
    const initial = (location.hash || "").replace("#", "");
    if (PAGE_TITLES[initial]) switchTab(initial);
    else await renderOverview();
    trace("overview rendered");
  } catch (err) {
    trace("boot error: " + (err && err.message ? err.message : String(err)));
    renderFatalError(err);
  }
}

boot();
