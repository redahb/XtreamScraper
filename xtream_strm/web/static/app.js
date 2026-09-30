"use strict";

// ---------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  const res = await fetch("/api" + path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok && res.status !== 502) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), isError ? 8000 : 3500);
}

function fmtTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

function fmtDuration(sec) {
  if (sec == null) return "–";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}

const badge = (status) => `<span class="badge s-${esc(status || "none")}">${esc(status || "never")}</span>`;

// ---------------------------------------------------------------------------------------
// State & tabs
// ---------------------------------------------------------------------------------------
const state = { providers: [], tab: "dashboard", categories: null, categoryProvider: null, pollTimer: null };

function showTab(name) {
  state.tab = name;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + name));
  if (name === "sync-history") loadSyncHistory();
  if (name === "probe-history") loadProbeHistory();
  if (name === "settings") loadSettings();
  if (name === "logs") loadLogs();
  if (name === "providers") renderProviders();
}

// ---------------------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------------------
async function refreshStatus() {
  let data;
  try {
    data = await api("GET", "/status");
  } catch (e) {
    schedulePoll(10000);
    return;
  }
  state.providers = data.providers;
  $("#version").textContent = "v" + data.version;
  const banner = $("#ffprobe-banner");
  if (data.ffprobe.found) {
    banner.classList.add("hidden");
  } else {
    banner.classList.remove("hidden");
    banner.textContent = `ffprobe not available – probing is disabled. ${data.ffprobe.how}. Install FFmpeg or set the ffprobe path under Settings.`;
  }
  $("#paths").textContent = `Database: ${data.db_path} · Log: ${data.log_path || "–"}`;
  renderJobs(data.jobs);
  renderDashboardProviders(data.providers);
  fillProviderFilters();
  if (state.tab === "providers") renderProviders();
  const active = data.jobs.some((j) => j.active);
  schedulePoll(active ? 2000 : 10000);
}

function schedulePoll(ms) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(refreshStatus, ms);
}

function renderJobs(jobs) {
  const el = $("#jobs");
  if (!jobs.length) {
    el.innerHTML = '<p class="muted">No jobs have run since the application started.</p>';
    return;
  }
  el.innerHTML = jobs.map((j) => {
    const pct = j.total ? Math.min(100, Math.round((100 * j.processed) / j.total)) : (j.active ? 0 : 100);
    const where = [j.provider, j.phase, j.category].filter(Boolean).map(esc).join(" › ");
    const summary = Object.entries(j.summary || {}).filter(([k]) => k !== "errors").map(([k, v]) => `${esc(k)}: ${esc(v)}`).join(" · ");
    return `<div class="job">
      <div class="head">
        <div><strong>${esc(j.description)}</strong> ${badge(j.status)}
          ${j.cancel_requested && j.active ? '<span class="muted small">cancelling…</span>' : ""}</div>
        <div class="muted small">started ${fmtTime(j.started_at)} · ${fmtDuration(j.elapsed_seconds)}
          ${j.active ? `<button data-action="cancel-job" data-id="${esc(j.id)}">Cancel</button>` : ""}</div>
      </div>
      ${j.active ? `<div class="progress"><div data-pct="${pct}"></div></div>` : ""}
      <div class="small">${where || ""} ${j.current_item && j.active ? "· " + esc(j.current_item) : ""}</div>
      <div class="small muted">Processed ${j.processed}${j.total ? " / " + j.total : ""} · errors ${j.errors}
        ${summary ? " · " + summary : ""}</div>
      ${j.recent_errors.length ? `<ul class="errors">${j.recent_errors.slice(-5).map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
    </div>`;
  }).join("");
  // CSP forbids inline style attributes; set widths through the CSSOM instead.
  $$("[data-pct]", el).forEach((bar) => { bar.style.width = bar.dataset.pct + "%"; });
}

function syncCell(s) {
  if (!s) return '<span class="muted">never</span>';
  return `${badge(s.status)} <span class="small">${fmtTime(s.started_at)}</span><br>
    <span class="small muted">+${s.created ?? 0} new · ${s.updated ?? 0} upd · ${s.skipped ?? 0} unchanged · ${s.missing ?? 0} missing · ${s.error_count} err</span>`;
}

function probeCell(p) {
  if (!p) return '<span class="muted">never</span>';
  return `${badge(p.status)} <span class="small">${fmtTime(p.started_at)}</span><br>
    <span class="small muted">${p.probed ?? 0} probed · ${p.succeeded ?? 0} ok · ${p.failed ?? 0} failed · ${p.skipped ?? 0} skipped</span>`;
}

function connCell(p) {
  if (p.last_test_ok == null) return '<span class="muted">not tested</span>';
  return `${badge(p.last_test_ok ? "ok" : "failed")}<br><span class="small muted">${esc(p.last_test_message || "")}</span>`;
}

function renderDashboardProviders(providers) {
  const body = $("#dashboard-providers");
  if (!providers.length) {
    body.innerHTML = '<tr><td colspan="7" class="muted">No providers yet – add one on the Providers tab.</td></tr>';
    return;
  }
  body.innerHTML = providers.map((p) => {
    const c = p.counts;
    return `<tr>
      <td><strong>${esc(p.name)}</strong><br><span class="small muted">${esc(p.effective_target_folder || "no target folder")}</span></td>
      <td>${p.enabled ? badge("ok").replace(">ok<", ">enabled<") : '<span class="badge">disabled</span>'}</td>
      <td>${connCell(p)}</td>
      <td>${p.activity ? badge("running").replace(">running<", `>${esc(p.activity)}<`) : '<span class="muted">idle</span>'}</td>
      <td>${syncCell(p.last_sync)}</td>
      <td>${probeCell(p.last_probe)}</td>
      <td class="small">${c.movies_active} movies · ${c.series_active} series · ${c.episodes_active} episodes
        ${c.movies_missing + c.episodes_missing ? `<br><span class="muted">${c.movies_missing} movies / ${c.episodes_missing} episodes missing at provider</span>` : ""}</td>
    </tr>`;
  }).join("");
}

// ---------------------------------------------------------------------------------------
// Providers
// ---------------------------------------------------------------------------------------
function renderProviders() {
  const el = $("#provider-list");
  if (!state.providers.length) {
    el.innerHTML = '<p class="muted">No providers configured.</p>';
    return;
  }
  el.innerHTML = state.providers.map((p) => `
    <div class="card provider-card">
      <div class="head">
        <div><strong>${esc(p.name)}</strong> ${p.enabled ? "" : '<span class="badge">disabled</span>'}
          ${p.activity ? badge("running").replace(">running<", `>${esc(p.activity)}<`) : ""}</div>
        <label class="small"><span><input type="checkbox" data-action="toggle-enabled" data-id="${p.id}" ${p.enabled ? "checked" : ""}> Enabled</span></label>
      </div>
      <dl class="kv">
        <dt>Base URL</dt><dd>${esc(p.base_url)}</dd>
        <dt>Username</dt><dd>${esc(p.username)}</dd>
        <dt>Target folder</dt><dd>${esc(p.effective_target_folder || "– not set –")}${p.target_folder ? "" : ' <span class="muted">(default)</span>'}</dd>
        <dt>Content</dt><dd>Movies ${p.movies_enabled ? "on" : "off"} (${p.selected_movie_categories} categories) · Series ${p.series_enabled ? "on" : "off"} (${p.selected_series_categories} categories)</dd>
        <dt>Connection</dt><dd>${connCell(p)}</dd>
      </dl>
      <div class="actions">
        <button data-action="edit-provider" data-id="${p.id}">Edit</button>
        <button data-action="test-provider" data-id="${p.id}">Test Connection</button>
        <button data-action="open-categories" data-id="${p.id}">Categories</button>
        <button class="primary" data-action="sync" data-id="${p.id}" data-scope="all">Sync</button>
        <button data-action="sync" data-id="${p.id}" data-scope="movies">Sync Movies</button>
        <button data-action="sync" data-id="${p.id}" data-scope="series">Sync Series</button>
        <button data-action="sync" data-id="${p.id}" data-scope="all" data-full="1" title="Re-fetch every series, ignoring the unchanged shortcut">Full sync</button>
        <button data-action="probe" data-id="${p.id}" data-scope="all">Probe</button>
        <button data-action="probe" data-id="${p.id}" data-scope="movies">Probe Movies</button>
        <button data-action="probe" data-id="${p.id}" data-scope="series">Probe Series</button>
        <button data-action="probe" data-id="${p.id}" data-scope="all" data-force="1">Force re-probe</button>
        <button class="danger" data-action="delete-provider" data-id="${p.id}">Delete</button>
      </div>
    </div>`).join("");
}

function openProviderForm(p) {
  const form = $("#provider-edit");
  form.reset();
  $("#provider-form").classList.remove("hidden");
  $("#provider-test-result").textContent = "";
  $("#provider-form-title").textContent = p ? `Edit provider – ${p.name}` : "Add provider";
  form.id.value = p ? p.id : "";
  for (const key of ["name", "base_url", "username", "user_agent", "target_folder"]) form[key].value = p ? p[key] || "" : "";
  for (const key of ["enabled", "movies_enabled", "series_enabled"]) form[key].checked = p ? !!p[key] : true;
  form.password.value = "";
  form.password.placeholder = p ? "unchanged" : "";
  form.password.required = !p;
  $("#password-hint").textContent = p ? "Leave the password empty to keep the stored one. Passwords are never shown again after saving." : "";
  form.name.focus();
}

function providerFormData() {
  const form = $("#provider-edit");
  const data = {};
  for (const key of ["id", "name", "base_url", "username", "password", "user_agent", "target_folder"]) data[key] = form[key].value.trim();
  for (const key of ["enabled", "movies_enabled", "series_enabled"]) data[key] = form[key].checked;
  return data;
}

async function saveProvider(ev) {
  ev.preventDefault();
  const data = providerFormData();
  const id = data.id;
  delete data.id;
  try {
    const res = id ? await api("PUT", `/providers/${id}`, data) : await api("POST", "/providers", data);
    toast(`Saved ${res.provider.name}`);
    $("#provider-form").classList.add("hidden");
    await refreshStatus();
    if (!id) openCategories(res.provider.id);
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------------------
// Categories
// ---------------------------------------------------------------------------------------
async function openCategories(id) {
  const p = state.providers.find((x) => x.id === Number(id));
  state.categoryProvider = Number(id);
  $("#category-panel").classList.remove("hidden");
  $("#category-provider").textContent = p ? p.name : id;
  $("#category-status").textContent = "";
  const data = await api("GET", `/providers/${id}/categories`);
  state.categories = data;
  renderCategories();
  if (!data.movie.length && !data.series.length) await refreshCategories(null);
  $("#category-panel").scrollIntoView({ behavior: "smooth" });
}

function renderCategories() {
  const data = state.categories;
  $("#category-refreshed").textContent = data.refreshed_at ? `Categories last refreshed ${fmtTime(data.refreshed_at)}` : "Categories not retrieved yet.";
  for (const type of ["movie", "series"]) {
    const filter = ($(`[data-filter="${type}"]`).value || "").toLowerCase();
    const list = data[type];
    $(`#cats-${type}`).innerHTML = list.length ? list.map((c) => `
      <label class="${c.present ? "" : "gone"} ${filter && !c.name.toLowerCase().includes(filter) ? "hidden" : ""}"
        title="${c.present ? "" : "No longer returned by the provider"}">
        <input type="checkbox" data-cat="${type}" value="${esc(c.category_id)}" ${c.selected ? "checked" : ""}>
        ${esc(c.name)} <span class="muted small">#${esc(c.category_id)}</span></label>`).join("")
      : '<p class="muted small">No categories. Use Refresh Categories.</p>';
    updateCategoryCount(type);
  }
}

function updateCategoryCount(type) {
  const boxes = $$(`input[data-cat="${type}"]`);
  $(`[data-count="${type}"]`).textContent = `(${boxes.filter((b) => b.checked).length} / ${boxes.length} selected)`;
}

function captureSelection() {
  if (!state.categories) return;
  for (const type of ["movie", "series"]) {
    const checked = new Set($$(`input[data-cat="${type}"]`).filter((b) => b.checked).map((b) => b.value));
    state.categories[type].forEach((c) => { c.selected = checked.has(c.category_id); });
  }
}

async function refreshCategories(type) {
  captureSelection();
  const pending = state.categories;
  $("#category-status").textContent = "Retrieving categories from provider…";
  try {
    const data = await api("POST", `/providers/${state.categoryProvider}/categories/refresh`, type ? { content_type: type } : {});
    // keep unsaved ticks the user already made
    for (const t of ["movie", "series"]) {
      const wanted = new Set((pending ? pending[t] : []).filter((c) => c.selected).map((c) => c.category_id));
      if (pending) data[t].forEach((c) => { c.selected = wanted.has(c.category_id) || (c.selected && !pending[t].length); });
    }
    state.categories = data;
    renderCategories();
    $("#category-status").textContent = data.errors && data.errors.length ? "Errors: " + data.errors.join("; ") : "Categories refreshed.";
  } catch (e) {
    $("#category-status").textContent = "";
    toast(e.message, true);
  }
}

async function saveCategories() {
  const body = {};
  for (const type of ["movie", "series"]) body[type] = $$(`input[data-cat="${type}"]`).filter((b) => b.checked).map((b) => b.value);
  try {
    state.categories = await api("PUT", `/providers/${state.categoryProvider}/categories`, body);
    renderCategories();
    $("#category-status").textContent = `Saved: ${body.movie.length} movie and ${body.series.length} series categories.`;
    refreshStatus();
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------------------
function fillProviderFilters() {
  $$(".provider-filter").forEach((sel) => {
    const current = sel.value;
    sel.innerHTML = '<option value="">All providers</option>' +
      state.providers.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
    sel.value = current;
  });
}

async function loadSyncHistory() {
  const pid = $("#sync-history-provider").value;
  const data = await api("GET", "/history/sync" + (pid ? `?provider_id=${pid}` : ""));
  $("#sync-history").innerHTML = data.items.length ? data.items.map((h) => `<tr>
    <td>${esc(h.provider_name)}</td><td>${fmtTime(h.started_at)}</td><td>${esc(h.sync_type)}</td>
    <td>${fmtDuration(h.duration_seconds)}</td><td>${badge(h.status)}</td>
    <td>${h.created}</td><td>${h.updated}</td><td>${h.skipped}</td><td>${h.missing}</td>
    <td>${h.error_count ? `<span class="s-err">${h.error_count}</span>` : 0}</td>
    <td><button data-action="sync-detail" data-id="${h.id}">Details</button></td></tr>`).join("")
    : '<tr><td colspan="11" class="muted">No synchronization history.</td></tr>';
}

async function loadProbeHistory() {
  const pid = $("#probe-history-provider").value;
  const data = await api("GET", "/history/probe" + (pid ? `?provider_id=${pid}` : ""));
  $("#probe-history").innerHTML = data.items.length ? data.items.map((h) => `<tr>
    <td>${esc(h.provider_name)}</td><td>${fmtTime(h.started_at)}</td><td>${esc(h.scope)}${h.forced ? " (forced)" : ""}</td>
    <td>${fmtDuration(h.duration_seconds)}</td><td>${badge(h.status)}</td>
    <td>${h.considered}</td><td>${h.probed}</td><td>${h.skipped}</td><td>${h.succeeded}</td>
    <td>${h.failed ? `<span class="s-err">${h.failed}</span>` : 0}</td>
    <td><button data-action="probe-detail" data-id="${h.id}">Details</button></td></tr>`).join("")
    : '<tr><td colspan="11" class="muted">No probe history.</td></tr>';
}

function detailHtml(item, title) {
  const stats = Object.entries(item.stats || {}).filter(([, v]) => typeof v === "number")
    .map(([k, v]) => `<div><span class="muted">${esc(k.replace(/_/g, " "))}:</span> ${v}</div>`).join("");
  const cats = (item.stats && item.stats.categories) || [];
  const list = (arr) => arr.length ? `<ul class="msglist">${arr.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : '<p class="muted small">None.</p>';
  return `<h2>${esc(title)} – ${esc(item.provider_name)} ${badge(item.status)}</h2>
    <p class="small muted">${fmtTime(item.started_at)} → ${fmtTime(item.finished_at)} (${fmtDuration(item.duration_seconds)})</p>
    <div class="stats">${stats}</div>
    ${cats.length ? `<h3>Categories processed</h3><p class="small">${cats.map(esc).join(", ")}</p>` : ""}
    <h3>Errors (${item.error_count})</h3>${list(item.errors || [])}
    <h3>Warnings (${item.warning_count})</h3>${list(item.warnings || [])}
    ${item.details_pruned ? '<p class="muted small">Messages for older runs are pruned; counters are kept.</p>' : ""}`;
}

// ---------------------------------------------------------------------------------------
// Settings & logs
// ---------------------------------------------------------------------------------------
async function loadSettings() {
  const data = await api("GET", "/settings");
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(data.settings)) {
    const input = form.elements[key];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = !!value; else input.value = value;
  }
  $("#restart-hint").textContent = `Currently listening on ${data.active_host || "?"}:${data.active_port || "?"}. Changing the address or port requires restarting the application.`;
}

async function saveSettings(ev) {
  ev.preventDefault();
  const form = $("#settings-form");
  const body = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    body[input.name] = input.type === "checkbox" ? input.checked : input.value;
  }
  try {
    const res = await api("PUT", "/settings", body);
    $("#settings-result").textContent = res.restart_required ? "Saved. Restart the application to apply the new address/port." : "Saved.";
    refreshStatus();
  } catch (e) {
    toast(e.message, true);
  }
}

async function loadLogs() {
  const data = await api("GET", "/logs?limit=400");
  $("#log-path").textContent = data.log_path ? `Log file: ${data.log_path}` : "";
  const pre = $("#log-lines");
  pre.textContent = data.lines.join("\n") || "No log lines yet.";
  pre.scrollTop = pre.scrollHeight;
}

// ---------------------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------------------
async function startJob(kind, btn) {
  const body = { scope: btn.dataset.scope || "all" };
  if (btn.dataset.id) body.provider_id = Number(btn.dataset.id);
  if (btn.dataset.force) body.force = true;
  if (btn.dataset.full) body.full_refresh = true;
  try {
    const res = await api("POST", "/" + kind, body);
    toast(`Started: ${res.job.description}`);
    showTab("dashboard");
    refreshStatus();
  } catch (e) {
    toast(e.message, true);
  }
}

const actions = {
  "sync": (btn) => startJob("sync", btn),
  "probe": (btn) => startJob("probe", btn),
  "cancel-job": async (btn) => { await api("POST", `/jobs/${btn.dataset.id}/cancel`); refreshStatus(); },
  "add-provider": () => openProviderForm(null),
  "edit-provider": (btn) => openProviderForm(state.providers.find((p) => p.id === Number(btn.dataset.id))),
  "cancel-provider": () => $("#provider-form").classList.add("hidden"),
  "delete-provider": async (btn) => {
    const p = state.providers.find((x) => x.id === Number(btn.dataset.id));
    if (!confirm(`Delete provider "${p.name}"?\n\nIts settings, categories and sync state are removed. Files already written to the library are NOT deleted.`)) return;
    try { await api("DELETE", `/providers/${p.id}`); toast("Provider deleted"); refreshStatus(); } catch (e) { toast(e.message, true); }
  },
  "test-provider": async (btn) => {
    btn.disabled = true;
    try {
      const res = await api("POST", `/providers/${btn.dataset.id}/test`);
      toast(res.message, !res.ok);
      refreshStatus();
    } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  },
  "test-unsaved": async (btn) => {
    btn.disabled = true;
    $("#provider-test-result").textContent = "Testing…";
    try {
      const res = await api("POST", "/providers/test", providerFormData());
      $("#provider-test-result").innerHTML = `${badge(res.ok ? "ok" : "failed")} ${esc(res.message)}`;
    } catch (e) { $("#provider-test-result").textContent = e.message; } finally { btn.disabled = false; }
  },
  "open-categories": (btn) => openCategories(btn.dataset.id).catch((e) => toast(e.message, true)),
  "cat-refresh": (btn) => refreshCategories(btn.dataset.type),
  "cat-all": (btn) => { $$(`input[data-cat="${btn.dataset.type}"]`).forEach((b) => { if (!b.closest("label").classList.contains("hidden")) b.checked = true; }); updateCategoryCount(btn.dataset.type); },
  "cat-none": (btn) => { $$(`input[data-cat="${btn.dataset.type}"]`).forEach((b) => { if (!b.closest("label").classList.contains("hidden")) b.checked = false; }); updateCategoryCount(btn.dataset.type); },
  "cat-save": () => saveCategories(),
  "cat-close": () => { $("#category-panel").classList.add("hidden"); state.categories = null; },
  "reload-sync-history": () => loadSyncHistory(),
  "reload-probe-history": () => loadProbeHistory(),
  "sync-detail": async (btn) => {
    const data = await api("GET", `/history/sync/${btn.dataset.id}`);
    const el = $("#sync-detail"); el.innerHTML = detailHtml(data.item, "Synchronization"); el.classList.remove("hidden"); el.scrollIntoView({ behavior: "smooth" });
  },
  "probe-detail": async (btn) => {
    const data = await api("GET", `/history/probe/${btn.dataset.id}`);
    const el = $("#probe-detail"); el.innerHTML = detailHtml(data.item, "Probe"); el.classList.remove("hidden"); el.scrollIntoView({ behavior: "smooth" });
  },
  "test-ffprobe": async () => {
    $("#ffprobe-result").textContent = "Testing…";
    const res = await api("POST", "/settings/test-ffprobe", { ffprobe_path: $("#settings-form").elements.ffprobe_path.value });
    $("#ffprobe-result").innerHTML = `${badge(res.ok ? "ok" : "failed")} ${esc(res.message)}`;
  },
  "reload-logs": () => loadLogs(),
};

document.addEventListener("click", (ev) => {
  const tab = ev.target.closest("#tabs button");
  if (tab) return showTab(tab.dataset.tab);
  const btn = ev.target.closest("button[data-action]");
  if (btn && actions[btn.dataset.action]) {
    ev.preventDefault();
    Promise.resolve(actions[btn.dataset.action](btn)).catch((e) => toast(e.message, true));
  }
});

document.addEventListener("change", async (ev) => {
  const t = ev.target;
  if (t.dataset.action === "toggle-enabled") {
    try { await api("POST", `/providers/${t.dataset.id}/enabled`, { enabled: t.checked }); refreshStatus(); } catch (e) { toast(e.message, true); }
  } else if (t.dataset.cat) {
    updateCategoryCount(t.dataset.cat);
  } else if (t.classList.contains("provider-filter")) {
    t.id.startsWith("sync") ? loadSyncHistory() : loadProbeHistory();
  }
});

document.addEventListener("input", (ev) => {
  if (ev.target.dataset.filter) { captureSelection(); renderCategories(); }
});

$("#provider-edit").addEventListener("submit", saveProvider);
$("#settings-form").addEventListener("submit", saveSettings);
refreshStatus();
