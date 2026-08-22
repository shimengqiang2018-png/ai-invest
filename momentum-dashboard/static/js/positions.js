/* ============================================================
 * 持仓
 * ========================================================== */

import { $, $$, esc, fmtNum, fmtNum3, fmtPct, pctCls, strColor, strategyLabel } from "./utils.js";
import { api, applyEnvelope, state, toast } from "./core.js";

async function renderPositions() {
  loadModelProviders();
  const [body, gridBody] = await Promise.all([
    api("/api/positions"),
    api("/api/grid", { quiet: true }).catch(() => null),
  ]);
  state.positions = applyEnvelope(body);
  const pos = state.positions;
  const summary = pos.account_summary || {};
  const holdings = pos.holdings || [];
  const gridMap = {};
  ((gridBody && gridBody.data && gridBody.data.items) || []).forEach((g) => {
    gridMap[g.code] = g;
  });
  const dateStr = pos.date ? String(pos.date).slice(0, 10) : "—";
  $("#dataAsOf").textContent = `持仓快照: ${dateStr} · ${pos.source || ""}`;
  $("#pos-subtitle").textContent = `${holdings.length} 只持仓`;

  const cards = [
    ["总资产", "¥" + fmtNum3(summary.total_assets), "", ""],
    ["证券市值", "¥" + fmtNum3(summary.securities_value), "", ""],
    ["可用现金", "¥" + fmtNum3(summary.available_cash), "", ""],
    ["仓位", fmtPct(summary.position_ratio, 3), "", ""],
    ["持仓盈亏", "¥" + fmtNum3(summary.total_pnl), fmtPct(summary.total_pnl_pct, 3, true), pctCls(summary.total_pnl)],
    ["当日盈亏", "¥" + fmtNum3(summary.daily_pnl), fmtPct(summary.daily_pnl_pct, 3, true), pctCls(summary.daily_pnl)],
  ];
  $("#pos-summary").innerHTML = cards
    .map(
      ([k, v, d, cls]) => `
      <div class="card">
        <div class="k">${esc(k)}</div>
        <div class="v ${cls}">${esc(v)}</div>
        <div class="d">${esc(d)}</div>
      </div>`
    )
    .join("");

  // 策略归属：子账户分桶 + 红线提示
  const buckets = pos.strategy_summary || {};
  const bucketColors = {
    网格子账户: "#2ebd85",
    动量子账户: "#4c9aff",
    底仓: "#9d8cff",
    现金储备: "#8b98a9",
    其他: "#5f6c7d",
  };
  const bucketEntries = Object.entries(buckets).sort((a, b) => b[1].market_value - a[1].market_value);
  $("#pos-buckets").innerHTML = bucketEntries.length
    ? bucketEntries
        .map(([name, info]) => {
          const color = bucketColors[name] || "#5f6c7d";
          return `
          <div class="card">
            <div class="k" style="color:${color}">${esc(name)}（${info.count} 只）</div>
            <div class="v">¥${fmtNum(info.market_value, 0)}</div>
            <div class="d">占比 ${info.weight != null ? fmtPct(info.weight, 2) : "—"}%</div>
          </div>`;
        })
        .join("")
    : '<div class="empty">暂无数据</div>';
  const notes = pos.notes || [];
  $("#pos-notes").innerHTML = notes
    .map(
      (n) => `
      <div class="advice-item">
        <div class="a-k">双策略红线</div>
        <div class="a-v" style="font-size:12.5px">${esc(n)}</div>
      </div>`
    )
    .join("");

  $("#pos-table").innerHTML = holdings.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>持仓</th><th>可用</th><th>现价</th>
        <th>成本</th><th>底仓</th><th>市值</th><th>持仓盈亏</th><th>盈亏率</th>
        <th>当日盈亏</th><th>子账户</th><th>策略归属</th>
      </tr></thead>
      <tbody>
        ${holdings
          .map((h) => {
            const label = strategyLabel(h.strategy);
            return `
            <tr>
              <td>${esc(h.code)}</td>
              <td>${esc(h.name)}</td>
              <td class="num">${fmtNum(h.shares, 0)}</td>
              <td class="num">${fmtNum(h.available, 0)}</td>
              <td class="num">${fmtNum3(h.price)}</td>
              <td class="num">${fmtNum3(h.cost)}</td>
              <td class="num dim">${fmtNum(h.base_shares, 0)}</td>
              <td class="num">${fmtNum3(h.market_value)}</td>
              <td class="num ${pctCls(h.pnl)}">${fmtNum3(h.pnl)}</td>
              <td class="num ${pctCls(h.pnl_pct)}">${fmtPct(h.pnl_pct, 3, true)}</td>
              <td class="num ${pctCls(h.daily_pnl)}">${fmtNum(h.daily_pnl, 0)}</td>
              <td class="dim">${esc(h.bucket || "—")}</td>
              <td><span class="tag" style="background:${strColor(label)}22;color:${strColor(label)}">${esc(label)}</span></td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无持仓数据</div>';

  // 网格标的状态（持仓中属于网格标的的）
  const gridHoldings = holdings.filter((h) => gridMap[h.code]);
  $("#pos-grid-targets").innerHTML = gridHoldings.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>现价</th><th>基准价</th>
        <th>买入触发</th><th>卖出触发</th><th>趋势评分</th><th>判断</th>
      </tr></thead>
      <tbody>
        ${gridHoldings
          .map((h) => {
            const g = gridMap[h.code];
            const cur = g.current_price;
            const hitSell = cur != null && g.sell_price != null && cur >= g.sell_price;
            const hitBuy = cur != null && g.buy_price != null && cur <= g.buy_price;
            const score = g.score;
            const scoreCls = score == null ? "dim" : score <= -4 ? "down" : score <= -2 ? "warn" : "up";
            return `
            <tr>
              <td>${esc(g.code)}</td>
              <td>${esc(g.name)}</td>
              <td class="num ${pctCls(g.change_pct)}">${fmtNum3(cur)}
                ${hitSell ? '<span class="pass-yes"> 卖✓</span>' : ""}
                ${hitBuy ? '<span class="pass-no"> 买✓</span>' : ""}</td>
              <td class="num">${fmtNum3(g.base_price)}</td>
              <td class="num">${fmtNum3(g.buy_price)}</td>
              <td class="num">${fmtNum3(g.sell_price)}</td>
              <td class="num ${scoreCls}">${score == null ? "—" : score}</td>
              <td class="dim">${esc(g.verdict || g.error || "—")}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">当前持仓中没有网格标的（或网格数据未加载）</div>';

  renderPositionChips("pos-groups", 12, true);
}

/* ============================================================
 * 持仓图片解析（多厂商模型）
 * ========================================================== */

let modelsProviderCache = null;

async function loadModelProviders(selId) {
  try {
    if (!modelsProviderCache) {
      const body = await api("/api/models", { quiet: true });
      modelsProviderCache = body.data.providers || [];
    }
    const providers = modelsProviderCache;
    const targets = selId
      ? [selId]
      : ["pos-provider", "grid-trigger-provider", "grid-config-provider"];
    const usable = providers.filter((p) => p.configured);
    const pool = usable.length ? usable : providers;
    for (const target of targets) {
      const sel = document.getElementById(target);
      if (!sel) continue;
      sel.innerHTML = "";
      if (!pool.length) {
        sel.innerHTML = '<option value="">未配置可用模型</option>';
        continue;
      }
      pool.forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p.name;
        opt.textContent =
          `${p.name}（${p.model}）` +
          `${p.vision ? "" : " · 本地OCR"}` +
          `${p.configured ? "" : " · 未配置Key"}`;
        sel.appendChild(opt);
      });
      const preferred = usable[0] || pool[0];
      sel.value = preferred.name;
    }
  } catch (err) {
    /* 静默：无模型配置时上传按钮会提示 */
  }
}


function verifyBadge(status) {
  if (status === "ok") return '<span class="tag ok">✓ 通过</span>';
  if (status === "warn") return '<span class="tag medium">⚠ 核实</span>';
  return '<span class="tag unknown">✗ 错误</span>';
}

function renderParseResult() {
  const data = state.parseResult;
  if (!data) return;
  const verified = data.verification || {};
  const meta = $("#pos-parse-meta");
  const statusText =
    verified.status === "ok" ? "通过" : verified.status === "warn" ? "存在警告" : "存在错误";
  const pipeline =
    data.parsed && data.parsed.parse_pipeline === "local_ocr" ? "本地OCR+模型" : "视觉模型";
  meta.textContent =
    `厂商 ${data.provider || "—"} · ${pipeline} · 核验 ${statusText} · ` +
    `${verified.counts.holdings || 0} 只持仓 / ${verified.counts.trades || 0} 笔交易`;

  const summary = verified.account_summary || {};
  const cards = [
    ["总资产", "¥" + fmtNum3(summary.total_assets), "", ""],
    ["证券市值", "¥" + fmtNum3(summary.securities_value), "", ""],
    ["可用资金", "¥" + fmtNum3(summary.available_cash), "", ""],
    ["持仓盈亏", "¥" + fmtNum3(summary.total_pnl), fmtPct(summary.total_pnl, 3, true), pctCls(summary.total_pnl)],
    ["当日盈亏", "¥" + fmtNum3(summary.daily_pnl), "", pctCls(summary.daily_pnl)],
  ];
  const issuesHtml = (summary.issues || []).length
    ? `<div class="advice-row grid" style="grid-column:1/-1">
         <div class="a-t">汇总核验问题</div><div>${esc((summary.issues || []).join("；"))}</div>
       </div>`
    : "";
  const summaryEdit = (summary.issues || []).length
    ? `<div class="advice-row grid" style="grid-column:1/-1">
         <div class="a-t">修改后点「核实汇总」清除问题</div>
         <label>总资产 <input class="edit-input num" data-summary="total_assets" type="number" step="0.01" value="${summary.total_assets ?? ""}"></label>
         <label>证券市值 <input class="edit-input num" data-summary="securities_value" type="number" step="0.01" value="${summary.securities_value ?? ""}"></label>
         <label>可用资金 <input class="edit-input num" data-summary="available_cash" type="number" step="0.01" value="${summary.available_cash ?? ""}"></label>
         <button class="btn ghost" data-summary-verify style="padding:4px 10px;font-size:12px">核实汇总</button>
       </div>`
    : "";
  $("#pos-parse-summary").innerHTML =
    cards
      .map(([k, v, d, cls]) => `
        <div class="card">
          <div class="k">${esc(k)}</div>
          <div class="v ${cls}">${esc(v)}</div>
          <div class="d">${esc(d)}</div>
        </div>`)
      .join("") + (summaryEdit || issuesHtml);

  const holdings = verified.holdings || [];
  const editable = (key, val, step, d) =>
    `<input class="edit-input num" data-edit="${key}" type="number" step="${step}" value="${val ?? ""}" title="${esc(d || "")}">`;
  const numCell = (val, d = 3) => `<span class="num">${fmtNum(val, d)}</span>`;
  $("#pos-parse-holdings").innerHTML = holdings.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>持仓</th><th>可用</th><th>现价</th>
        <th>成本</th><th>市值</th><th>占比</th><th>盈亏</th><th>盈亏率</th><th>来源</th><th>核验</th><th>操作</th>
      </tr></thead>
      <tbody>
        ${holdings
          .map((h) => {
            const editableRow = h.status !== "ok";
            const action =
              h.status !== "ok"
                ? `<button class="btn ghost" data-verify="${esc(h.code || "")}" style="padding:4px 10px;font-size:12px">核实修改</button>`
                : h.verified
                ? '<span class="pass-yes">已核实 ✓</span>'
                : '<span class="pass-yes">✓</span>';
            return `
            <tr data-code="${esc(h.code || "")}" title="${esc((h.issues || []).join("；") + (h.realtime_note ? "；" + h.realtime_note : ""))}">
              <td>${esc(h.code || "—")}</td>
              <td><input class="edit-input" data-edit="name" value="${esc(h.name || "")}" style="max-width:150px"></td>
              <td class="num">${editableRow ? editable("shares", h.shares, 1) : numCell(h.shares, 0)}</td>
              <td class="num">${editableRow ? editable("available", h.available, 1) : numCell(h.available, 0)}</td>
              <td class="num">${editableRow ? editable("price", h.price, 0.001) : numCell(h.price)}</td>
              <td class="num">${editableRow ? editable("cost", h.cost, 0.001) : numCell(h.cost)}</td>
              <td class="num">${editableRow ? editable("market_value", h.market_value, 0.001) : numCell(h.market_value)}</td>
              <td class="num">${h.weight_pct != null ? fmtPct(h.weight_pct, 2) : "—"}</td>
              <td class="num ${pctCls(h.pnl)}">${editableRow ? editable("pnl", h.pnl, 0.001) : fmtNum3(h.pnl)}</td>
              <td class="num ${pctCls(h.pnl_pct)}">${editableRow ? editable("pnl_pct", h.pnl_pct, 0.001) : fmtPct(h.pnl_pct, 3, true)}</td>
              <td class="dim">${esc(h.source || "—")}</td>
              <td>${verifyBadge(h.status)}</td>
              <td>${action}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">未识别到持仓</div>';

  const trades = verified.trades || [];
  $("#pos-parse-trades").innerHTML = trades.length
    ? `
    <table>
      <thead><tr>
        <th>日期</th><th>方向</th><th>代码</th><th>名称</th>
        <th>价格</th><th>数量</th><th>金额</th><th>来源</th><th>核验</th><th>操作</th>
      </tr></thead>
      <tbody>
        ${trades
          .map((t, ti) => `
            <tr data-trade-index="${ti}" title="${esc((t.issues || []).join("；"))}">
              <td class="num">${esc(t.date || "—")}</td>
              <td class="${String(t.action || "").includes("卖") ? "down" : "up"}">${esc(t.action || "—")}</td>
              <td>${esc(t.code || "—")}</td>
              <td>${esc(t.name || "—")}</td>
              <td class="num">${t.status === "error" ? `<input class="edit-input num" data-trade-edit="price" type="number" step="0.001" value="${t.price ?? ""}">` : fmtNum3(t.price)}</td>
              <td class="num">${t.status === "error" ? `<input class="edit-input num" data-trade-edit="shares" type="number" step="1" value="${t.shares ?? ""}">` : fmtNum(t.shares, 0)}</td>
              <td class="num">${t.status === "error" ? `<input class="edit-input num" data-trade-edit="amount" type="number" step="0.01" value="${t.amount ?? ""}">` : fmtNum3(t.amount)}</td>
              <td class="dim">${esc(t.source || "—")}</td>
              <td>${verifyBadge(t.status)}</td>
              <td>${t.status === "error" ? `<button class="btn ghost" data-trade-verify style="padding:4px 10px;font-size:12px">核实</button>` : ""}</td>
            </tr>`)
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">未识别到交易记录</div>';

  const cross = verified.cross_validation || [];
  if (cross.length) {
    const cvBox = document.createElement("div");
    cvBox.className = "table-wrap";
    cvBox.style.marginTop = "10px";
    cvBox.innerHTML = `
      <div class="panel-title">成交记录反推核验 <span class="panel-sub">成交反推 vs 截图（偏差>3% 标记错误）</span></div>
      <table>
        <thead><tr>
          <th>代码</th><th>名称</th><th>反推持仓</th><th>截图持仓</th>
          <th>反推成本</th><th>截图成本</th><th>成交笔数</th>
        </tr></thead>
        <tbody>
          ${cross
            .map((c) => {
              const shareDiff =
                c.calc_shares != null && c.reported_shares != null
                  ? Math.abs(c.calc_shares - c.reported_shares) / Math.max(Math.abs(c.calc_shares), 1)
                  : 0;
              const cls = shareDiff > 0.03 ? "warn" : "";
              return `
              <tr class="${cls ? "sel" : ""}">
                <td>${esc(c.code)}</td>
                <td>${esc(c.name || "—")}</td>
                <td class="num">${fmtNum(c.calc_shares, 0)}</td>
                <td class="num">${fmtNum(c.reported_shares, 0)}</td>
                <td class="num">${c.calc_cost != null ? fmtNum(c.calc_cost, 4) : "—"}</td>
                <td class="num">${c.reported_cost != null ? fmtNum(c.reported_cost, 4) : "—"}</td>
                <td class="num">${c.trades || 0}</td>
              </tr>`;
            })
            .join("")}
        </tbody>
      </table>`;
    $("#pos-parse-trades").appendChild(cvBox);
  }

  // ⚠ 核实项：编辑后标记已核实
  $$("#pos-parse-holdings [data-verify]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest("tr[data-code]");
      const code = row && row.dataset.code;
      const holding = (verified.holdings || []).find((h) => h.code === code);
      if (!holding) return;
      row.querySelectorAll("[data-edit]").forEach((input) => {
        if (input.dataset.edit === "name") {
          holding.name = input.value.trim();
          return;
        }
        const value = input.value === "" ? null : Number(input.value);
        holding[input.dataset.edit] = value !== null && Number.isFinite(value) ? value : null;
      });
      holding.status = "ok";
      holding.verified = true;
      holding.warnings = [];
      holding.errors = [];
      holding.issues = [];
      recomputeVerified(verified);
      renderParseResult();
      toast(`${holding.name || code} 已核实`);
    });
  });

  // 名称修改：任何一行都能直接改识别出的名称，改动即时写回 holding（确认时一并保存）
  $$("#pos-parse-holdings [data-edit='name']").forEach((input) => {
    input.addEventListener("change", () => {
      const row = input.closest("tr[data-code]");
      const code = row && row.dataset.code;
      const holding = (verified.holdings || []).find((h) => h.code === code);
      if (holding) holding.name = input.value.trim();
    });
  });

  // ⚠ 汇总：编辑后核实，清除汇总问题
  const summaryVerifyBtn = $("#pos-parse-summary [data-summary-verify]");
  if (summaryVerifyBtn) {
    summaryVerifyBtn.addEventListener("click", () => {
      $$("#pos-parse-summary [data-summary]").forEach((input) => {
        const value = input.value === "" ? null : Number(input.value);
        summary[input.dataset.summary] = value !== null && Number.isFinite(value) ? value : null;
      });
      summary.issues = [];
      recomputeVerified(verified);
      renderParseResult();
      toast("汇总已核实");
    });
  }

  // ⚠ 交易：编辑后核实
  $$("#pos-parse-trades [data-trade-verify]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest("tr[data-trade-index]");
      const index = row && Number(row.dataset.tradeIndex);
      const trade = (verified.trades || [])[index];
      if (!trade) return;
      row.querySelectorAll("[data-trade-edit]").forEach((input) => {
        const value = input.value === "" ? null : Number(input.value);
        trade[input.dataset.tradeEdit] = value !== null && Number.isFinite(value) ? value : null;
      });
      trade.status = "ok";
      trade.errors = [];
      trade.issues = [];
      recomputeVerified(verified);
      renderParseResult();
      toast(`${trade.code || ""} 交易已核实`);
    });
  });

  recomputeVerified(verified);

  $("#pos-parse-result").hidden = false;
}

function recomputeVerified(verified) {
  const holdings = verified.holdings || [];
  const trades = verified.trades || [];
  const summary = verified.account_summary || {};
  const hasError =
    holdings.some((h) => h.status === "error") ||
    trades.some((t) => t.status === "error") ||
    (summary.issues || []).length > 0;
  const hasWarn =
    holdings.some((h) => h.status === "warn") ||
    trades.some((t) => t.status === "warn");
  verified.status = hasError ? "error" : hasWarn ? "warn" : "ok";
  verified.counts = {
    holdings: holdings.length,
    trades: trades.length,
    holdings_ok: holdings.filter((h) => h.status === "ok").length,
    holdings_warn: holdings.filter((h) => h.status === "warn").length,
    holdings_error: holdings.filter((h) => h.status === "error").length,
    trades_error: trades.filter((t) => t.status === "error").length,
  };
  $("#pos-confirm").disabled = hasError;
  return !hasError;
}

async function runPositionsParse() {
  const provider = $("#pos-provider").value;
  if (!provider || provider === "未配置视觉模型") {
    toast("请先在 model_config.json 配置视觉模型厂商并设置 API Key", true);
    return;
  }
  if (!state.parseImages.length) {
    toast("请先选择截图", true);
    return;
  }
  $("#pos-parse-status").textContent = "解析中（模型调用可能需要 10-60 秒）…";
  $("#pos-confirm").disabled = true;
  try {
    const body = await api("/api/positions/parse", {
      progress: "正在调用视觉模型解析持仓…",
      timeoutMs: 120000,
      method: "POST",
      body: { provider, images: state.parseImages },
    });
    state.parseResult = body.data;
    renderParseResult();
    $("#pos-confirm").disabled =
      (state.parseResult.verification || {}).status === "error";
    $("#pos-parse-status").textContent = "";
  } catch (err) {
    toast("解析失败: " + err.message, true);
    $("#pos-parse-status").textContent = "解析失败";
  }
}

async function confirmPositionsUpdate() {
  const verified = state.parseResult && state.parseResult.verification;
  if (!verified || !verified.holdings) return;
  if (verified.status === "error") {
    toast("存在核验错误项，无法更新（请重新解析或修正）", true);
    return;
  }
  if (!confirm(`确认将解析结果（${verified.holdings.length} 只持仓）更新到系统持仓？`)) return;
  try {
    const body = await api("/api/positions/update", {
      progress: "正在更新持仓…",
      timeoutMs: 30000,
      method: "POST",
      body: { verified, source: "AI 图片解析" },
    });
    state.positions = body.data;
    discardParseResult();
    await renderPositions();
    toast("持仓已更新到系统" + (body.data.excel_file ? "，已生成 Excel" : ""));
  } catch (err) {
    toast("更新失败: " + err.message, true);
  }
}

function discardParseResult() {
  state.parseResult = null;
  state.parseImages = [];
  $("#pos-parse-result").hidden = true;
  $("#pos-parse-status").textContent = "";
  $("#pos-file").value = "";
  $("#pos-confirm").disabled = true;
}

function renderPositionChips(targetId, limit, showAll = false) {
  const holdings = (state.positions && state.positions.holdings) || (state.overview ? [] : []);
  if (!holdings.length) {
    $("#" + targetId).innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }
  const groups = {};
  holdings.forEach((h) => {
    const label = strategyLabel(h.strategy);
    groups[label] = (groups[label] || 0) + (h.market_value || 0);
  });
  const entries = Object.entries(groups).sort((a, b) => b[1] - a[1]).slice(0, limit);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  $("#" + targetId).innerHTML = entries
    .map(([label, v]) => `
      <div class="chip">
        <div class="c-n" style="color:${strColor(label)}">${esc(label)}</div>
        <div class="c-v">¥${fmtNum(v, 0)}</div>
        <div class="c-d">${total ? ((v / total) * 100).toFixed(1) : 0}%</div>
      </div>`)
    .join("");
}

export {
  confirmPositionsUpdate,
  discardParseResult,
  loadModelProviders,
  recomputeVerified,
  renderParseResult,
  renderPositionChips,
  renderPositions,
  runPositionsParse,
  verifyBadge,
};
