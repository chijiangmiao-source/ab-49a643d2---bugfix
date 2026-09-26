"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* 非 JSON 响应 */ }
  return {
    ok: res.ok,
    status: res.status,
    data,
    replayed: res.headers.get("X-Idempotent-Replay") === "true",
  };
}

function showResult(title, obj, note) {
  const panel = $("#panel-result");
  panel.hidden = false;
  $("#panel-result-title").textContent = title + (note ? `（${note}）` : "");
  $("#panel-result-body").textContent = JSON.stringify(obj, null, 2);
  $("#panel-error").hidden = true;
}

function showError(payload, status) {
  const panel = $("#panel-error");
  panel.hidden = false;
  $("#panel-error-body").textContent =
    `HTTP ${status}\n` + JSON.stringify(payload, null, 2);
  $("#panel-result").hidden = true;
}

function fmtInvalidation(inv) {
  if (!inv) return "—";
  return `${inv.root} · 操作 ${inv.operation_id}`;
}

async function refresh() {
  const r = await api("GET", "/api/records");
  if (!r.ok) { showError(r.data, r.status); return; }
  renderTable(r.data.records);
  renderDepOptions(r.data.records.filter((x) => x.valid));
}

function renderTable(records) {
  const tbody = $("#records-table tbody");
  tbody.innerHTML = "";
  for (const rec of records) {
    const tr = document.createElement("tr");
    tr.className = rec.valid ? "valid" : "invalid";
    const deps = rec.depends_on.length ? rec.depends_on.join(", ") : "—";
    const summary = rec.summary + (rec.reading_mk != null ? `（${rec.reading_mk} mK）` : "");
    tr.innerHTML = `
      <td class="mono"><a href="#" data-lineage="${esc(rec.id)}">${esc(rec.id)}</a></td>
      <td>${rec.kind === "raw" ? "原始" : "推导"}</td>
      <td>${esc(rec.detector)}</td>
      <td>${esc(summary)}</td>
      <td><span class="badge ${rec.valid ? "ok" : "bad"}">${rec.valid ? "有效" : "失效"}</span></td>
      <td class="mono">${esc(deps)}</td>
      <td class="mono">${esc(fmtInvalidation(rec.invalidation))}</td>
      <td class="mono ts">${esc(rec.created_at)}</td>
      <td><button type="button" data-invalidate="${esc(rec.id)}" ${rec.valid ? "" : "disabled"}>失效裁决</button></td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("a[data-lineage]").forEach((a) =>
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      showLineage(a.dataset.lineage);
    }));
  tbody.querySelectorAll("button[data-invalidate]").forEach((b) =>
    b.addEventListener("click", () => invalidate(b.dataset.invalidate)));
}

function renderDepOptions(validRecords) {
  const box = $("#dep-options");
  box.innerHTML = "";
  if (!validRecords.length) {
    box.innerHTML = '<span class="muted">暂无有效记录可作为依据</span>';
    return;
  }
  for (const rec of validRecords) {
    const label = document.createElement("label");
    label.className = "dep-option";
    label.innerHTML =
      `<input type="checkbox" value="${esc(rec.id)}"> ` +
      `<span class="mono">${esc(rec.id)}</span> ${esc(rec.summary)}`;
    box.appendChild(label);
  }
}

async function invalidate(recordId) {
  const opId = window.prompt(
    `对 ${recordId} 发起失效裁决。\n` +
    `请输入操作标识（相同标识重复提交将返回首次结果；改换目标将冲突）：`,
    `op-${Date.now()}`);
  if (!opId) return;
  const r = await api(
    "POST",
    `/api/records/${encodeURIComponent(recordId)}/invalidate`,
    { operation_id: opId });
  if (r.ok) {
    showResult(
      `失效裁决完成：${recordId}`,
      r.data,
      r.replayed ? "重放：返回首次结果" : "新裁决");
  } else {
    showError(r.data, r.status);
  }
  await refresh();
}

async function showLineage(id) {
  const r = await api("GET", `/api/records/${encodeURIComponent(id)}/lineage`);
  if (!r.ok) { showError(r.data, r.status); return; }
  $("#panel-lineage").hidden = false;
  $("#lineage-id").textContent = id;
  const L = r.data;
  $("#lineage-body").innerHTML = `
    <p>直接依据：<span class="mono">${esc(L.direct_dependencies.join(", ") || "—")}</span></p>
    <p>直接被引：<span class="mono">${esc(L.direct_dependents.join(", ") || "—")}</span></p>
    <p>全部上游：<span class="mono">${esc(L.ancestors.join(", ") || "—")}</span></p>
    <p>全部下游：<span class="mono">${esc(L.descendants.join(", ") || "—")}</span></p>`;
}

$("#form-raw").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const body = {
    kind: "raw",
    detector: f.detector.value.trim(),
    summary: f.summary.value.trim(),
    reading_mk: f.reading_mk.value === "" ? null : Number(f.reading_mk.value),
  };
  const r = await api("POST", "/api/records", body);
  if (r.ok) {
    showResult(`已创建 ${r.data.id}`, r.data);
    f.summary.value = "";
  } else {
    showError(r.data, r.status);
  }
  await refresh();
});

$("#form-derived").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const deps = [...document.querySelectorAll("#dep-options input:checked")]
    .map((x) => x.value);
  const body = {
    kind: "derived",
    detector: f.detector.value.trim(),
    summary: f.summary.value.trim(),
    depends_on: deps,
  };
  const r = await api("POST", "/api/records", body);
  if (r.ok) {
    showResult(`已创建 ${r.data.id}`, r.data);
    f.summary.value = "";
  } else {
    showError(r.data, r.status);
  }
  await refresh();
});

$("#btn-refresh").addEventListener("click", refresh);

async function checkHealth() {
  const el = $("#health");
  try {
    const r = await api("GET", "/health");
    el.textContent = r.ok ? "● 服务健康" : "● 健康检查失败";
    el.className = "health " + (r.ok ? "ok" : "bad");
  } catch (_) {
    el.textContent = "● 服务不可达";
    el.className = "health bad";
  }
}

checkHealth();
refresh();
