/* ============================================================
 * 网格策略
 * ========================================================== */

import { $, $$, esc, fmtNum, fmtNum3, fmtPct, pctCls } from "./utils.js";
import { api, state, toast } from "./core.js";
import { drawKlineChart } from "./charts.js";
import { loadModelProviders, verifyBadge } from "./positions.js";

async function renderGrid(force) {
  const body = await api("/api/grid" + (force ? "?refresh=1" : ""), {
    progress: "正在分析网格标的…",
    timeoutMs: force ? 180000 : 60000,
  });
  const grid = body.data;
  const items = grid.items || [];
  const groups = grid.groups || {};
  const groupMeta = [
    ["stop", "⛔ 暂停买入", groups.stop || []],
    ["caution", "⚠ 谨慎", groups.caution || []],
    ["ok", "✅ 正常", groups.ok || []],
    ["unknown", "— 未知", groups.unknown || []],
  ];
  $("#grid-groups").innerHTML = groupMeta
    .map(([key, label, codes]) => {
      if (!codes.length) return "";
      return `
        <div class="advice-item">
          <div class="a-k">${label}（${codes.length}）</div>
          <div class="a-v" style="font-size:12.5px">${esc(codes.join(" "))}</div>
        </div>`;
    })
    .join("");
  $("#grid-subtitle").textContent = `${items.length} 只标的 · 刷新于 ${(grid.as_of || "").slice(0, 19).replace("T", " ")}`;
  $("#grid-note").textContent = "";

  const gridScores = {};
  $("#grid-table").innerHTML = items.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>现价</th><th>基准价</th>
        <th>买入触发</th><th>卖出触发</th><th>评分</th><th>BB宽</th>
        <th>MA状态</th><th>判断</th><th>触发次数</th><th>最近触发</th><th>评分</th>
      </tr></thead>
      <tbody>
        ${items
          .map((g) => {
            const score = g.score;
            const scoreCls = score == null ? "dim" : score <= -4 ? "down" : score <= -2 ? "warn" : "up";
            const last = g.last_trigger;
            const lastText = last
              ? `${last.date} ${last.action === "buy" ? "买" : "卖"} ${fmtNum3(last.price)} × ${fmtNum(last.shares, 0)}`
              : "—";
            const cur = g.current_price;
            const sellPrice = g.sell_price;
            const buyPrice = g.buy_price;
            const hitSell =
              cur != null && sellPrice != null && cur >= sellPrice
                ? '<span class="pass-yes"> 卖✓</span>'
                : "";
            const hitBuy =
              cur != null && buyPrice != null && cur <= buyPrice
                ? '<span class="pass-no"> 买✓</span>'
                : "";
            gridScores[g.code] = g.scores || {};
            return `
            <tr data-grid-score-code="${esc(g.code)}" title="${esc(g.error || g.verdict || "")}">
              <td>${esc(g.code)}</td>
              <td>${esc(g.name)}</td>
              <td class="num ${pctCls(g.change_pct)}">${fmtNum3(cur)}${hitSell}${hitBuy}</td>
              <td class="num">${fmtNum3(g.base_price)}</td>
              <td class="num">${fmtNum3(buyPrice)}</td>
              <td class="num">${fmtNum3(sellPrice)}</td>
              <td class="num ${scoreCls}">${score == null ? "—" : score}</td>
              <td class="num">${g.bb_width != null ? fmtPct(g.bb_width, 1) : "—"}</td>
              <td>${esc(g.ma_state || "—")}</td>
              <td class="dim">${esc(g.verdict || g.error || "—")}</td>
              <td class="num">${g.trigger_count || 0}</td>
              <td class="num dim">${esc(lastText)}</td>
              <td><button class="btn ghost" data-grid-score style="padding:4px 8px;font-size:12px">评分</button></td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无网格配置（tools/grid_trading.py CONFIGS 为空）</div>';
  state.gridScores = gridScores;
  $$("#grid-table [data-grid-score]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tr = btn.closest("tr[data-grid-score-code]");
      gridScoreDetail(tr, tr.dataset.gridScoreCode);
    });
  });

  let positions = grid.positions || [];
  try {
    // 网格持仓详情：单独实时查库（不经 5 分钟总览缓存）
    const posBody = await api("/api/grid/positions", { quiet: true });
    if (posBody.data && posBody.data.positions) {
      positions = posBody.data.positions;
    }
  } catch (err) {
    /* 实时接口失败时回退总览缓存数据 */
  }
  const held = positions.filter((p) => (p.total_shares || 0) > 0);
  const totalMv = held.reduce((s, p) => s + (p.market_value || 0), 0);
  const totalPnl = held.reduce((s, p) => s + (p.pnl || 0), 0);
  $("#grid-positions-summary").textContent = held.length
    ? `${held.length} 只持仓 · 总市值 ¥${fmtNum(totalMv, 2)} · 浮动盈亏 ${
        totalPnl >= 0 ? "+" : ""
      }¥${fmtNum(totalPnl, 2)}（网格配置 ${positions.length} 只）`
    : "暂无网格持仓";
  $("#grid-positions").innerHTML = positions.length
    ? `
    <table>
      <thead><tr><th>代码</th><th>名称</th><th>策略归属</th><th>总持仓</th>
        <th>底仓</th><th>网格仓</th><th>现价</th><th>成本</th><th>市值</th>
        <th>浮动盈亏</th><th>盈亏率</th><th>当前基准价</th><th>备注</th></tr></thead>
      <tbody>
        ${positions
          .map((p) => {
            const pnl = p.pnl;
            const pnlCls = pnl == null ? "dim" : pnl >= 0 ? "up" : "down";
            const note = p.note
              ? `<span class="down" title="${esc(p.note)}">⚠ ${esc(p.note)}</span>`
              : '<span class="dim">—</span>';
            return `
            <tr>
              <td>${esc(p.code)}</td>
              <td>${esc(p.name)}</td>
              <td>${esc(p.strategy)}</td>
              <td class="num">${fmtNum(p.total_shares, 0)}</td>
              <td class="num dim">${fmtNum(p.base_position, 0)}</td>
              <td class="num">${fmtNum(p.grid_position, 0)}</td>
              <td class="num">${fmtNum3(p.price)}</td>
              <td class="num dim">${fmtNum3(p.cost)}</td>
              <td class="num">${fmtNum(p.market_value, 2)}</td>
              <td class="num ${pnlCls}">${pnl == null ? "—" : (pnl >= 0 ? "+" : "") + fmtNum(pnl, 2)}</td>
              <td class="num ${pnlCls}">${p.pnl_pct == null ? "—" : fmtPct(p.pnl_pct, 2)}</td>
              <td class="num">${fmtNum3(p.base_price)}</td>
              <td class="dim">${note}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无网格持仓</div>';

  const codeSel = $("#grid-trigger-code");
  if (codeSel && !codeSel.options.length) {
    items.forEach((g) => {
      const opt = document.createElement("option");
      opt.value = g.code;
      opt.textContent = `${g.code} ${g.name || ""}`;
      codeSel.appendChild(opt);
    });
    if (codeSel.options.length && !$("#grid-trigger-date").value) {
      $("#grid-trigger-date").value = new Date().toISOString().slice(0, 10);
    }
    if (codeSel.options.length && !$("#grid-trigger-time").value) {
      $("#grid-trigger-time").value = new Date().toTimeString().slice(0, 5);
    }
  }
  await loadModelProviders();
  await loadGridTriggers();
  await renderGridConfigList();
}

function gridScoreDetail(tr, code) {
  const existing = tr.nextElementSibling;
  if (existing && existing.classList && existing.classList.contains("grid-score-detail")) {
    existing.remove();
    return;
  }
  const s = (state.gridScores || {})[code] || {};
  const tg = s.trigger || {};
  const row = (label, value, cls = "") =>
    `<tr><td>${esc(label)}</td><td class="num ${cls}">${value}</td></tr>`;
  const detail = document.createElement("tr");
  detail.className = "grid-score-detail";
  detail.innerHTML = `<td colspan="13">
    <div class="panel-title">多维度评分 · ${esc(code)}</div>
    <table>
      <tbody>
        ${row("趋势评分", s.trend ?? "—",
          s.trend != null && s.trend > 0 ? "up" : s.trend != null && s.trend < 0 ? "down" : "")}
        ${row("网格适配评分", s.grid != null ? `${s.grid}/21` : "—",
          s.grid != null && s.grid >= 15 ? "up" : s.grid != null && s.grid < 10 ? "down" : "")}
        ${row("动量RSRS", s.rsrs ?? "—",
          s.rsrs != null && s.rsrs > 0 ? "up" : s.rsrs != null && s.rsrs < 0 ? "down" : "")}
        ${row("动量信号", s.momentum ?? "—")}
        ${row("年化波动%", s.vol20 ?? "—")}
        ${row("RSI", s.rsi ?? "—")}
        ${row("20日趋势%", s.trend20 ?? "—")}
        ${row("BB宽度%", s.bb_width ?? "—")}
        ${row("触发次数", tg.count ?? "—")}
        ${row("日均触发", tg.freq ?? "—")}
        ${row("最近方向链", tg.chain ?? "—")}
        ${row("触发判断", tg.verdict ?? "—")}
      </tbody>
    </table>
  </td>`;
  tr.after(detail);
}

async function loadGridTriggers() {
  const code = $("#grid-trigger-filter-code").value.trim();
  const type = $("#grid-trigger-filter-type").value;
  const start = $("#grid-trigger-filter-start").value;
  const end = $("#grid-trigger-filter-end").value;
  const query = new URLSearchParams({ limit: "500" });
  if (code) query.set("code", code);
  if (type) query.set("trigger_type", type);
  if (start) query.set("start", start);
  if (end) query.set("end", end);
  let records = [];
  try {
    const body = await api(`/api/grid/triggers/list?${query}`, { quiet: true });
    records = body.data.records || [];
  } catch (err) {
    $("#grid-triggers").innerHTML =
      `<div class="empty">查询失败：${esc(err.message)}</div>`;
    return;
  }
  $("#grid-triggers-count").textContent = `共 ${records.length} 条`;
  $("#grid-triggers").innerHTML = records.length
    ? `
    <table>
      <thead><tr><th>日期</th><th>动作</th><th>类型</th><th>代码</th><th>名称</th>
        <th>价格</th><th>数量</th><th>基准价变化</th><th>来源</th></tr></thead>
      <tbody>
        ${records
          .map((t) => `
            <tr>
              <td class="num">${esc(t.trigger_date || "")}</td>
              <td class="${t.action === "buy" ? "up" : "down"}">${t.action === "buy" ? "买入" : "卖出"}</td>
              <td>${esc({ grid: "网格", add: "加仓", reduce: "减仓", momentum: "动量" }[t.trigger_type] || "网格")}</td>
              <td>${esc(t.code)}</td>
              <td>${esc(t.name || "—")}</td>
              <td class="num">${fmtNum3(t.price)}</td>
              <td class="num">${fmtNum(t.shares, 0)}</td>
              <td class="num dim">${fmtNum3(t.base_price_before)} → ${fmtNum3(t.base_price_after)}</td>
              <td class="dim">${esc(t.source || "—")}</td>
            </tr>`)
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无匹配的触发记录</div>';
}

async function addGridTrigger() {
  const payload = {
    code: $("#grid-trigger-code").value,
    date: $("#grid-trigger-date").value,
    time: $("#grid-trigger-time").value,
    action: $("#grid-trigger-action").value,
    trigger_type: $("#grid-trigger-type").value,
    price: $("#grid-trigger-price").value,
    shares: $("#grid-trigger-shares").value,
  };
  const bb = $("#grid-trigger-bb").value;
  const ba = $("#grid-trigger-ba").value;
  if (bb) payload.base_price_before = bb;
  if (ba) payload.base_price_after = ba;
  if (!payload.code || !payload.date || !payload.price || !payload.shares) {
    toast("请填写完整（标的/日期/价格/数量）", true);
    return;
  }
  try {
    const body = await api("/api/grid/triggers", { method: "POST", body: payload });
    const rec = body.data.record;
    toast(
      `${body.data.db_status === "inserted" ? "已录入" : "记录已存在（更新）"} ` +
        `${rec.code} ${rec.date} ${rec.action === "buy" ? "买入" : "卖出"} ` +
        `${fmtNum3(rec.price)}×${rec.shares}`
    );
    ["#grid-trigger-price", "#grid-trigger-shares", "#grid-trigger-bb", "#grid-trigger-ba"]
      .forEach((id) => ($(id).value = ""));
    await renderGrid(true);
  } catch (err) {
    toast("录入失败: " + err.message, true);
  }
}

async function parseGridTriggers() {
  const files = state.gridParseImages || [];
  if (!files.length) {
    toast("请先选择成交截图", true);
    return;
  }
  const provider = $("#grid-trigger-provider").value;
  if (!provider) {
    toast("未配置可用识别模型", true);
    return;
  }
  try {
    $("#grid-trigger-note").textContent = "识别中（模型调用可能需要 10-60 秒）…";
    const body = await api("/api/grid/triggers/parse", {
      method: "POST",
      body: { provider, images: files },
      timeoutMs: 120000,
    });
    $("#grid-trigger-note").textContent =
      `识别完成（${body.data.pipeline === "vision" ? "视觉模型" : "本地OCR+模型"}），` +
      `共 ${body.data.records.length} 条，请核验后确认录入`;
    renderGridParseResult(body.data.records);
  } catch (err) {
    $("#grid-trigger-note").textContent = "";
    toast("识别失败: " + err.message, true);
  }
}

function renderGridParseResult(records) {
  const box = $("#grid-trigger-parse-result");
  if (!records.length) {
    box.innerHTML = '<div class="empty">未识别到成交记录</div>';
    return;
  }
  box.innerHTML = `
    <div class="advice-banner" id="grid-trigger-verify-hint"></div>
    <table>
      <thead><tr><th>核验</th><th>代码</th><th>名称</th><th>日期</th><th>时间</th>
        <th>动作</th><th>类型</th><th>价格</th><th>数量</th><th>基准价前</th><th>基准价后</th><th></th></tr></thead>
      <tbody>
        ${records
          .map((r, i) => gridTriggerRowHtml(r, i))
          .join("")}
      </tbody>
    </table>
    <div class="toolbar">
      <button class="btn" id="grid-trigger-add-row">+ 添加一行</button>
      <button class="btn" id="grid-trigger-drop-verify">移除需核对</button>
      <button class="btn" id="grid-trigger-confirm">确认录入 ${records.length} 条</button>
      <button class="btn" id="grid-trigger-clear">清空</button>
    </div>`;
  updateGridTriggerHint();
  $("#grid-trigger-add-row").addEventListener("click", () => {
    const tbody = box.querySelector("tbody");
    const index = tbody ? tbody.children.length : 0;
    tbody.insertAdjacentHTML(
      "beforeend",
      gridTriggerRowHtml({ action: "buy" }, index)
    );
    const count = box.querySelectorAll("tbody tr").length;
    const btn = $("#grid-trigger-confirm");
    if (btn) btn.textContent = `确认录入 ${count} 条`;
    updateGridTriggerHint();
  });
  $("#grid-trigger-drop-verify").addEventListener("click", () => {
    const rows = [...box.querySelectorAll("tbody tr")];
    let removed = 0;
    rows.forEach((tr) => {
      if (!gridTriggerRowValid(collectGridTriggerRow(tr))) {
        tr.remove();
        removed++;
      }
    });
    const count = box.querySelectorAll("tbody tr").length;
    const btn = $("#grid-trigger-confirm");
    if (btn) btn.textContent = `确认录入 ${count} 条`;
    $("#grid-trigger-note").textContent = removed
      ? `已移除 ${removed} 条待核对记录，剩余 ${count} 条`
      : `没有待核对记录（共 ${count} 条）`;
    updateGridTriggerHint();
  });
  $("#grid-trigger-confirm").addEventListener("click", confirmGridTriggers);
  $("#grid-trigger-clear").addEventListener("click", () => {
    box.innerHTML = "";
    $("#grid-trigger-note").textContent = "";
    state.gridParseImages = [];
    $("#grid-trigger-files").value = "";
  });
}

function gridTriggerRowHtml(r, i) {
  const dup = r.duplicate ? '<span class="tag unknown">重复</span>' : "";
  const verified = r.status === "ok" ? "1" : "0";
  const warnHtml = (r.warns || []).length
    ? `<div class="dim">${esc((r.warns || []).join("；"))}</div>`
    : "";
  const issueHtml = (r.issues || []).length
    ? `<div class="warn">${esc((r.issues || []).join("；"))}</div>`
    : "";
  return `
  <tr data-idx="${i}" data-status="${esc(r.status || "")}"
      data-dup="${r.duplicate ? "1" : "0"}" data-verified="${verified}">
    <td>${verifyBadge(r.status)}${dup}${warnHtml}${issueHtml}</td>
    <td><input data-f="code" value="${esc(r.code || "")}" size="6"></td>
    <td class="dim">${esc(r.name || "—")}</td>
    <td><input data-f="date" value="${esc(r.date || "")}" size="10"></td>
    <td><input data-f="time" type="time" value="${esc(r.time || "")}" size="6"></td>
    <td>
      <select data-f="action">
        <option value="buy"${r.action === "buy" ? " selected" : ""}>买入</option>
        <option value="sell"${r.action === "sell" ? " selected" : ""}>卖出</option>
      </select>
    </td>
    <td>
      <select data-f="trigger_type">
        ${["grid", "add", "reduce", "momentum"]
          .map(
            (t) =>
              `<option value="${t}"${(r.trigger_type || "grid") === t ? " selected" : ""}>${
                { grid: "网格", add: "加仓", reduce: "减仓", momentum: "动量" }[t]
              }</option>`
          )
          .join("")}
      </select>
    </td>
    <td><input data-f="price" type="number" step="0.001" value="${r.price ?? ""}" size="7"></td>
    <td><input data-f="shares" type="number" step="1" value="${r.shares ?? ""}" size="7"></td>
    <td><input data-f="base_price_before" type="number" step="0.001" value="${r.base_price_before ?? ""}" size="7"></td>
    <td><input data-f="base_price_after" type="number" step="0.001" value="${r.base_price_after ?? ""}" size="7"></td>
    <td>
      <button class="btn mini" data-verify>${verified === "1" ? "✓已核实" : "核实"}</button>
      <button class="btn mini" data-del title="删除该行">删除</button>
    </td>
  </tr>`;
}

function gridTriggerVerifyState() {
  const rows = [...$$("#grid-trigger-parse-result tbody tr")];
  return {
    total: rows.length,
    verified: rows.filter((tr) => tr.dataset.verified === "1").length,
    pending: rows.filter((tr) => tr.dataset.verified !== "1").length,
    warn: rows.filter((tr) => tr.dataset.status === "warn").length,
    err: rows.filter((tr) => tr.dataset.status === "error").length,
    dup: rows.filter((tr) => tr.dataset.dup === "1").length,
  };
}

function updateGridTriggerHint() {
  const hint = $("#grid-trigger-verify-hint");
  if (!hint) return;
  const s = gridTriggerVerifyState();
  if (s.pending) {
    hint.innerHTML =
      `<div class="advice-item"><div class="a-k">⚠ 待核实 ${s.pending} 条</div>` +
      `<div class="a-v" style="font-size:12.5px">逐条点击行内「核实」按钮；错误/警告行也可删除或修改字段后重新核实</div></div>`;
  } else {
    hint.innerHTML =
      `<div class="advice-item"><div class="a-k">✓ 全部 ${s.total} 条已核实</div>` +
      `<div class="a-v" style="font-size:12.5px">${s.dup ? `其中 ${s.dup} 条与历史重复，确认时自动跳过` : "无重复记录"}，可点「确认录入」</div></div>`;
  }
}

function collectGridTriggerRow(tr) {
  const record = {};
  $$("input[data-f], select[data-f]", tr).forEach((el) => {
    const key = el.dataset.f;
    const value = el.value.trim();
    if (
      key === "price" ||
      key === "base_price_before" ||
      key === "base_price_after" ||
      key === "shares"
    ) {
      record[key] = value === "" ? null : Number(value);
    } else {
      record[key] = value;
    }
  });
  return record;
}

function gridTriggerRowValid(r) {
  const code = String(r.code || "").trim();
  const date = String(r.date || "").trim();
  const time = String(r.time || "").trim();
  const action = String(r.action || "").trim();
  const type = String(r.trigger_type || "grid").trim();
  const price = Number(r.price);
  const shares = Number(r.shares);
  return (
    /^\d{6}$/.test(code) &&
    /^\d{4}-\d{2}-\d{2}$/.test(date) &&
    (!time || /^\d{2}:\d{2}(:\d{2})?$/.test(time)) &&
    (action === "buy" || action === "sell") &&
    ["grid", "add", "reduce", "momentum"].includes(type) &&
    price > 0 &&
    Number.isInteger(shares) &&
    shares > 0
  );
}

async function confirmGridTriggers() {
  const rows = [...$$("#grid-trigger-parse-result tbody tr")];
  const records = rows.map(collectGridTriggerRow);
  const invalidCount = records.filter((r) => !gridTriggerRowValid(r)).length;
  if (invalidCount) {
    toast(
      `还有 ${invalidCount} 条记录待核对（请修正，或点「移除需核对」）`,
      true
    );
    return;
  }
  const verifyState = gridTriggerVerifyState();
  if (verifyState.pending) {
    toast(
      `还有 ${verifyState.pending} 条待核实，请逐条点击行内「核实」后再确认`,
      true
    );
    return;
  }
  try {
    const body = await api("/api/grid/triggers/confirm", {
      method: "POST",
      body: { records },
      timeoutMs: 60000,
    });
    toast(`确认录入 ${body.data.added} 条（新增 ${body.data.records.filter((r) => r.db_status === "inserted").length} / 重复 ${body.data.records.filter((r) => r.db_status === "duplicate").length}）`);
    $("#grid-trigger-parse-result").innerHTML = "";
    $("#grid-trigger-note").textContent = "";
    state.gridParseImages = [];
    $("#grid-trigger-files").value = "";
    await renderGrid(true);
  } catch (err) {
    toast("确认失败: " + err.message, true);
  }
}

/* ============================================================
 * 网格配置：截图识别 → 核对 → 参数寻优 → 保存
 * ============================================================ */

async function parseGridConfigs() {
  const files = state.gridConfigImages || [];
  if (!files.length) {
    toast("请先选择配置截图", true);
    return;
  }
  const provider = $("#grid-config-provider").value;
  if (!provider) {
    toast("未配置可用识别模型", true);
    return;
  }
  try {
    $("#grid-config-note").textContent = "解析中（模型调用可能需要 10-60 秒）…";
    const body = await api("/api/grid/configs/parse", {
      method: "POST",
      body: { provider, images: files },
      timeoutMs: 120000,
    });
    $("#grid-config-note").textContent =
      `解析完成（${body.data.pipeline === "vision" ? "视觉模型" : "本地OCR+模型"}），` +
      `共 ${body.data.configs.length} 条配置，请核对后可「参数寻优」，确认后保存`;
    renderGridConfigResult(body.data.configs);
  } catch (err) {
    $("#grid-config-note").textContent = "";
    toast("解析失败: " + err.message, true);
  }
}

async function renderGridConfigList() {
  try {
    const body = await api("/api/grid/configs", { quiet: true });
    const configs = body.data.configs || [];
    state.savedGridConfigs = {};
    configs.forEach((c) => {
      state.savedGridConfigs[c.code] = c;
    });
    $("#grid-configs-count").textContent = `共 ${configs.length} 条`;
    $("#grid-configs-list").innerHTML = configs.length
      ? `
      <table>
        <thead><tr>
          <th>代码</th><th>名称</th><th>策略</th><th>基准价</th>
          <th>上间距%</th><th>下间距%</th><th>区间低</th><th>区间高</th>
          <th>卖出委托</th><th>买入委托</th><th>每格份</th>
          <th>下限份</th><th>上限份</th><th>层数</th><th>状态</th>
          <th>上次寻优</th><th>优化</th>
        </tr></thead>
        <tbody>
          ${configs.map((c) => `
            <tr title="${esc(c.note || "")}">
              <td>${esc(c.code)}</td>
              <td>${esc(c.name || "—")}</td>
              <td>${esc(c.strategy_type || "—")}</td>
              <td class="num">${fmtNum3(c.base_price)}</td>
              <td class="num">${c.spacing_up_pct ?? "—"}</td>
              <td class="num">${c.spacing_down_pct ?? "—"}</td>
              <td class="num">${fmtNum3(c.price_low)}</td>
              <td class="num">${fmtNum3(c.price_high)}</td>
              <td class="dim">${esc(c.order_type_sell || "—")}</td>
              <td class="dim">${esc(c.order_type_buy || "—")}</td>
              <td class="num">${c.shares_per_grid ?? "—"}</td>
              <td class="num">${c.base_position ?? "—"}</td>
              <td class="num">${c.max_position ?? "—"}</td>
              <td class="num">${c.levels_above != null ? `${c.levels_above}+${c.levels_below ?? 0}` : "—"}</td>
              <td>${esc(c.status || "—")}</td>
              <td class="num">${c.last_opt
                ? `${c.last_opt.spacing}%/${c.last_opt.levels}层/${c.last_opt.shares}股<br><span class="dim">年化 ${fmtPct(c.last_opt.annual_return_pct, 1)} · ${(c.last_opt_at || "").slice(0, 10)}</span>`
                : '<span class="dim">—</span>'}</td>
              <td><button class="btn ghost" data-grid-opt="${esc(c.code)}" style="padding:4px 8px;font-size:12px">优化</button></td>
            </tr>`).join("")}
        </tbody>
      </table>`
      : '<div class="empty">暂无已保存配置（保存后显示在这里）</div>';
    $$("#grid-configs-list [data-grid-opt]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const code = btn.dataset.gridOpt;
        const tr = btn.closest("tr");
        gridConfigOptimizeRow(code, tr);
      });
    });
  } catch (err) {
    $("#grid-configs-list").innerHTML =
      `<div class="empty">读取配置失败: ${esc(err.message)}</div>`;
  }
}

async function gridConfigOptimizeRow(code, tr) {
  // 已展开则收起
  const existing = tr.nextElementSibling;
  if (existing && existing.classList && existing.classList.contains("grid-opt-detail")) {
    existing.remove();
    return;
  }
  const detail = document.createElement("tr");
  detail.className = "grid-opt-detail";
  detail.innerHTML = `<td colspan="16"><div class="dim">参数寻优中（回测约需 10-60 秒）…</div></td>`;
  tr.after(detail);
  const cfg = (state.savedGridConfigs || {})[code] || {};
  try {
    // 资金以当前持仓金额为准
    let optCapital = 100000;
    let capitalNote = "当前无持仓，按默认 ¥100,000 优化";
    try {
      const posBody = await api("/api/grid/positions", { quiet: true });
      const pos = (posBody.data.positions || []).find((p) => p.code === code);
      if (pos && pos.market_value > 0) {
        optCapital = Math.round(pos.market_value);
        capitalNote = `按当前持仓金额 ¥${fmtNum(optCapital, 0)} 优化`;
      }
    } catch (err) { /* 持仓取不到时用默认资金 */ }
    const body = await api(
      `/api/grid/optimize?codes=${encodeURIComponent(code)}&capital=${optCapital}`,
      {
      timeoutMs: 180000,
      }
    );
    const best = (body.data.best_per_code || {})[code];
    const top = (body.data.results || [])
      .filter((r) => r.code === code)
      .slice(0, 5);
    if (!best) {
      detail.innerHTML = `<td colspan="16"><div class="empty">无寻优结果（可能数据不足）</div></td>`;
      return;
    }
    const params = body.data.params || {};
    const startLabel =
      params.start === "3y" ? "近 3 年"
      : params.start === "5y" ? "近 5 年"
      : params.start === "full" ? "全部(2018起)"
      : "近 2 年";
    const paramText =
      `${capitalNote} · 区间 ${startLabel} · 资金 ¥${fmtNum(params.capital || 0, 0)} · ` +
      `间距候选 ${(params.spacings || []).join("/")}% · ` +
      `层数候选 ${(params.levels || []).join("/")} · ` +
      `每格金额候选 ${(params.grid_values || []).join("/")}元（折算股数）`;
    const currentSpacing =
      cfg.spacing_up_pct != null && cfg.spacing_down_pct != null &&
      cfg.spacing_up_pct !== cfg.spacing_down_pct
        ? `${cfg.spacing_up_pct}/${cfg.spacing_down_pct}`
        : (cfg.spacing_up_pct ?? "—");
    const compareRows = [
      ["间距%", currentSpacing, best.spacing],
      ["层数(上/下)", cfg.levels_above != null ? `${cfg.levels_above}+${cfg.levels_below ?? 0}` : "—", `${best.levels}+${best.levels}`],
      ["每格份数", cfg.shares_per_grid ?? "—", best.shares],
      ["每格金额(元)", cfg.base_price != null && cfg.shares_per_grid != null
        ? fmtNum(cfg.base_price * cfg.shares_per_grid, 0)
        : "—", best.grid_value != null ? fmtNum(best.grid_value, 0) : "—"],
      ["持仓范围(份)", cfg.base_position != null || cfg.max_position != null
        ? `${cfg.base_position ?? "—"}~${cfg.max_position ?? "—"}`
        : "—",
        best.position_range
          ? `${best.position_range.min}~${best.position_range.max}`
          : "—"],
      ["年化收益%", "—", best.annual_return_pct != null ? fmtPct(best.annual_return_pct, 2) : "—"],
      ["最大回撤%", "—", best.max_dd_pct != null ? fmtPct(best.max_dd_pct, 2) : "—"],
      ["Sharpe", "—", best.sharpe != null ? fmtNum(best.sharpe, 2) : "—"],
      ["胜率%", "—", best.win_rate_pct != null ? fmtPct(best.win_rate_pct, 1) : "—"],
    ];
    detail.innerHTML = `<td colspan="16">
      <div class="panel-title">优化建议 · ${esc(code)} ${esc(cfg.name || "")}
        <span class="panel-sub">按年化收益排序（前 ${top.length} 组）</span>
        <button class="btn mini" data-opt-collapse style="float:right">收起</button>
      </div>
      <div class="dim" style="margin:6px 0">${esc(paramText)}</div>
      <table>
        <thead><tr><th>参数</th><th>当前配置</th><th>优化建议</th></tr></thead>
        <tbody>
          ${compareRows.map(([k, cur, opt]) => `
            <tr><td>${k}</td><td class="num">${cur}</td><td class="num ${k === "最大回撤%" ? "down" : "up"}"><b>${opt}</b></td></tr>`).join("")}
        </tbody>
      </table>
      ${top.length ? `
        <div class="dim" style="margin-top:6px">Top 候选（间距%/层数/股数/年化%）：</div>
        <div class="dim">${top.map((r) => `${r.spacing}%/${r.levels}层/${r.shares}股 ${fmtPct(r.annual_return_pct, 1)}`).join("；")}</div>` : ""}
      <div class="toolbar">
        <button class="btn" data-opt-apply>应用优化参数（更新保存）</button>
        <button class="btn" data-advise>重新研判</button>
        <span class="muted">应用后基准价/区间/委托方式保持不变；间距/层数/每格/持仓范围按候选更新</span>
      </div>
      <div id="grid-config-advise-result" style="margin-top:10px"></div>
    </td>`;
    detail.querySelector("[data-opt-collapse]").addEventListener("click", () => {
      detail.remove();
    });
    detail.querySelector("[data-opt-apply]").addEventListener("click", async () => {
      const updated = {
        ...cfg,
        spacing_up_pct: best.spacing,
        spacing_down_pct: best.spacing,
        levels_above: best.levels,
        levels_below: best.levels,
        shares_per_grid: best.shares,
        ...(best.position_range
          ? {
              base_position: best.position_range.min,
              max_position: best.position_range.max,
            }
          : {}),
        strategy_type: cfg.strategy_type || "网格交易",
        status: cfg.status || "active",
      };
      try {
        const resp = await api("/api/grid/configs/update", {
          method: "POST",
          body: { configs: [updated] },
          timeoutMs: 30000,
        });
        toast(`已应用优化参数与持仓范围并保存（${resp.data.saved} 条）`);
        await renderGridConfigList();
      } catch (err) {
        toast("应用失败: " + err.message, true);
      }
    });
    const adviseBtn = detail.querySelector("[data-advise]");
    if (adviseBtn) {
      adviseBtn.addEventListener("click", () => {
        const box = detail.querySelector("#grid-config-advise-result");
        gridConfigAdvise(code, cfg, top, box);
      });
    }
    // 每次寻优后自动调用大模型研判（结合市场环境给出适配配置）
    gridConfigAdvise(
      code,
      cfg,
      top,
      detail.querySelector("#grid-config-advise-result")
    );
  } catch (err) {
    detail.innerHTML = `<td colspan="16"><div class="empty">寻优失败: ${esc(err.message)}</div></td>`;
  }
}

async function gridConfigAdvise(code, cfg, top, box) {
  box.innerHTML = '<div class="dim">大模型研判中（约 10-30 秒）…</div>';
  try {
    const providerSel = $("#grid-config-provider");
    const provider = providerSel ? providerSel.value : "";
    const body = await api("/api/grid/configs/advise", {
      method: "POST",
      body: { code, provider, top, current_config: cfg },
      timeoutMs: 120000,
    });
    const d = body.data;
    const rec = d.recommended_config || {};
    const sameBadge = d.same_as_backtest
      ? '<span class="pass-yes">与回测最优一致</span>'
      : '<span class="pass-no">已偏离回测最优</span>';
    const ta = d.trigger_analysis || {};
    const tradesHtml = (ta.recent_trades || []).length
      ? `<details style="margin-top:4px">
           <summary class="dim" style="cursor:pointer">最近30天成交记录（${ta.recent_trades.length} 条）</summary>
           <table style="margin-top:4px">
             <thead><tr><th>日期</th><th>时间</th><th>方向</th><th>类型</th><th>价格</th><th>数量</th></tr></thead>
             <tbody>
               ${ta.recent_trades.map((t) => {
                 const sell = String(t.action || "").includes("卖");
                 return `
                 <tr>
                   <td class="num">${esc(t.date || "")}</td>
                   <td class="num">${esc(t.time || "—")}</td>
                   <td class="${sell ? "down" : "up"}">${sell ? "卖" : "买"}</td>
                   <td class="dim">${esc(t.type || "grid")}</td>
                   <td class="num">${t.price}</td>
                   <td class="num">${t.shares}</td>
                 </tr>`;
               }).join("")}
             </tbody>
           </table>
         </details>`
      : "";
    const taHtml = ta.has_triggers
      ? `<div class="panel-title" style="margin-top:8px">触发记录分析 · ${esc(code)}</div>
         <div class="dim">共 ${ta.count} 条 · 网格自动 ${ta.grid_count} 条（买 ${ta.buys} / 卖 ${ta.sells}）
           ${ta.add_count ? `· 主动加仓 ${ta.add_count} 条` : ""}
           ${ta.reduce_count ? `· 主动减仓 ${ta.reduce_count} 条` : ""}
           <br>网格跨度 ${ta.span_days} 天 · 日均 ${ta.freq_per_day} 次
           · 最近网格链 ${esc(ta.recent_chain || "—")}
           · 网格买均价 ${ta.avg_buy ?? "—"} / 卖均价 ${ta.avg_sell ?? "—"}
           ${ta.grid_shares ? `· 网格累计 ${ta.grid_shares} 股` : ""}</div>
         ${(ta.notes || []).map((n) => `<div class="dim" style="margin-top:2px">ℹ ${esc(n)}</div>`).join("")}
         ${tradesHtml}
         <div class="${ta.issues && ta.issues.length ? "down" : "up"}" style="margin-top:4px">
           ${ta.issues && ta.issues.length ? esc(ta.issues.join("；")) : esc(ta.verdict || "")}
         </div>`
      : `<div class="dim" style="margin-top:6px">暂无触发记录，无法评估实际触发行为</div>`;
    const factorHtml = (d.factors || [])
      .map((f) => `
        <tr>
          <td>${esc(f.factor || "")}</td>
          <td class="${f.impact === "利好" ? "up" : f.impact === "利空" ? "down" : "dim"}">${esc(f.impact || "—")}</td>
          <td class="dim">${esc(f.detail || "")}</td>
        </tr>`)
      .join("");
    box.innerHTML = `
      <div class="panel-title">大模型市场研判 · ${esc(code)}
        <span class="panel-sub">宏观 / 地缘 / 周期 / 汇率等影响因子</span>
      </div>
      <div class="advice-item"><div class="a-k">市场判断</div>
        <div class="a-v">${esc(d.market_verdict || "—")}（信心 ${d.confidence != null ? Math.round(d.confidence * 100) : "—"}%）</div></div>
      <div class="advice-item"><div class="a-k">方向评估</div>
        <div class="a-v">${esc(d.direction_assessment || "—")}</div></div>
      <div class="advice-item"><div class="a-k">与回测最优的关系</div>
        <div class="a-v">${sameBadge}
          <span class="dim" style="margin-left:4px">${esc(d.deviation_note || "")}</span></div></div>
      <table>
        <thead><tr><th>因素</th><th>影响</th><th>说明</th></tr></thead>
        <tbody>${factorHtml || '<tr><td colspan="3" class="dim">无</td></tr>'}</tbody>
      </table>
      ${taHtml}
      <div class="panel-title" style="margin-top:8px">建议配置</div>
      <div>间距 <b>${rec.spacing_up_pct ?? "—"}%</b>/<b>${rec.spacing_down_pct ?? "—"}%</b>
        · 层数 <b>${rec.levels_above ?? "—"}+${rec.levels_below ?? "—"}</b>
        · 每格 <b>${rec.shares_per_grid ?? "—"}</b> 股
        · 持仓 <b>${rec.base_position ?? "—"}~${rec.max_position ?? "—"}</b></div>
      <div class="dim" style="margin-top:4px">${esc(d.reasoning || "")}</div>
      ${(d.risks || []).length ? `<div class="down" style="margin-top:4px">风险：${esc((d.risks || []).join("；"))}</div>` : ""}
      <div class="toolbar">
        <button class="btn" data-advise-apply>应用建议配置（更新保存）</button>
      </div>
      <details style="margin-top:8px">
        <summary class="dim" style="cursor:pointer">对话注释（输入上下文 + 模型原文）</summary>
        <div style="white-space:pre-wrap;font-size:12px;margin-top:4px">
          <span class="dim">【输入给模型】</span>
${esc(d.input_context || "")}
        </div>
        <div style="white-space:pre-wrap;font-size:12px;margin-top:6px">
          <span class="dim">【模型返回原文】</span>
${esc(d.raw_text || "")}
        </div>
      </details>`;
    box.querySelector("[data-advise-apply]").addEventListener("click", async () => {
      const updated = {
        ...cfg,
        ...rec,
        strategy_type: cfg.strategy_type || "网格交易",
        status: cfg.status || "active",
      };
      try {
        await api("/api/grid/configs/update", {
          method: "POST",
          body: { configs: [updated] },
          timeoutMs: 30000,
        });
        toast("已应用大模型建议配置并保存");
        await renderGridConfigList();
      } catch (err) {
        toast("应用失败: " + err.message, true);
      }
    });
  } catch (err) {
    box.innerHTML = `<div class="empty">研判失败: ${esc(err.message)}</div>`;
  }
}

/* ============================================================
 * 网格选品池：全市场 ETF/LOF 适配评分
 * ============================================================ */

async function renderGridScreener() {
  const type = $("#grid-screener-type").value;
  const t0 = $("#grid-screener-t0").value;
  const category = $("#grid-screener-category").value;
  const minSize = $("#grid-screener-size").value || "0";
  const minScore = $("#grid-screener-score").value || "0";
  const minVol = $("#grid-screener-vol-min").value || "";
  const maxVol = $("#grid-screener-vol-max").value || "";
  const minAmount = $("#grid-screener-amount").value || "0";
  const maxTrend = $("#grid-screener-trend").value || "";
  const minAmplitude = $("#grid-screener-amplitude").value || "0";
  const maxDd = $("#grid-screener-drawdown").value || "";
  const trendScore = $("#grid-screener-trend-score").value || "";
  const sort = $("#grid-screener-sort").value;
  $("#grid-screener-note").textContent = "筛选中…";
  try {
    const body = await api(
      `/api/grid/screener?category=${encodeURIComponent(category)}` +
      `&lof=${type === "lof" ? "1" : "0"}&t0=${t0}&min_size=${minSize}` +
      `&min_score=${minScore}&min_vol=${minVol}&max_vol=${maxVol}` +
      `&min_amount=${minAmount}&max_trend=${maxTrend}` +
      `&min_amplitude=${minAmplitude}&max_dd=${maxDd}` +
      `&trend_min=${trendScore}&sort=${sort}`,
      { quiet: true }
    );
    const rows = body.data.rows || [];
    const cats = body.data.categories || [];
    const catSel = $("#grid-screener-category");
    if (catSel && !catSel.dataset.loaded) {
      catSel.dataset.loaded = "1";
      catSel.innerHTML =
        '<option value="">全部</option>' +
        cats.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
    }
    $("#grid-screener-note").textContent =
      `共 ${body.data.total} 只（显示前 300，评分≥${minScore}）`;
    $("#grid-screener-result").innerHTML = rows.length
      ? `
      <table>
        <thead><tr>
          <th data-metric="code">代码 ⓘ</th><th data-metric="name">名称 ⓘ</th>
          <th data-metric="type">类型 ⓘ</th><th data-metric="t0">T+0 ⓘ</th>
          <th data-metric="category">类别 ⓘ</th><th data-metric="close">现价 ⓘ</th>
          <th data-metric="vol">波动% ⓘ</th><th data-metric="trend_score">趋势评分 ⓘ</th>
          <th data-metric="trend20">20日趋势% ⓘ</th><th data-metric="amplitude">振幅% ⓘ</th>
          <th data-metric="bb">BB宽% ⓘ</th><th data-metric="rsi">RSI ⓘ</th>
          <th data-metric="amount">成交额万 ⓘ</th><th data-metric="size">规模亿 ⓘ</th>
          <th data-metric="dd">回撤% ⓘ</th><th data-metric="grid_score">评分 ⓘ</th>
          <th>操作</th>
        </tr></thead>
        <tbody>
          ${rows.map((r) => `
            <tr data-screen-code="${esc(r.code)}">
              <td>${esc(r.code)}</td>
              <td>${esc(r.name)}</td>
              <td>${esc(r.type)}</td>
              <td>${r.t0 ? '<span class="pass-yes">T+0</span>' : '<span class="dim">T+1</span>'}</td>
              <td class="dim">${esc(r.subcategory || r.category || "—")}</td>
              <td class="num">${r.close != null ? fmtNum3(r.close) : "—"}</td>
              <td class="num">${r.vol_pct ?? "—"}</td>
              <td class="num ${r.trend_score != null && r.trend_score > 0 ? "up" : r.trend_score != null && r.trend_score < 0 ? "down" : ""}">${r.trend_score ?? "—"}</td>
              <td class="num ${pctCls(r.trend20_pct)}">${r.trend20_pct ?? "—"}</td>
              <td class="num">${r.amplitude ?? "—"}</td>
              <td class="num">${r.bb_width ?? "—"}</td>
              <td class="num">${r.rsi ?? "—"}</td>
              <td class="num">${r.avg_amount_wan != null ? fmtNum(r.avg_amount_wan, 0) : "—"}</td>
              <td class="num">${r.fund_size_yi}</td>
              <td class="num">${r.max_dd_pct ?? "—"}</td>
              <td class="num ${r.grid_score >= 15 ? "up" : r.grid_score >= 11 ? "" : "down"}"><b>${r.grid_score}</b></td>
              <td><button class="btn ghost" data-grid-screen-opt style="padding:4px 8px;font-size:12px">寻优</button></td>
            </tr>`).join("")}
        </tbody>
      </table>`
      : '<div class="empty">无符合条件的标的（可降低评分/规模门槛）</div>';
    $$("#grid-screener-result [data-grid-screen-opt]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tr = btn.closest("tr[data-screen-code]");
        gridScreenOptimize(tr.dataset.screenCode, tr.querySelector("td:nth-child(2)").textContent, tr);
      });
    });
    bindMetricHints();
  } catch (err) {
    $("#grid-screener-note").textContent = "";
    $("#grid-screener-result").innerHTML =
      `<div class="empty">筛选失败: ${esc(err.message)}</div>`;
  }
}

// 全页面指标说明字典：key 与各表格 <th data-metric="key"> 对应。点击表头的「ⓘ」弹出说明。
const METRIC_HELP = {
  // ── 网格选品池 ──
  code: "证券代码（6 位）。",
  name: "基金名称。",
  type: "ETF 或 LOF（按代码规则判断：16xxxx / 501-506 为 LOF，其余为 ETF）。",
  t0: "是否支持 T+0 交易。跨境/商品/债券/货币基金 T+0，A股股票 ETF/LOF 为 T+1。T+0 可日内进出，网格更灵活（评分 +1）。",
  category: "资产类别：A股宽基 / A股行业 / 跨境ETF / 商品ETF / 债券ETF / 货币基金。",
  close: "最新收盘价（来自 K 线缓存）。",
  vol: "年化波动率：近 20 日收益率标准差 × √252。年化 15-35% 是网格甜区（评分 5 分）。",
  trend_score: "趋势评分（-5 空头 ~ +5 多头）：按 MA20/MA60 排列 + 20 日趋势估算。震荡偏弱适合网格，单边空头不适合。",
  trend20: "近 20 个交易日涨跌幅（%）。|趋势| 越小越偏震荡，网格越有利。",
  amplitude: "每日振幅：近 60 日平均（最高价−最低价）÷ 昨收 × 100%，衡量日内震荡幅度与网格触发机会。",
  bb: "布林带宽度：4×σ(20) ÷ MA20 × 100%，衡量波动带宽与均值回归空间。",
  rsi: "RSI14 相对强弱指标。极端超买（>70）/超卖（<30）提示短期反转风险。",
  amount: "日均成交额（万元）：按近 60 日成交量 × 价格 × 100 估算，衡量流动性。",
  size: "基金规模（亿元），来自 ETF 元数据。规模越大流动性/清盘风险越低。",
  dd: "历史最大回撤（%），来自全市场回测。网格需控制单边深跌风险，回撤越小越稳。",
  grid_score: "网格适配评分（满分 21）：波动率甜区 5 + 流动性 5 + 均值回归 5 + 回撤可控 5 + T+0 加分 1。",

  // ── 通用绩效指标 ──
  annual: "年化收益率：把区间总收益折算成一年，便于跨不同时长比较。=(1+总收益)^(365/区间天数)−1。",
  total: "区间总收益率：期末权益 ÷ 期初本金 − 1，未年化。",
  max_dd: "最大回撤：区间内权益从峰值到谷底的最大跌幅，衡量最坏情况下的亏损深度。",
  sharpe: "夏普比率：年化超额收益 ÷ 年化波动（无风险利率 ≈2%）。>1 良好，>2 优秀。",
  sortino: "Sortino 比率：年化超额收益 ÷ 下行波动，只惩罚下行风险。",
  calmar: "卡玛比率：年化收益 ÷ 最大回撤。>1 表示年化收益能覆盖最大回撤。",
  dsr: "DSR 显著性概率：扣除「从 N 组里选最优」的运气成分后，该组合仍显著优于纯运气的概率。>0.95 显著；但只校正采样运气，不校正样本内 regime 依赖——最终以样本外验证为准。",
  win_rate: "胜率：盈利卖出交易数 ÷ 总卖出交易数。",
  trades: "交易次数：区间内买入 + 卖出总次数。",
  excess: "超额 α：策略年化收益 − 买入持有基准年化。>0 表示跑赢无脑持有。",
  benchmark: "基准年化：买入持有（B&H）基准的年化收益，作对照。",
  grade: "等级：综合 Sharpe / 胜率 / 盈亏比打的 A+~F 评级。",
  annual_vol: "年化波动：日收益率标准差 × √252，衡量风险。",

  // ── 样本外验证（Walk-Forward）──
  oos_total: "样本外总收益：Walk-Forward 各折测试段拼接后的总收益（选参时未用到的数据，更能反映真实可复制性）。",
  oos_annual: "样本外年化：样本外总收益折算的年化。",
  oos_sharpe: "样本外夏普：样本外测试段收益的夏普比率。",
  selected: "被选中次数：该组合在 Walk-Forward 各折 train 段被选为最优的次数 / 总折数。越高说明入选越稳定。",

  // ── 网格参数 ──
  spacing: "间距%：每格涨跌百分比，网格触发买卖的步长。",
  levels: "层数：基准价上下各挂几层。",
  shares: "每格股数：每层委托股数。",
  grid_value: "每格金额：每层资金（股数 × 基准价）。",
  position_range: "持仓范围：底仓到满仓的股数区间。",
  base_price: "基准价：网格中枢价，围绕它上下挂层。",
  price_range: "价格区间：网格覆盖的最低 ~ 最高价。",
  buy_sell: "买/卖：区间内买入与卖出触发次数。",

  // ── 动量 / 全市场 ──
  rsrs: "RSRS：阻力支撑相对强度，动量信号的核心指标。",
  slope: "年化斜率：RSRS 回归斜率的年化值。",
  signal_wr10: "信号胜率 10d：突破信号出现后 10 个交易日上涨的比例。",
  signal_wr20: "信号胜率 20d：突破信号出现后 20 个交易日上涨的比例。",
  composite: "综合评分：多维打分加权合计（0-100，越高越优；具体维度因模块而异）。",
  cagr: "CAGR：复合年化增长率（Compound Annual Growth Rate），即区间年化收益率。",
  ic: "IC：信息系数（Information Coefficient），因子预测值与未来收益的秩相关，衡量因子有效性。",
  ir: "IR：信息比率（Information Ratio），IC 均值 ÷ IC 标准差，衡量因子稳定度。",
  forward_window: "前向窗口：因子信号后观察未来收益的持有期长度。",
  asset_dd: "资产回撤：该情景下资产（买入持有）的区间最大回撤。",
  strat_return: "策略收益：该情景下策略的区间收益，与资产回撤对照看抗跌能力。",
};

function bindMetricHints() {
  let popup = document.getElementById("metric-popup");
  if (!popup) {
    popup = document.createElement("div");
    popup.id = "metric-popup";
    popup.style.cssText =
      "position:fixed;z-index:9999;max-width:340px;padding:10px 12px;" +
      "background:#fff;border:1px solid #d4d4d8;border-radius:8px;" +
      "box-shadow:0 4px 16px rgba(0,0,0,.18);font-size:12.5px;line-height:1.55;" +
      "color:#27272a;display:none;";
    document.body.appendChild(popup);
  }
  // 事件委托：任意带 data-metric 的表头点击即弹说明，动态渲染的表格无需重复绑定。
  if (bindMetricHints._bound) return;
  bindMetricHints._bound = true;
  document.addEventListener("click", (ev) => {
    const th = ev.target.closest("th[data-metric]");
    if (th) {
      const text = METRIC_HELP[th.dataset.metric];
      popup.textContent = text || th.textContent;
      const rect = th.getBoundingClientRect();
      popup.style.display = "block";
      popup.style.left = Math.min(rect.left, window.innerWidth - popup.offsetWidth - 12) + "px";
      popup.style.top = (rect.bottom + 8) + "px";
    } else if (!ev.target.closest("#metric-popup")) {
      popup.style.display = "none";
    }
  });
  document.addEventListener("mouseover", (ev) => {
    const th = ev.target.closest("th[data-metric]");
    if (th) th.style.cursor = "pointer";
  });
}

async function gridScreenOptimize(code, name, tr) {
  const existing = tr.nextElementSibling;
  if (existing && existing.classList && existing.classList.contains("grid-screen-detail")) {
    existing.remove();
    return;
  }
  const detail = document.createElement("tr");
  detail.className = "grid-screen-detail";
  detail.innerHTML = `<td colspan="17"><div class="dim">寻优中（约 10-60 秒）…</div></td>`;
  tr.after(detail);
  try {
    const body = await api(`/api/grid/optimize?codes=${encodeURIComponent(code)}`, {
      timeoutMs: 180000,
    });
    const best = (body.data.best_per_code || {})[code];
    if (!best) {
      detail.innerHTML = `<td colspan="17"><div class="empty">无寻优结果</div></td>`;
      return;
    }
    const pr = best.position_range || {};
    const params = body.data.params || {};
    const startLabel =
      params.start === "3y" ? "近 3 年" :
      params.start === "5y" ? "近 5 年" :
      params.start === "full" ? "全部(2018起)" : "近 2 年";
    detail.innerHTML = `<td colspan="17">
      <div class="panel-title">寻优建议 · ${esc(code)} ${esc(name || "")}
        <span class="panel-sub">区间 ${startLabel} · 资金 ¥${fmtNum(params.capital || 0, 0)} · 按年化排序</span>
      </div>
      <table>
        <thead><tr><th data-metric="spacing">间距% ⓘ</th><th data-metric="levels">层数 ⓘ</th><th data-metric="shares">每格股数 ⓘ</th><th data-metric="grid_value">每格金额 ⓘ</th>
          <th data-metric="position_range">持仓范围 ⓘ</th><th data-metric="annual">年化% ⓘ</th><th data-metric="benchmark">基准年化 ⓘ</th><th data-metric="excess">超额α ⓘ</th><th data-metric="total">总收益% ⓘ</th><th data-metric="max_dd">回撤% ⓘ</th><th data-metric="sharpe">Sharpe ⓘ</th></tr></thead>
        <tbody><tr>
          <td class="num"><b>${best.spacing}</b></td>
          <td class="num"><b>${best.levels}</b></td>
          <td class="num"><b>${best.shares}</b></td>
          <td class="num">${fmtNum(best.grid_value, 0)}</td>
          <td class="num">${pr.min ?? "—"}~${pr.max ?? "—"}</td>
          <td class="num up">${fmtPct(best.annual_return_pct, 2)}</td>
          <td class="num ${pctCls(best.buy_and_hold_annual_pct)}">${fmtPct(best.buy_and_hold_annual_pct, 2)}</td>
          <td class="num ${pctCls(best.alpha_pct)}">${fmtPct(best.alpha_pct, 1, true)}</td>
          <td class="num">${fmtPct(best.total_return_pct, 2)}</td>
          <td class="num down">${fmtPct(best.max_dd_pct, 2)}</td>
          <td class="num">${best.sharpe == null ? "—" : fmtNum(best.sharpe, 2)}</td>
        </tr></tbody>
      </table>
      <div class="toolbar">
        <button class="btn" data-screen-save>保存为网格配置</button>
      </div>
      ${best.beat_benchmark === false ? `<div class="dim" style="color:var(--warn);margin-top:8px">⚠ 该配置年化 ${fmtPct(best.annual_return_pct, 2)} 跑输买入持有基准 ${fmtPct(best.buy_and_hold_annual_pct, 2)}（超额 α ${fmtPct(best.alpha_pct, 1, true)}），保存前请确认</div>` : ""}
      <div id="grid-screen-advise"></div>
    </td>`;
    detail.querySelector("[data-screen-save]").addEventListener("click", async () => {
      if (best.beat_benchmark === false &&
          !confirm(`该配置年化 ${fmtPct(best.annual_return_pct, 2)} 跑输买入持有基准 ${fmtPct(best.buy_and_hold_annual_pct, 2)}（超额 α ${fmtPct(best.alpha_pct, 1, true)}），确认仍要保存？`)) {
        return;
      }
      const base = best.grid_config && best.grid_config.base_price;
      const cfg = {
        code,
        name,
        strategy_type: "网格交易",
        base_price: base != null ? base : (best.grid_config && best.grid_config.base_price),
        spacing_up_pct: best.spacing,
        spacing_down_pct: best.spacing,
        price_low: best.grid_config && best.grid_config.price_range && best.grid_config.price_range.min,
        price_high: best.grid_config && best.grid_config.price_range && best.grid_config.price_range.max,
        order_type_sell: "限价即时买一价卖出",
        order_type_buy: "限价即时卖一价买入",
        shares_per_grid: best.shares,
        levels_above: best.levels,
        levels_below: best.levels,
        base_position: pr.min,
        max_position: pr.max,
        status: "active",
      };
      try {
        await api("/api/grid/configs/update", {
          method: "POST",
          body: { configs: [cfg] },
          timeoutMs: 30000,
        });
        toast(`已保存 ${code} 为网格配置`);
        await renderGridConfigList();
      } catch (err) {
        toast("保存失败: " + err.message, true);
      }
    });
    const cfgForAdvise = { code, name };
    gridConfigAdvise(code, cfgForAdvise, (body.data.results || []).slice(0, 5), detail.querySelector("#grid-screen-advise"));
  } catch (err) {
    detail.innerHTML = `<td colspan="17"><div class="empty">寻优失败: ${esc(err.message)}</div></td>`;
  }
}

function gridConfigNum(v) {
  return v == null || v === "" ? "" : v;
}

function gridConfigRowHtml(c, i) {
  return `
  <tr data-config-index="${i}" title="${esc((c.issues || []).join("；"))}">
    <td>${verifyBadge(c.status)}</td>
    <td><input class="edit-input" data-cfg="code" value="${esc(c.code || "")}" size="7"></td>
    <td><input class="edit-input" data-cfg="name" value="${esc(c.name || "")}" size="9"></td>
    <td><input class="edit-input num" data-cfg="base_price" type="number" step="0.001" value="${gridConfigNum(c.base_price)}" size="6"></td>
    <td><input class="edit-input num" data-cfg="spacing_up_pct" type="number" step="0.1" value="${gridConfigNum(c.spacing_up_pct)}" size="4"></td>
    <td><input class="edit-input num" data-cfg="spacing_down_pct" type="number" step="0.1" value="${gridConfigNum(c.spacing_down_pct)}" size="4"></td>
    <td><input class="edit-input num" data-cfg="price_low" type="number" step="0.001" value="${gridConfigNum(c.price_low)}" size="6"></td>
    <td><input class="edit-input num" data-cfg="price_high" type="number" step="0.001" value="${gridConfigNum(c.price_high)}" size="6"></td>
    <td><input class="edit-input" data-cfg="order_type_sell" value="${esc(c.order_type_sell || "")}" size="8"></td>
    <td><input class="edit-input" data-cfg="order_type_buy" value="${esc(c.order_type_buy || "")}" size="8"></td>
    <td><input class="edit-input num" data-cfg="shares_per_grid" type="number" step="100" value="${gridConfigNum(c.shares_per_grid)}" size="5"></td>
    <td><input class="edit-input num" data-cfg="base_position" type="number" step="100" value="${gridConfigNum(c.base_position)}" size="6"></td>
    <td><input class="edit-input num" data-cfg="max_position" type="number" step="100" value="${gridConfigNum(c.max_position)}" size="6"></td>
    <td>
      <button class="btn ghost" data-config-verify style="padding:4px 8px;font-size:12px">核实</button>
      <button class="btn ghost" data-config-optimize style="padding:4px 8px;font-size:12px">参数寻优</button>
    </td>
  </tr>`;
}

function collectGridConfigRow(tr) {
  const out = { strategy_type: "网格交易", status: "active" };
  tr.querySelectorAll("[data-cfg]").forEach((input) => {
    const key = input.dataset.cfg;
    const raw = input.value.trim();
    if (raw === "") {
      out[key] = null;
    } else if (input.type === "number") {
      const n = Number(raw);
      out[key] = Number.isFinite(n) ? n : raw;
    } else {
      out[key] = raw;
    }
  });
  return out;
}

function renderGridConfigResult(configs) {
  const box = $("#grid-config-parse-result");
  if (!configs.length) {
    box.innerHTML = '<div class="empty">未识别到网格配置</div>';
    return;
  }
  box.innerHTML = `
    <div class="advice-banner" id="grid-config-verify-hint"></div>
    <div class="table-scroll" style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>核验</th><th>代码</th><th>名称</th><th>基准价</th>
        <th>上间距%</th><th>下间距%</th><th>区间低</th><th>区间高</th>
        <th>卖出委托</th><th>买入委托</th><th>每格份</th>
        <th>下限份</th><th>上限份</th><th>操作</th>
      </tr></thead>
      <tbody>
        ${configs.map((c, i) => gridConfigRowHtml(c, i)).join("")}
      </tbody>
    </table>
    </div>
    <div id="grid-config-optimize-result" style="margin-top:10px"></div>
    <div class="toolbar">
      <button class="btn" id="grid-config-verify-all">全部核实</button>
      <button class="btn" id="grid-config-confirm">确认保存 ${configs.length} 条</button>
      <button class="btn" id="grid-config-clear">清空</button>
    </div>`;
  updateGridConfigHint(configs);
  $("#grid-config-verify-all").addEventListener("click", () => {
    $$("#grid-config-parse-result tbody tr").forEach((tr) => {
      tr.dataset.verified = "1";
    });
    updateGridConfigHint();
    toast("已全部标记核实，可直接保存");
  });
  $("#grid-config-confirm").addEventListener("click", confirmGridConfigs);
  $("#grid-config-clear").addEventListener("click", () => {
    box.innerHTML = "";
    $("#grid-config-note").textContent = "";
    state.gridConfigImages = [];
    $("#grid-config-files").value = "";
  });
  box.addEventListener("click", async (ev) => {
    const verifyBtn = ev.target.closest("[data-config-verify]");
    if (verifyBtn) {
      const tr = verifyBtn.closest("tr");
      tr.dataset.verified = "1";
      verifyBtn.textContent = "✓已核实";
      updateGridConfigHint();
      return;
    }
    const optBtn = ev.target.closest("[data-config-optimize]");
    if (optBtn) {
      const tr = optBtn.closest("tr");
      const cfg = collectGridConfigRow(tr);
      if (!cfg.code) {
        toast("请先填写证券代码再寻优", true);
        return;
      }
      optBtn.disabled = true;
      optBtn.textContent = "寻优中…";
      try {
        await runGridConfigOptimize(cfg.code, tr);
      } finally {
        optBtn.disabled = false;
        optBtn.textContent = "参数寻优";
      }
    }
  });
}

function updateGridConfigHint(configs) {
  const hint = $("#grid-config-verify-hint");
  if (!hint) return;
  const rows = [...$$("#grid-config-parse-result tbody tr")];
  const pending = rows.filter((tr) => tr.dataset.verified !== "1").length;
  hint.innerHTML = pending
    ? `<div class="a-t">ℹ ${pending} 条尚未逐条核实（可选；保存只拦截错误项，也可点「全部核实」）</div>`
    : `<div class="a-t">✅ 全部已核实，可「参数寻优」或直接保存</div>`;
}

async function runGridConfigOptimize(code, tr) {
  const box = $("#grid-config-optimize-result");
  box.innerHTML = '<div class="dim">参数寻优中（回测约需 10-60 秒）…</div>';
  // 拉取网格总览：当前价 + 数据库已存配置，用于补全空字段
  let overviewItem = {};
  try {
    const overview = await api("/api/grid", { quiet: true });
    overviewItem =
      (overview.data.items || []).find((i) => i.code === code) || {};
  } catch (err) {
    /* 拿不到总览时只填空间距/股数 */
  }
  const currentPrice = overviewItem.current_price;
  const dbCfg = overviewItem.config || {};
  const fillIfEmpty = (row, key, value) => {
    const el = row.querySelector(`[data-cfg="${key}"]`);
    if (el && String(el.value).trim() === "") el.value = value;
  };
  const body = await api(`/api/grid/optimize?codes=${encodeURIComponent(code)}`, {
    timeoutMs: 180000,
  });
  const results = (body.data.results || []).filter((r) => r.code === code);
  const best = (body.data.best_per_code || {})[code];
  const top = results.slice(0, 8);
  if (!top.length) {
    box.innerHTML = '<div class="empty">未返回寻优结果</div>';
    return;
  }
  box.innerHTML = `
    <div class="panel-title">参数寻优建议 · ${code} <span class="panel-sub">按年化收益排序，点「应用」填入上方该行</span></div>
    <table>
      <thead><tr><th data-metric="spacing">间距% ⓘ</th><th data-metric="levels">层数 ⓘ</th><th data-metric="shares">每格股数 ⓘ</th><th data-metric="grid_value">每格金额 ⓘ</th>
        <th data-metric="annual">年化% ⓘ</th><th data-metric="benchmark">基准年化 ⓘ</th><th data-metric="excess">超额α ⓘ</th><th data-metric="total">总收益% ⓘ</th><th data-metric="max_dd">最大回撤% ⓘ</th><th data-metric="sharpe">Sharpe ⓘ</th><th data-metric="win_rate">胜率% ⓘ</th><th></th></tr></thead>
      <tbody>
        ${top.map((r) => `
          <tr>
            <td class="num">${r.spacing}</td>
            <td class="num">${r.levels}</td>
            <td class="num">${r.shares}</td>
            <td class="num">${fmtNum(r.grid_value, 0)}</td>
            <td class="num up">${fmtPct(r.annual_return_pct, 2)}</td>
            <td class="num ${pctCls(r.buy_and_hold_annual_pct)}">${fmtPct(r.buy_and_hold_annual_pct, 2)}</td>
            <td class="num ${pctCls(r.alpha_pct)}">${fmtPct(r.alpha_pct, 1, true)}</td>
            <td class="num">${fmtPct(r.total_return_pct, 2)}</td>
            <td class="num down">${fmtPct(r.max_dd_pct, 2)}</td>
            <td class="num">${r.sharpe == null ? "—" : fmtNum(r.sharpe, 2)}</td>
            <td class="num">${r.win_rate_pct == null ? "—" : fmtPct(r.win_rate_pct, 1)}</td>
            <td><button class="btn ghost" data-config-apply="${r.spacing}|${r.levels}|${r.shares}" style="padding:4px 8px;font-size:12px">应用</button></td>
          </tr>`).join("")}
      </tbody>
    </table>
    ${best ? `<div class="dim">最优组合：间距 ${best.spacing}% / ${best.levels} 层 / ${best.shares} 股（每格≈¥${fmtNum(best.grid_value, 0)}），年化 ${fmtPct(best.annual_return_pct, 2)}，最大回撤 ${fmtPct(best.max_dd_pct, 2)}${best.beat_benchmark === false ? ' <span class="badge-warn">⚠跑输基准，谨慎采用</span>' : ""}</div>` : ""}`;
  box.querySelectorAll("[data-config-apply]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const [spacing, levels, shares] = btn.dataset.configApply.split("|");
      const spacingN = Number(spacing);
      const levelsN = Number(levels);
      // 只补空字段：OCR 已识别的内容优先保留
      fillIfEmpty(tr, "base_price", currentPrice != null ? currentPrice : (dbCfg.base_price ?? ""));
      fillIfEmpty(tr, "spacing_up_pct", spacing);
      fillIfEmpty(tr, "spacing_down_pct", spacing);
      fillIfEmpty(tr, "shares_per_grid", shares);
      const baseVal = Number(tr.querySelector('[data-cfg="base_price"]').value);
      if (baseVal > 0 && levelsN > 0) {
        fillIfEmpty(
          tr, "price_low",
          (baseVal * Math.pow(1 - spacingN / 100, levelsN)).toFixed(3)
        );
        fillIfEmpty(
          tr, "price_high",
          (baseVal * Math.pow(1 + spacingN / 100, levelsN)).toFixed(3)
        );
      }
      fillIfEmpty(tr, "order_type_sell", "限价即时买一价卖出");
      fillIfEmpty(tr, "order_type_buy", "限价即时卖一价买入");
      fillIfEmpty(tr, "base_position", dbCfg.base_position ?? "");
      fillIfEmpty(tr, "max_position", dbCfg.max_position ?? "");
      tr.dataset.verified = "1";
      updateGridConfigHint();
      toast(`已应用：间距 ${spacing}% / ${levels} 层 / ${shares} 股，空字段已按现价/DB配置补全，请核对后保存`);
    });
  });
}

async function confirmGridConfigs() {
  const rows = [...$$("#grid-config-parse-result tbody tr")];
  const configs = rows.map(collectGridConfigRow);
  try {
    const body = await api("/api/grid/configs/update", {
      method: "POST",
      body: { configs },
      timeoutMs: 30000,
    });
    toast(`网格配置已保存 ${body.data.saved} 条`);
    $("#grid-config-parse-result").innerHTML = "";
    $("#grid-config-note").textContent = "";
    state.gridConfigImages = [];
    $("#grid-config-files").value = "";
    await renderGrid(true);
  } catch (err) {
    toast("保存失败: " + err.message, true);
  }
}

async function renderGridOptimize(force) {
  const codes = $("#grid-opt-codes").value.trim();
  const capital = ($("#grid-opt-capital").value || "100000").trim();
  const start = $("#grid-opt-start").value;
  const query =
    `codes=${encodeURIComponent(codes)}&capital=${capital}` +
    `&start=${start}${force ? "&refresh=1" : ""}`;
  const body = await api(`/api/grid/optimize?${query}`, {
    progress: "正在并行扫描网格参数寻优（多标的并行，首次可能需要 1-2 分钟）…",
    timeoutMs: force ? 900000 : 180000,
  });
  const data = body.data;
  const results = data.results || [];
  const best = Object.values(data.best_per_code || {});
  const params = data.params || {};
  const startLabel =
    params.start === "3y" ? "近 3 年"
    : params.start === "5y" ? "近 5 年"
    : params.start === "full" ? "全部(2018起)"
    : "近 2 年";
  $("#grid-opt-note").textContent =
    `${params.codes ? params.codes.length : 0} 个标的 × ` +
    `内置候选（间距 ${(params.spacings || []).join("/")}%、层数 ${(params.levels || []).join("/")}、` +
    `每格金额 ${(params.grid_values || []).join("/")} 元折算股数）= ${results.length} 组 · ` +
    `资金 ¥${fmtNum(params.capital || 0, 0)} · 区间 ${startLabel}`;

  $("#grid-opt-best").innerHTML = best.length
    ? `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th data-metric="spacing">最优间距 ⓘ</th><th data-metric="levels">层数 ⓘ</th><th data-metric="shares">每格股数 ⓘ</th>
        <th data-metric="grid_value">每格金额 ⓘ</th><th data-metric="base_price">基准价 ⓘ</th><th data-metric="price_range">价格区间 ⓘ</th><th data-metric="position_range">持仓区间 ⓘ</th>
        <th data-metric="annual">年化 ⓘ</th><th data-metric="benchmark">基准年化 ⓘ</th><th data-metric="excess">超额α ⓘ</th><th data-metric="total">总收益 ⓘ</th><th data-metric="max_dd">MaxDD ⓘ</th><th data-metric="sharpe">Sharpe ⓘ</th><th data-metric="win_rate">胜率 ⓘ</th><th data-metric="grade">等级 ⓘ</th>
      </tr></thead>
      <tbody>
        ${best
          .map((r) => {
            const gc2 = r.grid_config || {};
            const pr = gc2.price_range || {};
            const posr = gc2.position_range || {};
            return `
            <tr class="sel">
              <td>${esc(r.code)}</td>
              <td>${esc(r.name)}</td>
              <td class="num">${r.spacing}%</td>
              <td class="num">${r.levels}</td>
              <td class="num">${fmtNum(r.shares, 0)}</td>
              <td class="num">${r.grid_value != null ? "¥" + fmtNum(r.grid_value, 0) : "—"}</td>
              <td class="num">${gc2.base_price != null ? fmtNum3(gc2.base_price) : "—"}</td>
              <td class="num">${pr.min != null ? `${fmtNum3(pr.min)}~${fmtNum3(pr.max)}` : "—"}</td>
              <td class="num">${posr.min != null ? `${fmtNum(posr.min, 0)}~${fmtNum(posr.max, 0)}` : "—"}</td>
              <td class="num ${pctCls(r.annual_return_pct)}">${fmtPct(r.annual_return_pct, 2, true)}</td>
              <td class="num ${pctCls(r.buy_and_hold_annual_pct)}">${fmtPct(r.buy_and_hold_annual_pct, 2, true)}</td>
              <td class="num ${pctCls(r.alpha_pct)}">${fmtPct(r.alpha_pct, 1, true)}${r.beat_benchmark === false ? ' <span class="badge-warn">跑输基准</span>' : ""}</td>
              <td class="num ${pctCls(r.total_return_pct)}">${fmtPct(r.total_return_pct, 2, true)}</td>
              <td class="num down">${fmtPct(r.max_dd_pct, 2)}</td>
              <td class="num">${fmtNum(r.sharpe)}</td>
              <td class="num">${fmtPct(r.win_rate_pct, 1)}</td>
              <td class="num">${esc(r.grade || "—")}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无优化结果</div>';

  const sorted = [...results].sort(
    (a, b) => (b.annual_return_pct || 0) - (a.annual_return_pct || 0)
  ).slice(0, 100);
  $("#grid-opt-results").innerHTML = sorted.length
    ? `
    <div class="muted" style="margin-bottom:8px">共 ${results.length} 组，仅显示按年化前 100 组</div>
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th data-metric="spacing">间距% ⓘ</th><th data-metric="levels">层数 ⓘ</th><th data-metric="shares">每格 ⓘ</th>
        <th data-metric="base_price">基准价 ⓘ</th><th data-metric="price_range">价格区间 ⓘ</th><th data-metric="annual">年化 ⓘ</th><th data-metric="excess">超额α ⓘ</th><th data-metric="total">总收益 ⓘ</th><th data-metric="max_dd">MaxDD ⓘ</th><th data-metric="sharpe">Sharpe ⓘ</th>
        <th data-metric="win_rate">胜率 ⓘ</th><th data-metric="buy_sell">买/卖 ⓘ</th><th data-metric="trades">交易 ⓘ</th><th data-metric="grade">等级 ⓘ</th>
      </tr></thead>
      <tbody>
        ${sorted
          .map((r) => {
            const gc3 = r.grid_config || {};
            const pr3 = gc3.price_range || {};
            return `
            <tr title="${esc(gc3.trigger_desc || "")}">
              <td>${esc(r.code)}</td>
              <td>${esc(r.name)}</td>
              <td class="num">${r.spacing}%</td>
              <td class="num">${r.levels}</td>
              <td class="num">${fmtNum(r.shares, 0)}</td>
              <td class="num">${gc3.base_price != null ? fmtNum3(gc3.base_price) : "—"}</td>
              <td class="num">${pr3.min != null ? `${fmtNum3(pr3.min)}~${fmtNum3(pr3.max)}` : "—"}</td>
              <td class="num ${pctCls(r.annual_return_pct)}">${fmtPct(r.annual_return_pct, 2, true)}</td>
              <td class="num ${pctCls(r.alpha_pct)}">${fmtPct(r.alpha_pct, 1, true)}</td>
              <td class="num ${pctCls(r.total_return_pct)}">${fmtPct(r.total_return_pct, 2, true)}</td>
              <td class="num down">${fmtPct(r.max_dd_pct, 2)}</td>
              <td class="num">${fmtNum(r.sharpe)}</td>
              <td class="num">${fmtPct(r.win_rate_pct, 1)}</td>
              <td class="num">${r.triggered_buy ?? 0}/${r.triggered_sell ?? 0}</td>
              <td class="num">${r.trades ?? 0}</td>
              <td class="num">${esc(r.grade || "—")}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`
    : '<div class="empty">暂无数据</div>';
}

export {
  METRIC_HELP,
  addGridTrigger,
  bindMetricHints,
  collectGridConfigRow,
  collectGridTriggerRow,
  confirmGridConfigs,
  confirmGridTriggers,
  gridConfigAdvise,
  gridConfigNum,
  gridConfigOptimizeRow,
  gridConfigRowHtml,
  gridScoreDetail,
  gridScreenOptimize,
  gridTriggerRowHtml,
  gridTriggerRowValid,
  gridTriggerVerifyState,
  loadGridTriggers,
  parseGridConfigs,
  parseGridTriggers,
  renderGrid,
  renderGridConfigList,
  renderGridConfigResult,
  renderGridOptimize,
  renderGridParseResult,
  renderGridScreener,
  runGridConfigOptimize,
  updateGridConfigHint,
  updateGridTriggerHint,
};

