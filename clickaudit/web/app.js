// 点击审计批次台前端（原生 ES module，无构建）
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  batches: [],
  current: null,
  tab: "cells",
};

const KIND_COLUMNS = {
  cells: [
    { key: "group", placeholder: "candidate|control", width: 110 },
    { key: "channel", placeholder: "渠道", width: 110 },
    { key: "date", placeholder: "日期", width: 110 },
    { key: "reach", placeholder: "触达", width: 90, numeric: true },
    { key: "log_clicks", placeholder: "日志点击", width: 100, numeric: true },
  ],
  channel_audit: [
    { key: "channel", placeholder: "渠道", width: 160 },
    { key: "valid_clicks", placeholder: "有效点击", width: 140, numeric: true },
  ],
  date_audit: [
    { key: "date", placeholder: "日期", width: 160 },
    { key: "valid_clicks", placeholder: "有效点击", width: 140, numeric: true },
  ],
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

function setMsg(el, text, ok = false) {
  el.textContent = text;
  el.className = `msg ${ok ? "ok" : "err"}`;
}

// ---------- 批次列表 ----------
async function loadBatches(selectId = null) {
  state.batches = (await api("/api/batches")).batches;
  const tbody = $("#batch-rows");
  tbody.innerHTML = "";
  for (const b of state.batches) {
    const tr = document.createElement("tr");
    const received = ["cells", "channel_audit", "date_audit"]
      .map((k) => (b.received[k] ? "✓" : "·"))
      .join(" ");
    tr.innerHTML = `
      <td>${b.batch_id}</td>
      <td><span class="pill ${b.status}">${b.status}</span></td>
      <td class="kpi">${received}</td>
      <td>${b.note || ""}</td>
      <td>${b.parent_batch_id || "—"}</td>
      <td class="kpi">${b.created_at}</td>
      <td><button class="small ghost">打开</button></td>`;
    tr.querySelector("button").onclick = () => openBatch(b.batch_id);
    tbody.appendChild(tr);
  }
  if (selectId && !state.batches.some((b) => b.batch_id === selectId)) {
    await loadBatches();
  }
}

async function openBatch(batchId) {
  const detail = await api(`/api/batches/${batchId}`);
  state.current = detail;
  $("#work-card").hidden = false;
  $("#result-card").hidden = false;
  $("#cur-batch").textContent = batchId;
  $("#cur-status").textContent = detail.meta.status;
  $("#cur-status").className = `pill ${detail.meta.status}`;
  // 已提交的数据回填编辑器
  for (const kind of Object.keys(KIND_COLUMNS)) {
    const doc = detail.submissions[kind];
    const rows = doc ? extractRows(kind, doc) : [blankRow(kind)];
    renderEditor(kind, rows);
  }
  renderResult(detail);
}

function extractRows(kind, doc) {
  if (kind === "cells") return doc.rows;
  return doc.entries;
}

function blankRow(kind) {
  const row = {};
  for (const col of KIND_COLUMNS[kind]) row[col.key] = "";
  return row;
}

// ---------- 表格编辑器 ----------
function renderEditor(kind, rows) {
  const cols = KIND_COLUMNS[kind];
  const host = $(`#${kind}-editor`);
  const table = document.createElement("table");
  table.className = "grid";
  table.innerHTML =
    "<thead><tr>" +
    cols.map((c) => `<th style="min-width:${c.width}px">${c.key}</th>`).join("") +
    "<th></th></tr></thead>";
  const tbody = document.createElement("tbody");
  table.appendChild(tbody);
  host.innerHTML = "";
  host.appendChild(table);

  const addRow = (data) => {
    const tr = document.createElement("tr");
    for (const col of cols) {
      const td = document.createElement("td");
      const input = document.createElement("input");
      input.type = "text";
      input.dataset.key = col.key;
      input.value = data[col.key] ?? "";
      td.appendChild(input);
      tr.appendChild(td);
    }
    const tdBtn = document.createElement("td");
    const del = document.createElement("button");
    del.className = "small ghost";
    del.textContent = "删";
    del.onclick = () => tr.remove();
    tdBtn.appendChild(del);
    tr.appendChild(tdBtn);
    tbody.appendChild(tr);
  };
  rows.forEach(addRow);
  table.dataset.addRow = "1";
  host._addRow = () => addRow(blankRow(kind));
}

function collectEditor(kind) {
  const cols = KIND_COLUMNS[kind];
  const rows = [];
  for (const tr of $$(`#${kind}-editor tbody tr`)) {
    const row = {};
    let empty = true;
    for (const col of cols) {
      const raw = tr.querySelector(`input[data-key="${col.key}"]`).value.trim();
      if (raw !== "") empty = false;
      if (col.numeric) {
        if (!/^\d+$/.test(raw)) {
          throw new Error(`${kind}.${col.key} 必须是非负整数，收到 “${raw}”`);
        }
        row[col.key] = Number(raw);
      } else {
        row[col.key] = raw;
      }
    }
    if (!empty) {
      for (const col of cols) {
        if (row[col.key] === "" || row[col.key] === undefined) {
          throw new Error(`${kind} 存在未填完的行：${col.key}`);
        }
      }
      rows.push(row);
    }
  }
  if (!rows.length) throw new Error(`${kind} 至少需要一行`);
  return kind === "cells" ? { rows } : { entries: rows };
}

// ---------- 结果渲染 ----------
function renderResult(detail) {
  const body = $("#result-body");
  body.innerHTML = "";
  const a = detail.analysis;
  if (!a) {
    body.innerHTML = `<p class="hint">三份数据未齐全，尚未冻结分析。</p>`;
    $("#run-log").textContent = detail.run_log || "";
    return;
  }
  if (!a.feasible) {
    const c = a.conflict;
    const d = c.details || {};
    body.innerHTML = `
      <div class="conflict">
        <h3>无解：两套计数与格子容量存在冲突</h3>
        <p>${c.message}</p>
        <p class="kpi">冲突类型：<b>${c.kind}</b>
        ${d.required != null ? `；最大流 ${d.max_flow}/${d.required}` : ""}</p>
        ${d.cut_channels ? `<p class="kpi">最小割证书：渠道侧 [${d.cut_channels.join(", ")}]
           → 日期割外 [${(d.cut_dates_outside || []).join(", ")}]
           的跨割容量仅 <b>${d.cut_capacity_cross}</b>，
           承载不了渠道审计 <b>${d.audit_inside_channels}</b></p>` : ""}
      </div>`;
  } else {
    body.appendChild(renderBounds(a));
  }
  $("#run-log").textContent = detail.run_log || "";
}

function renderBounds(a) {
  const wrap = document.createElement("div");
  const { channels, dates, groups } = a.schema;
  const totals = a.totals;
  wrap.innerHTML = `
    <p class="kpi">审计有效点击总数 <b>${totals.audit_total}</b>；
      格子容量合计 <b>${totals.capacity_total}</b>；
      组触达合计 candidate=<b>${totals.group_reach.candidate}</b>
      control=<b>${totals.group_reach.control}</b></p>
    <div class="bounds">
      ${boundCard("下界（最小点击率差）", a.lower_bound, channels, dates, groups)}
      ${boundCard("上界（最大点击率差）", a.upper_bound, channels, dates, groups)}
    </div>`;
  return wrap;
}

function boundCard(title, b, channels, dates, groups) {
  const ctrText = Object.entries(b.group_ctr_decimal)
    .map(([g, v]) => `${g}=${(v * 100).toFixed(3)}%(${b.group_ctr[g]})`).join("，");
  const allocTables = groups
    .map((g) => {
      const gi = groups.indexOf(g);
      const mat = b.allocation[g];
      const head = "<tr><th>渠道\\日期</th>" + dates.map((d) => `<th>${d}</th>`).join("") + "<th>组内点击</th></tr>";
      const rows = mat
        .map((row, ci) => `<tr><td>${channels[ci]}</td>` +
          row.map((x) => `<td>${x}</td>`).join("") +
          `<td>${row.reduce((s, x) => s + x, 0)}</td></tr>`)
        .join("");
      return `<h4>组 ${g} 的真实点击分配</h4><div class="alloc"><table class="grid">${head}${rows}</table></div>`;
    })
    .join("");
  return `
    <div class="bound-box">
      <h3>${title}</h3>
      <div class="big">${(b.ctr_diff_decimal * 100).toFixed(4)} 个百分点</div>
      <p class="kpi">精确分数 <b>${b.ctr_diff_fraction}</b> ≈ ${b.ctr_diff_decimal.toFixed(6)}</p>
      <p class="kpi">组点击率：${ctrText}</p>
      ${allocTables}
    </div>`;
}

// ---------- 事件绑定 ----------
$("#btn-create").onclick = async () => {
  const meta = await api("/api/batches", {
    method: "POST",
    body: JSON.stringify({ note: $("#note").value }),
  });
  await loadBatches();
  await openBatch(meta.batch_id);
};

$("#btn-refresh").onclick = () => loadBatches();

$$(".tab").forEach((btn) => {
  btn.onclick = () => {
    state.tab = btn.dataset.tab;
    $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".tabpane").forEach((p) => (p.hidden = p.dataset.pane !== state.tab));
  };
});

$$("button[data-addrow]").forEach((btn) => {
  btn.onclick = () => $(`#${btn.dataset.addrow}-editor`)._addRow();
});

$$("button[data-submit]").forEach((btn) => {
  btn.onclick = async () => {
    if (!state.current) return;
    const kind = btn.dataset.submit;
    const msg = $("#submit-msg");
    try {
      const payload = collectEditor(kind);
      const meta = await api(
        `/api/batches/${state.current.meta.batch_id}/submit/${kind}`,
        { method: "POST", body: JSON.stringify(payload) });
      setMsg(msg, `已提交 ${kind}。批次状态：${meta.status}`, true);
      await loadBatches();
      await openBatch(meta.batch_id);
    } catch (e) {
      setMsg(msg, e.message);
    }
  };
});

$$("input[data-import]").forEach((input) => {
  input.onchange = async () => {
    const kind = input.dataset.import;
    const file = input.files[0];
    if (!file) return;
    try {
      const doc = JSON.parse(await file.text());
      const rows = extractRows(kind, doc);
      renderEditor(kind, rows);
      setMsg($("#submit-msg"), `已载入文件，请核对后点“提交本份”`, true);
    } catch (e) {
      setMsg($("#submit-msg"), `文件解析失败：${e.message}`);
    }
    input.value = "";
  };
});

$("#btn-rerun").onclick = async () => {
  if (!state.current) return;
  try {
    const r = await api(`/api/batches/${state.current.meta.batch_id}/rerun`,
      { method: "POST" });
    setMsg($("#action-msg"),
      `重跑完成：结果一致（analysis_checksum=${r.analysis_checksum.slice(0, 16)}…）`, true);
    await loadBatches();
    await openBatch(state.current.meta.batch_id);
  } catch (e) {
    setMsg($("#action-msg"), e.message);
  }
};

$("#btn-correct").onclick = async () => {
  if (!state.current) return;
  try {
    const meta = await api(
      `/api/batches/${state.current.meta.batch_id}/correction`,
      { method: "POST", body: JSON.stringify({ note: "页面发起修正" }) });
    await loadBatches();
    await openBatch(meta.batch_id);
    setMsg($("#action-msg"),
      `已另开修正批次 ${meta.batch_id}，旧批次冻结结果保留不变`, true);
  } catch (e) {
    setMsg($("#action-msg"), e.message);
  }
};

loadBatches();
