/* DMX 灯具地址编排台 —— 原生 JavaScript 前端 */
"use strict";

const state = {
  catalog: [],
  fixtures: [],
  lines: [],
  reservations: [],
  version: null,
  latestPublishedPlanId: null,
  preview: null, // 当前展示的预览 payload（feasible 时）
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body instanceof FormData ? undefined : { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : `HTTP ${res.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function toast(message, kind = "") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ---------- Tabs ----------
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $(`#tab-${tab.dataset.tab}`).classList.add("active");
    if (tab.dataset.tab === "published") loadPublished();
  });
});

// ---------- 全局版本 ----------
async function refreshVersion() {
  const v = await api("/api/plans/version");
  state.version = v.config_version;
  state.latestPublishedPlanId = v.latest_published_plan_id;
  $("#version-badge").textContent = `配置版本 v${v.config_version}`;
  $("#published-badge").textContent = v.latest_published_plan_id
    ? `已发布 #${v.latest_published_plan_id}`
    : "未发布";
  $("#published-badge").classList.toggle("badge-muted", !v.latest_published_plan_id);
  refreshStaleBanner();
}

function refreshStaleBanner() {
  const banner = $("#stale-banner");
  if (state.preview && state.preview.config_version !== state.version) {
    banner.textContent =
      `配置已变化（当前 v${state.version}，预览基于 v${state.preview.config_version}），请重新生成预览；旧预览无法发布。`;
    banner.classList.remove("hidden");
    $("#btn-publish").disabled = true;
  } else {
    banner.classList.add("hidden");
  }
}

// ---------- 型录 ----------
async function loadCatalog() {
  state.catalog = await api("/api/catalog");
  const list = $("#catalog-list");
  if (!state.catalog.length) {
    list.innerHTML = '<p class="muted">尚未导入任何型号。</p>';
  } else {
    list.innerHTML = state.catalog
      .map(
        (t) => `
      <div class="type-card">
        <div class="title">${esc(t.manufacturer)} ${esc(t.name)}
          <button class="danger" data-del-type="${t.id}" style="float:right;font-size:12px">删除型号</button>
        </div>
        <div>${t.modes
          .map((m) => `<span class="mode-chip">${esc(m.name)} · ${m.channels}ch</span>`)
          .join("")}</div>
      </div>`
      )
      .join("");
  }
  fillFixtureModeSelect();
}

$("#catalog-list").addEventListener("click", async (e) => {
  const id = e.target.dataset.delType;
  if (id && confirm("确认删除该型号及其全部模式？被灯具实例引用时将被拒绝。")) {
    try {
      await api(`/api/catalog/${id}`, { method: "DELETE" });
      toast("已删除", "success");
      await Promise.all([loadCatalog(), refreshVersion(), loadFixtures()]);
    } catch (err) {
      toast(err.message, "error");
    }
  }
});

const SAMPLE_CSV = `manufacturer,name,mode,channels
ACME,Solar 360,Basic,9
ACME,Solar 360,Extended,14
ACME,Solar 360,Full,22
BrightCo,MegaPar 200,3CH,3
BrightCo,MegaPar 200,8CH,8
BrightCo,MegaPar 200,11CH,11
LitePro,Beam 150,Standard,16
LitePro,Beam 150,Macro,20
StrobeKing,Storm 1k,Basic,2
StrobeKing,Storm 1k,DMX Pro,5`;

$("#btn-load-sample").addEventListener("click", () => {
  $("#catalog-text").value = SAMPLE_CSV;
});

async function importCatalogBody(body, isForm) {
  const opts = isForm ? { method: "POST", body } : { method: "POST", body: JSON.stringify(body) };
  const result = await api("/api/catalog/import", opts);
  $("#catalog-msg").innerHTML = `<span class="muted">新增型号 ${result.created_types}，更新型号 ${result.updated_types}，新增模式 ${result.created_modes}，更新模式通道数 ${result.updated_modes}</span>`;
  toast("型录导入成功", "success");
  await Promise.all([loadCatalog(), refreshVersion()]);
}

$("#catalog-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    await importCatalogBody(form, true);
    e.target.value = "";
  } catch (err) {
    toast(err.message, "error");
  }
});

$("#btn-import-text").addEventListener("click", async () => {
  const text = $("#catalog-text").value.trim();
  if (!text) return toast("请先粘贴 CSV/JSON 内容", "error");
  try {
    let rows;
    if (text.startsWith("[") || text.startsWith("{")) {
      const parsed = JSON.parse(text);
      rows = Array.isArray(parsed) ? parsed : [parsed];
      await importCatalogBody(rows, false);
    } else {
      const form = new FormData();
      form.append("file", new Blob([text], { type: "text/csv" }), "catalog.csv");
      await importCatalogBody(form, true);
    }
  } catch (err) {
    toast(err.message, "error");
  }
});

// ---------- 灯具实例 ----------
function fillFixtureModeSelect() {
  const sel = $("#fixture-mode-select");
  const current = sel.value;
  sel.innerHTML = state.catalog
    .flatMap((t) =>
      t.modes.map(
        (m) =>
          `<option value="${m.id}">${esc(t.manufacturer)} ${esc(t.name)} / ${esc(m.name)} (${m.channels}ch)</option>`
      )
    )
    .join("");
  if (current) sel.value = current;
}

async function loadFixtures() {
  state.fixtures = await api("/api/fixtures");
  $("#fixture-count").textContent = state.fixtures.length;
  $("#fixture-tbody").innerHTML = state.fixtures
    .map(
      (f) => `
    <tr>
      <td>${f.id}</td>
      <td>${esc(f.name)}</td>
      <td>${esc(f.type_name || "-")}</td>
      <td>${esc(f.mode_name || "-")}</td>
      <td>${f.channels}</td>
      <td>${f.previous_line ?? "—"}</td>
      <td>${f.previous_start ?? "—"}</td>
      <td>${f.locked ? "🔒" : ""}</td>
      <td>
        <button data-edit="${f.id}">编辑</button>
        <button class="danger" data-del="${f.id}">删除</button>
      </td>
    </tr>`
    )
    .join("");
}

$("#fixture-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {
    name: fd.get("name").trim(),
    mode_id: Number(fd.get("mode_id")),
    previous_line: fd.get("previous_line") ? Number(fd.get("previous_line")) : null,
    previous_start: fd.get("previous_start") ? Number(fd.get("previous_start")) : null,
    locked: fd.get("locked") === "on",
  };
  try {
    await api("/api/fixtures", { method: "POST", body: JSON.stringify(body) });
    toast("灯具已添加", "success");
    e.target.reset();
    await Promise.all([loadFixtures(), refreshVersion()]);
  } catch (err) {
    toast(err.message, "error");
  }
});

$("#fixture-tbody").addEventListener("click", async (e) => {
  const delId = e.target.dataset.del;
  const editId = e.target.dataset.edit;
  if (delId && confirm("确认删除该灯具实例？")) {
    try {
      await api(`/api/fixtures/${delId}`, { method: "DELETE" });
      toast("已删除", "success");
      await Promise.all([loadFixtures(), refreshVersion()]);
    } catch (err) {
      toast(err.message, "error");
    }
  } else if (editId) {
    const f = state.fixtures.find((x) => x.id === Number(editId));
    const name = prompt("灯具名称", f.name);
    if (name === null) return;
    const line = prompt("既有线路 (1-4，留空表示无)", f.previous_line ?? "");
    if (line === null) return;
    const start = prompt("既有起始地址 (1-512，留空表示无)", f.previous_start ?? "");
    if (start === null) return;
    const locked = confirm("确定 = 锁定到既有线路与地址；取消 = 不锁定");
    try {
      await api(`/api/fixtures/${editId}`, {
        method: "PUT",
        body: JSON.stringify({
          name: name.trim() || f.name,
          previous_line: line.trim() ? Number(line) : null,
          previous_start: start.trim() ? Number(start) : null,
          locked,
        }),
      });
      toast("已更新", "success");
      await Promise.all([loadFixtures(), refreshVersion()]);
    } catch (err) {
      toast(err.message, "error");
    }
  }
});

// ---------- 线路与保留区 ----------
async function loadLines() {
  state.lines = await api("/api/lines");
  $("#line-tbody").innerHTML = state.lines
    .map(
      (l) => `
    <tr>
      <td>${l.id}</td>
      <td><input value="${esc(l.name)}" data-line-name="${l.id}" style="width:180px" /></td>
      <td>
        <label class="switch">
          <input type="checkbox" data-line-active="${l.id}" ${l.is_active ? "checked" : ""} />
          <span class="slider"></span>
        </label>
      </td>
      <td><button data-line-save="${l.id}">保存</button></td>
    </tr>`
    )
    .join("");
  $("#res-line").innerHTML = state.lines
    .map((l) => `<option value="${l.id}">${l.id} - ${esc(l.name)}</option>`)
    .join("");
}

$("#line-tbody").addEventListener("click", async (e) => {
  const id = e.target.dataset.lineSave;
  if (!id) return;
  const name = $(`[data-line-name="${id}"]`).value;
  const isActive = $(`[data-line-active="${id}"]`).checked;
  try {
    await api(`/api/lines/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ name, is_active: isActive }),
    });
    toast("线路已更新", "success");
    await Promise.all([loadLines(), refreshVersion(), loadReservations()]);
  } catch (err) {
    toast(err.message, "error");
    await loadLines();
  }
});

async function loadReservations() {
  state.reservations = await api("/api/reservations");
  const lineName = Object.fromEntries(state.lines.map((l) => [l.id, l.name]));
  $("#reservation-tbody").innerHTML = state.reservations
    .map(
      (r) => `
    <tr>
      <td>${r.id}</td>
      <td>${r.line_id} - ${esc(lineName[r.line_id] || "")}</td>
      <td>${r.start}</td>
      <td>${r.end}</td>
      <td>${r.end - r.start + 1}</td>
      <td>${esc(r.reason)}</td>
      <td><button class="danger" data-del-res="${r.id}">删除</button></td>
    </tr>`
    )
    .join("");
}

$("#reservation-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {
    line_id: Number(fd.get("line_id")),
    start: Number(fd.get("start")),
    end: Number(fd.get("end")),
    reason: fd.get("reason") || "",
  };
  try {
    await api("/api/reservations", { method: "POST", body: JSON.stringify(body) });
    toast("保留区已添加", "success");
    e.target.reset();
    await Promise.all([loadReservations(), refreshVersion()]);
  } catch (err) {
    toast(err.message, "error");
  }
});

$("#reservation-tbody").addEventListener("click", async (e) => {
  const id = e.target.dataset.delRes;
  if (id && confirm("确认删除该保留区？")) {
    try {
      await api(`/api/reservations/${id}`, { method: "DELETE" });
      toast("已删除", "success");
      await Promise.all([loadReservations(), refreshVersion()]);
    } catch (err) {
      toast(err.message, "error");
    }
  }
});

// ---------- 编排预览 ----------
$("#btn-preview").addEventListener("click", generatePreview);

async function generatePreview() {
  try {
    const result = await api("/api/plans/preview", { method: "POST" });
    // 只有逐级证明最优（optimal）的预览才可发布；未证最优结果不得进入发布流程。
    state.preview = result.feasible && result.optimality_status === "optimal" ? result : null;
    renderPreview(result);
    await refreshVersion();
  } catch (err) {
    toast(err.message, "error");
  }
}

function renderPreview(p) {
  const conflictPanel = $("#conflict-panel");
  const unprovenPanel = $("#unproven-panel");
  const migrationPanel = $("#migration-panel");
  const allocPanel = $("#alloc-panel");

  if (p.optimality_status === "unproven") {
    // 可行但未证最优：展示候选，同时明确不可发布。
    unprovenPanel.classList.remove("hidden");
    $("#unproven-msg").textContent = p.message || "未能在限定时间内证明最优。";
    conflictPanel.classList.add("hidden");
    allocPanel.classList.remove("hidden");
    $("#plan-summary").textContent =
      `候选预览（未证最优，不可发布）· 基于配置 v${p.config_version}` +
      ` · 迁移 ${p.migration_count} 台 · 启用 ${p.used_lines} 条线路`;
    renderTracks(p.occupancy);
    renderMigrations(p.migrations);
    renderAllocations(p.allocations);
    $("#btn-publish").disabled = true;
    return;
  }

  unprovenPanel.classList.add("hidden");

  if (!p.feasible) {
    conflictPanel.classList.remove("hidden");
    migrationPanel.classList.add("hidden");
    allocPanel.classList.add("hidden");
    $("#tracks").innerHTML = "";
    $("#btn-publish").disabled = true;
    $("#plan-summary").textContent = "无可行方案";
    $("#conflict-list").innerHTML = p.conflicts
      .map((c) => `<li><strong>[${esc(c.type)}]</strong> ${esc(c.message)}</li>`)
      .join("");
    return;
  }

  conflictPanel.classList.add("hidden");
  allocPanel.classList.remove("hidden");
  $("#plan-summary").textContent =
    `预览 #${p.plan_id} · 基于配置 v${p.config_version} · ✅ 已逐级证明最优` +
    ` · 迁移 ${p.migration_count} 台 · 启用 ${p.used_lines} 条线路`;

  renderTracks(p.occupancy);
  renderMigrations(p.migrations);
  renderAllocations(p.allocations);

  const fresh = p.config_version === state.version;
  $("#btn-publish").disabled = !fresh;
}

function renderTracks(occupancy) {
  const pct = (addr) => `calc(${(addr - 1) / 512 * 100}%)`;
  const widthPct = (a, b) => `calc(${(b - a + 1) / 512 * 100}%)`;
  $("#tracks").innerHTML = occupancy
    .map((line) => {
      const resBlocks = line.reservations
        .map(
          (r) =>
            `<div class="block reserved" style="left:${pct(r.start)};width:${widthPct(r.start, r.end)}"
              title="保留区 #${r.id}：${r.start}-${r.end} ${esc(r.reason)}">保 #${r.id}</div>`
        )
        .join("");
      const fxBlocks = line.fixtures
        .map((f) => {
          const cls = f.locked ? "locked" : f.migrated ? "migrated" : "fixture";
          return `<div class="block ${cls}" style="left:${pct(f.start)};width:${widthPct(f.start, f.end)}"
            title="#${f.fixture_id} ${esc(f.name)}：${f.start}-${f.end}（${f.channels}ch）${f.locked ? " 🔒锁定" : f.migrated ? " 迁移" : ""}">
            ${f.channels >= 14 ? esc(f.name) : f.fixture_id}</div>`;
        })
        .join("");
      return `
      <div class="track ${line.is_active ? "" : "disabled"}">
        <div class="track-head">
          <strong>线路 ${line.line_id} · ${esc(line.name)}</strong>${line.is_active ? "" : "（已停用）"}
          <span class="muted">已用 ${line.used_channels}/512，空闲 ${line.free_channels}</span>
        </div>
        <div class="track-bar">${line.is_active ? resBlocks + fxBlocks : ""}</div>
        <div class="track-scale"><span>1</span><span>128</span><span>256</span><span>384</span><span>512</span></div>
      </div>`;
    })
    .join("");
}

function addrOr(v) {
  return v ?? "—";
}

function renderMigrations(migrations) {
  const panel = $("#migration-panel");
  $("#migration-count").textContent = migrations.length;
  if (!migrations.length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  $("#migration-tbody").innerHTML = migrations
    .map(
      (m) => `
    <tr>
      <td>${m.fixture_id}</td><td>${esc(m.name)}</td>
      <td>${addrOr(m.from_line)}</td><td>${addrOr(m.from_start)}</td>
      <td>➡️</td>
      <td>${m.to_line}</td><td>${m.to_start}</td>
      <td>${m.locked ? "🔒" : ""}</td>
    </tr>`
    )
    .join("");
}

function renderAllocations(allocations) {
  $("#alloc-tbody").innerHTML = allocations
    .map(
      (a) => `
    <tr>
      <td>${a.fixture_id}</td><td>${esc(a.name)}</td>
      <td>${a.line_id}</td><td>${a.start}</td><td>${a.end}</td><td>${a.channels}</td>
      <td>${a.locked ? "🔒 锁定" : a.migrated ? "🟠 迁移" : "✅ 原位"}</td>
    </tr>`
    )
    .join("");
}

$("#btn-publish").addEventListener("click", async () => {
  if (!state.preview) return;
  if (state.preview.optimality_status !== "optimal") {
    toast("该预览未证明最优，不能发布", "error");
    return;
  }
  if (state.preview.config_version !== state.version) {
    toast("配置版本已过期，请重新生成预览", "error");
    return;
  }
  if (!confirm("确认按当前预览原子发布？发布后旧预览立即失效。")) return;
  try {
    const result = await api(`/api/plans/${state.preview.plan_id}/publish`, { method: "POST" });
    toast(`已发布 #${result.plan_id}（配置 v${result.config_version}）`, "success");
    state.preview = null;
    $("#plan-summary").textContent = "发布完成，可在“已发布”页签下载 CSV";
    $("#btn-publish").disabled = true;
    await Promise.all([refreshVersion(), loadFixtures()]);
    loadPublished();
  } catch (err) {
    toast(err.message, "error");
    await refreshVersion();
  }
});

// ---------- 已发布 ----------
async function loadPublished() {
  const data = await api("/api/plans/published/latest");
  const empty = $("#published-empty");
  const content = $("#published-content");
  if (!data.has_published) {
    empty.classList.remove("hidden");
    content.classList.add("hidden");
    return;
  }
  empty.classList.add("hidden");
  content.classList.remove("hidden");
  const when = new Date(data.created_at).toLocaleString("zh-CN");
  $("#published-meta").textContent =
    `发布 #${data.plan_id} · 配置 v${data.config_version} · ${when} · 迁移 ${data.migration_count} 台 · 启用 ${data.used_lines} 条线路`;
  $("#btn-download-csv").href = `/api/plans/published/${data.plan_id}/csv`;
  $("#published-tbody").innerHTML = data.allocations
    .map(
      (a) => `
    <tr>
      <td>${a.fixture_id}</td><td>${esc(a.name)}</td>
      <td>${a.line_id}</td><td>${a.start}</td><td>${a.end}</td><td>${a.channels}</td>
      <td>${a.migrated ? "🟠 是" : "否"}</td><td>${a.locked ? "🔒" : ""}</td>
    </tr>`
    )
    .join("");
}

// ---------- 启动 ----------
(async function init() {
  try {
    await Promise.all([loadCatalog(), loadFixtures(), loadLines(), refreshVersion()]);
    await loadReservations();
  } catch (err) {
    toast("初始化失败：" + err.message, "error");
  }
})();
