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
  if (name === "scrapers") loadScrapers();
  if (name === "unmatched") loadUnmatched(false);
  if (name === "metadata-history") loadMetadataHistory();
  if (name === "artwork") { loadArtwork(); loadArtworkHistory(); }
  if (name === "about") loadAbout();
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
  $("#version").textContent = data.version;
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

function metadataCell(m) {
  if (!m) return '<span class="muted">never</span>';
  return `${badge(m.status)} <span class="small">${fmtTime(m.started_at)}</span><br>
    <span class="small muted">${m.matched ?? 0} matched · ${m.updated ?? 0} updated · ${m.unmatched ?? 0} unmatched (${m.ambiguous ?? 0} ambiguous) · ${m.error_count} err</span>`;
}

function connCell(p) {
  if (p.last_test_ok == null) return '<span class="muted">not tested</span>';
  return `${badge(p.last_test_ok ? "ok" : "failed")}<br><span class="small muted">${esc(p.last_test_message || "")}</span>`;
}

function renderDashboardProviders(providers) {
  const body = $("#dashboard-providers");
  if (!providers.length) {
    body.innerHTML = '<tr><td colspan="8" class="muted">No providers yet – add one on the Providers tab.</td></tr>';
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
      <td>${metadataCell(p.last_metadata)}</td>
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
        <button data-action="scrape" data-id="${p.id}" data-scope="all">Scrape Metadata</button>
        <button data-action="scrape" data-id="${p.id}" data-scope="all" data-force="1">Force metadata refresh</button>
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
// Metadata scrapers
// ---------------------------------------------------------------------------------------
async function loadScrapers() {
  const data = await api("GET", "/scrapers");
  const body = $("#scraper-list");
  const n = data.plugins.length;
  body.innerHTML = n ? data.plugins.map((p, i) => `<tr>
    <td>${p.priority}
      <button data-action="scraper-move" data-id="${esc(p.plugin_id)}" data-dir="up" ${i === 0 ? "disabled" : ""} title="Move up">▲</button>
      <button data-action="scraper-move" data-id="${esc(p.plugin_id)}" data-dir="down" ${i === n - 1 ? "disabled" : ""} title="Move down">▼</button></td>
    <td><strong>${esc(p.name)}</strong> ${p.version ? `<span class="muted small">${esc(p.version)}</span>` : ""}</td>
    <td class="small">${p.media_types.map(esc).join(", ") || "–"}</td>
    <td><label class="small"><span><input type="checkbox" data-action="scraper-enabled" data-id="${esc(p.plugin_id)}" ${p.enabled ? "checked" : ""}> ${p.enabled ? "enabled" : "disabled"}</span></label></td>
    <td><label class="small"><span><input type="checkbox" data-action="scraper-overwrite" data-id="${esc(p.plugin_id)}" ${p.overwrite ? "checked" : ""}> ${p.overwrite ? "on" : "off"}</span></label></td>
    <td>${p.configured ? badge("ok").replace(">ok<", ">configured<") : '<span class="badge s-warn">not configured</span>'}</td>
    <td>${p.has_config ? `<button data-action="scraper-configure" data-id="${esc(p.plugin_id)}">Configure</button>` : ""}
      <button data-action="scraper-test" data-id="${esc(p.plugin_id)}">Test</button></td></tr>`).join("")
    : '<tr><td colspan="7" class="muted">No scraper plugins installed.</td></tr>';
}

const scraperPage = { id: null, schema: [] };

async function openScraperConfig(id) {
  const data = await api("GET", `/scrapers/${encodeURIComponent(id)}/config`);
  scraperPage.id = id;
  scraperPage.schema = data.schema;
  $("#scraper-config-title").textContent = `${data.name} Settings`;
  $("#scraper-config-result").textContent = "";
  const field = (f) => {
    const value = data.values[f.key] ?? f.default ?? "";
    const help = f.help ? `<span class="small muted">${esc(f.help)}</span>` : "";
    if (f.type === "boolean") {
      return `<label class="checks"><span><input type="checkbox" name="${esc(f.key)}" ${value ? "checked" : ""}> ${esc(f.label)}</span>${help}</label>`;
    }
    if (f.type === "select") {
      return `<label>${esc(f.label)}<select name="${esc(f.key)}">${f.choices.map((c) =>
        `<option value="${esc(c.value)}" ${c.value === value ? "selected" : ""}>${esc(c.label)}</option>`).join("")}</select>${help}</label>`;
    }
    if (f.type === "secret") {
      const set = data.secrets[f.key];
      return `<label>${esc(f.label)} ${set ? badge("ok").replace(">ok<", ">configured<") : '<span class="badge s-warn">not set</span>'}
        <input type="password" name="${esc(f.key)}" autocomplete="new-password" placeholder="${set ? "Configured – leave empty to keep" : ""}">${help}</label>`;
    }
    const type = f.type === "integer" ? "number" : "text";
    const bounds = (f.minimum != null ? ` min="${f.minimum}"` : "") + (f.maximum != null ? ` max="${f.maximum}"` : "");
    return `<label>${esc(f.label)}<input type="${type}" name="${esc(f.key)}" value="${esc(value)}"${bounds}>${help}</label>`;
  };
  const basic = data.schema.filter((f) => !f.advanced);
  const advanced = data.schema.filter((f) => f.advanced);
  $("#scraper-config-fields").innerHTML = basic.map(field).join("") || '<p class="muted">This scraper has no settings.</p>';
  $("#scraper-config-advanced-fields").innerHTML = advanced.map(field).join("");
  $("#scraper-config-advanced").classList.toggle("hidden", !advanced.length);
  $("#scraper-config-tests").innerHTML = (data.tests || []).map((t) =>
    `<button type="button" data-action="scraper-test-extra" data-test="${esc(t.key)}">${esc(t.label)}</button>`).join(" ");
  renderScraperStatus(data.status);
  showTab("scraper-config");
}

function renderScraperStatus(status) {
  const entries = Object.entries(status || {});
  $("#scraper-config-status").innerHTML = entries.length
    ? `<strong>Status</strong><table><tbody>${entries.map(([k, v]) => `<tr><td class="muted">${esc(k)}</td><td>${esc(v)}</td></tr>`).join("")}</tbody></table>`
    : "";
}

function scraperFormValues() {
  const form = $("#scraper-config-form");
  const values = {};
  for (const f of scraperPage.schema) {
    const input = form.elements[f.key];
    if (!input) continue;
    values[f.key] = f.type === "boolean" ? input.checked : input.value;
  }
  return values;
}

async function saveScraperConfig(ev) {
  ev.preventDefault();
  try {
    await api("PUT", `/scrapers/${encodeURIComponent(scraperPage.id)}/config`, { values: scraperFormValues() });
    toast("Scraper settings saved");
    openScraperConfig(scraperPage.id);
  } catch (e) {
    $("#scraper-config-result").textContent = e.message;
    toast(e.message, true);
  }
}

async function testScraper(id, values, test) {
  const res = await api("POST", `/scrapers/${encodeURIComponent(id)}/test`, { ...(values ? { values } : {}), ...(test ? { test } : {}) });
  if (id === scraperPage.id) renderScraperStatus(res.status);
  return `${badge(res.ok ? "ok" : "failed")} ${esc(res.message)}`;
}

async function loadMetadataHistory() {
  const pid = $("#metadata-history-provider").value;
  const data = await api("GET", "/history/metadata" + (pid ? `?provider_id=${pid}` : ""));
  $("#metadata-history").innerHTML = data.items.length ? data.items.map((h) => `<tr>
    <td>${esc(h.provider_name)}</td><td>${fmtTime(h.started_at)}</td><td>${esc(h.scope)}${h.forced ? " (forced)" : ""}</td>
    <td>${fmtDuration(h.duration_seconds)}</td><td>${badge(h.status)}</td>
    <td>${h.considered}</td><td>${h.matched}</td><td>${h.unmatched}</td><td>${h.ambiguous}</td><td>${h.updated}</td>
    <td>${h.unchanged}</td><td>${h.skipped}</td><td>${h.error_count ? `<span class="s-err">${h.error_count}</span>` : 0}</td>
    <td class="small">${(h.plugins || []).map(esc).join(", ")}</td>
    <td><button data-action="metadata-detail" data-id="${h.id}">Details</button></td></tr>`).join("")
    : '<tr><td colspan="15" class="muted">No metadata history.</td></tr>';
}


// ---------------------------------------------------------------------------------------
// Unmatched media & manual matching
// ---------------------------------------------------------------------------------------
const unmatched = { offset: 0, limit: 50, total: 0, items: [], types: null, current: null, request: null, qTimer: null };
const UM_STATUS = { matched: "matched", unmatched: "unmatched", ambiguous: "ambiguous", invalid_binding: "match gone",
  not_found: "not found", api_error: "source error" };
const KIND_NAMES = { movie: "Movie", series: "Series" };

async function manualTypes(reload) {
  if (!unmatched.types || reload) unmatched.types = (await api("GET", "/metadata/manual-id-types")).plugins;
  return unmatched.types;
}

function fillSelect(sel, options, allLabel) {
  const current = sel.value;
  sel.innerHTML = `<option value="">${esc(allLabel)}</option>` +
    options.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("");
  sel.value = options.some(([value]) => String(value) === current) ? current : "";
}

async function loadUnmatched(resetPage) {
  const types = await manualTypes(true);
  fillSelect($("#um-provider"), state.providers.map((p) => [p.id, p.name]), "All providers");
  fillSelect($("#um-plugin"), types.map((p) => [p.plugin_id, p.name]), "All scrapers");
  if (resetPage) unmatched.offset = 0;
  const params = new URLSearchParams({ status: $("#um-status").value, limit: unmatched.limit, offset: unmatched.offset });
  if ($("#um-provider").value) params.set("provider_id", $("#um-provider").value);
  if ($("#um-type").value) params.set("type", $("#um-type").value);
  if ($("#um-plugin").value) params.set("plugin_id", $("#um-plugin").value);
  if ($("#um-q").value.trim()) params.set("q", $("#um-q").value.trim());
  const data = await api("GET", "/metadata/unmatched?" + params);
  unmatched.items = data.items;
  unmatched.total = data.total;
  if (data.offset && !data.items.length) { unmatched.offset = 0; return loadUnmatched(false); }
  $("#um-list").innerHTML = data.items.length ? data.items.map((item, i) => `<tr>
    <td><strong>${esc(item.title)}</strong>${item.year ? ` (${item.year})` : ""}<br>
      <span class="badge ${item.matched ? "s-matched" : "s-unmatched"}">${item.matched ? "Matched" : "Unmatched"}</span></td>
    <td>${esc(KIND_NAMES[item.item_kind])}</td>
    <td>${esc(item.provider_name)}<br><span class="small muted">${esc(item.category_name || "#" + item.category_id)}</span></td>
    <td>${item.plugins.map((p) => `<div class="um-plugin"><span class="badge s-${esc(p.status)}">${esc(p.name)}: ${esc(UM_STATUS[p.status] || p.status)}</span>
      ${p.manual ? '<span class="small">set by hand</span>' : ""}
      ${p.message && p.status !== "matched" ? `<span class="small muted">${esc(p.message)}</span>` : ""}</div>`).join("")}</td>
    <td class="small">${fmtTime(item.last_attempt)}</td>
    <td><button data-action="um-open" data-index="${i}">Fix match</button></td></tr>`).join("")
    : '<tr><td colspan="6" class="muted">Nothing to show for these filters.</td></tr>';
  const first = data.total ? data.offset + 1 : 0;
  $("#um-page").textContent = `${first}–${data.offset + data.items.length} of ${data.total}`;
  $('[data-action="um-prev"]').disabled = data.offset === 0;
  $('[data-action="um-next"]').disabled = data.offset + data.items.length >= data.total;
}

function itemKey(item) {
  return { item_kind: item.item_kind, provider_id: item.provider_id, category_id: item.category_id, item_id: item.item_id };
}

function bindingHtml(item, b, plugin) {
  const native = plugin && plugin.native_namespace;
  const canUse = native && (plugin.manual_id_fields[item.item_kind] || []).some((f) => f.namespace === native);
  const matched = b.status === "matched" && b.remote_id
    ? `<p class="small">Matched to <strong>${esc(b.matched_title || b.remote_id)}</strong>${b.matched_year ? ` (${b.matched_year})` : ""},
        ID ${esc(b.remote_id)}${b.manual ? " · <strong>set by hand</strong>: " + Object.entries(b.manual_ids).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(", ") : ""}</p>` : "";
  const actions = b.manual
    ? `<button data-action="um-change" data-plugin="${esc(b.plugin_id)}">Change ID</button>
       <button class="danger" data-action="um-remove" data-plugin="${esc(b.plugin_id)}">Remove manual match</button>`
    : (plugin ? `<button data-action="um-change" data-plugin="${esc(b.plugin_id)}">Enter ID</button>` : "");
  const candidates = b.candidates.length ? `<table><thead><tr><th>Candidate</th><th>Year</th><th>Score</th><th>ID</th><th></th></tr></thead><tbody>
    ${b.candidates.map((c, i) => `<tr><td>${esc(c.title || "–")}</td><td>${esc(c.year ?? "–")}</td>
      <td>${c.score != null ? esc(Math.round(c.score)) : "–"}</td><td class="small">${esc(c.remote_id)}</td>
      <td>${canUse ? `<button data-action="um-candidate" data-plugin="${esc(b.plugin_id)}" data-candidate="${i}">Use this match</button>` : ""}</td></tr>`).join("")}
    </tbody></table>` : "";
  return `<div class="um-binding"><strong>${esc(b.name)}</strong> <span class="badge s-${esc(b.status)}">${esc(UM_STATUS[b.status] || b.status)}</span>
    <span class="small muted">last attempt ${fmtTime(b.last_attempt)}</span>
    ${b.message ? `<p class="small muted">${esc(b.message)}</p>` : ""}${matched}${candidates}
    <div class="toolbar small">${actions}</div></div>`;
}

function openUnmatched(index) {
  const item = unmatched.items[Number(index)];
  unmatched.current = item;
  unmatched.request = null;
  const plugins = unmatched.types.filter((p) => p.media_types.includes(item.item_kind));
  const byId = Object.fromEntries(unmatched.types.map((p) => [p.plugin_id, p]));
  const el = $("#um-detail");
  el.innerHTML = `<h2>${esc(item.title)}${item.year ? ` (${item.year})` : ""} – ${esc(KIND_NAMES[item.item_kind])}</h2>
    <p class="small muted">${esc(item.provider_name)} · ${esc(item.category_name || "#" + item.category_id)} · Xtream ID ${esc(item.item_id)}</p>
    ${item.plugins.map((b) => bindingHtml(item, b, byId[b.plugin_id])).join("")}
    <h3>Set an ID by hand</h3>
    ${plugins.length ? `<form id="um-form" autocomplete="off">
      <div class="grid">
        <label>Scraper<select name="plugin_id">${plugins.map((p) => `<option value="${esc(p.plugin_id)}">${esc(p.name)}${
          !p.configured ? " (not configured)" : !p.enabled ? " (disabled)" : ""}</option>`).join("")}</select></label>
        <label>ID type<select name="namespace"></select></label>
        <label>ID<input name="id_value"><span class="small muted" id="um-help"></span></label>
      </div>
      <div class="toolbar"><button type="submit" class="primary">Check ID</button>
        <button type="button" data-action="um-close">Close</button><span id="um-result"></span></div>
    </form>
    <div id="um-preview" class="hidden"></div>`
    : '<p class="muted">No installed scraper accepts IDs for this type.</p><div class="toolbar"><button data-action="um-close">Close</button></div>'}`;
  el.classList.remove("hidden");
  if (plugins.length) {
    fillNamespaces();
    $("#um-form").addEventListener("submit", (ev) => { ev.preventDefault(); verifyManual(); });
  }
  el.scrollIntoView({ behavior: "smooth" });
}

function fillNamespaces(selected) {
  const form = $("#um-form");
  const plugin = unmatched.types.find((p) => p.plugin_id === form.plugin_id.value);
  const fields = (plugin && plugin.manual_id_fields[unmatched.current.item_kind]) || [];
  form.namespace.innerHTML = fields.map((f) => `<option value="${esc(f.namespace)}">${esc(f.label)}</option>`).join("");
  if (selected) form.namespace.value = selected;
  showFieldHelp();
}

function showFieldHelp() {
  const form = $("#um-form");
  const plugin = unmatched.types.find((p) => p.plugin_id === form.plugin_id.value);
  const field = ((plugin && plugin.manual_id_fields[unmatched.current.item_kind]) || []).find((f) => f.namespace === form.namespace.value);
  form.id_value.placeholder = field ? field.placeholder : "";
  let help = field ? field.help : "";
  if (plugin && !plugin.configured) help += " This scraper is not configured, so the ID cannot be checked yet.";
  else if (plugin && !plugin.enabled) help += " This scraper is disabled: the match is saved, and used once you enable it.";
  $("#um-help").textContent = help.trim();
  $("#um-preview").classList.add("hidden");
}

function idList(ids) {
  return Object.entries(ids || {}).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(" · ");
}

async function verifyManual() {
  const form = $("#um-form");
  const body = { ...itemKey(unmatched.current), plugin_id: form.plugin_id.value, namespace: form.namespace.value, value: form.id_value.value };
  $("#um-result").textContent = "Checking…";
  $("#um-preview").classList.add("hidden");
  let res;
  try {
    res = await api("POST", "/metadata/manual-match", body);
  } catch (e) {
    $("#um-result").textContent = "";
    toast(e.message, true);
    return;
  }
  $("#um-result").textContent = "";
  unmatched.request = body;
  const v = res.verified;
  const current = v.current && v.current.remote_id !== v.remote_id
    ? `<p class="small">This replaces the current ${v.current.manual ? "manual " : ""}match: ${esc(v.current.title || v.current.remote_id)}${v.current.year ? ` (${v.current.year})` : ""}.</p>` : "";
  const preview = $("#um-preview");
  preview.className = "um-preview";
  preview.innerHTML = `<p>${esc(v.plugin_name)} found <strong>${esc(v.title || "(no title)")}</strong>${v.year ? ` (${v.year})` : ""} for ${esc(v.namespace_label)} ${esc(v.value)}.</p>
    <p class="small muted">IDs at the source: ${idList(v.external_ids) || "–"}</p>
    <p class="small">Saved as final, never changed by any scraper: ${idList(v.manual_ids)}</p>${current}
    <p class="um-warn"><strong>Warning:</strong> Confirm fetches this ${esc(KIND_NAMES[unmatched.current.item_kind] || "item").toLowerCase()} from ${esc(v.plugin_name)} straight away
      (or at the next scrape if that cannot run now). The title, plot and all other details in the NFO <strong>are cleared and rebuilt</strong>
      from what the scrapers return, with ${esc(v.plugin_name)} winning even with Overwrite off. Its artwork <strong>replaces</strong> the managed images,
      and images left from the old match are <strong>removed</strong>${unmatched.current.item_kind === "series" ? ". This covers every season and episode too" : ""}.
      Changes you made by hand to those NFO fields are lost. This happens once; after that the normal Overwrite setting applies.
      Image files you added yourself are never replaced or removed.</p>
    <div class="toolbar"><button class="primary" data-action="um-confirm">Confirm</button>
      <button data-action="um-cancel">Cancel</button></div>`;
}

async function confirmManual() {
  if (!unmatched.request) return;
  try {
    const res = await api("POST", "/metadata/manual-match", { ...unmatched.request, confirm: true });
    toast(res.job ? `Saved. Refreshing details and artwork: ${res.job.description}`
      : `Saved. ${res.note || "Details and artwork are replaced at the next scrape."}`);
  } catch (e) {
    toast(e.message, true);
    return;
  }
  $("#um-detail").classList.add("hidden");
  refreshStatus();
  loadUnmatched(false);
}

async function removeManual(pluginId) {
  const b = unmatched.current.plugins.find((p) => p.plugin_id === pluginId);
  if (!confirm(`Remove the ${b ? b.name : ""} match set by hand for "${unmatched.current.title}"?\n\nThe IDs are taken out of the NFO (if unchanged) and the item is matched automatically again. No files are deleted.`)) return;
  try {
    const res = await api("POST", "/metadata/manual-match/remove", { ...itemKey(unmatched.current), plugin_id: pluginId });
    toast(res.job ? `Removed. Started: ${res.job.description}` : `Removed. ${res.note || ""}`.trim());
  } catch (e) {
    toast(e.message, true);
    return;
  }
  $("#um-detail").classList.add("hidden");
  refreshStatus();
  loadUnmatched(false);
}

function selectPlugin(pluginId, namespace) {
  const form = $("#um-form");
  if (!form) return false;
  form.plugin_id.value = pluginId;
  if (form.plugin_id.value !== pluginId) return false;
  fillNamespaces(namespace);
  return true;
}

// ---------------------------------------------------------------------------------------
// Artwork
// ---------------------------------------------------------------------------------------
const STATUS_LABELS = { ok: "ok", pending: "pending", external: "kept (not ours)", kept: "kept (newer available)",
  modified: "changed outside app", disabled: "disabled", error: "error" };

async function loadArtwork() {
  const data = await api("GET", "/artwork");
  const form = $("#artwork-form");
  for (const input of form.elements) {
    if (!input.name || !(input.name in data.settings)) continue;
    const value = data.settings[input.name];
    if (input.type === "radio") input.checked = input.value === value;
    else if (input.type === "checkbox") input.checked = !!value;
    else input.value = value;
  }
  const c = data.counts;
  const slots = Object.entries(c.slots || {}).map(([k, v]) => `<div><span class="muted">${esc(STATUS_LABELS[k] || k)}:</span> ${v}</div>`).join("");
  $("#artwork-counts").innerHTML = `<div><span class="muted">selected artwork:</span> ${c.slot_total}</div>${slots}
    <div><span class="muted">managed local files:</span> ${c.managed_files}</div>
    <div><span class="muted">protected (changed outside app):</span> ${c.modified_files}</div>
    <div><span class="muted">managed NFO links:</span> ${c.nfo_refs}</div>`;
}

async function saveArtwork(ev) {
  ev.preventDefault();
  const body = {};
  for (const input of $("#artwork-form").elements) {
    if (!input.name) continue;
    if (input.type === "radio") { if (input.checked) body[input.name] = input.value; }
    else body[input.name] = input.type === "checkbox" ? input.checked : input.value;
  }
  try {
    const res = await api("PUT", "/artwork/settings", body);
    $("#artwork-result").textContent = res.job ? "Saved. Existing artwork is being reconciled in the background." : "Saved.";
    toast(res.job ? `Started: ${res.job.description}` : "Artwork settings saved");
    refreshStatus();
    loadArtwork();
  } catch (e) {
    toast(e.message, true);
  }
}

async function startArtworkJob(force) {
  if (force && !confirm("Replace artwork files this application downloaded, including ones changed outside the application?\n\nFiles you added yourself are never touched.")) return;
  try {
    const res = await api("POST", "/artwork/reconcile", force ? { force: true } : {});
    toast(`Started: ${res.job.description}`);
    refreshStatus();
  } catch (e) {
    toast(e.message, true);
  }
}

async function loadArtworkHistory() {
  const pid = $("#artwork-history-provider").value;
  const data = await api("GET", "/history/artwork" + (pid ? `?provider_id=${pid}` : ""));
  $("#artwork-history").innerHTML = data.items.length ? data.items.map((h) => `<tr>
    <td>${esc(h.provider_name)}</td><td>${fmtTime(h.started_at)}</td><td>${esc(h.mode)}${h.forced ? " (force)" : ""}</td>
    <td>${fmtDuration(h.duration_seconds)}</td><td>${badge(h.status)}</td>
    <td>${h.considered}</td><td>${h.downloaded}</td><td>${h.nfo_urls_written}</td><td>${h.local_removed}</td>
    <td>${h.nfo_refs_removed}</td><td>${h.unchanged}</td><td>${h.skipped}</td>
    <td>${h.error_count ? `<span class="s-err">${h.error_count}</span>` : 0}</td>
    <td><button data-action="artwork-detail" data-id="${h.id}">Details</button></td></tr>`).join("")
    : '<tr><td colspan="14" class="muted">No artwork jobs yet.</td></tr>';
}

async function loadAbout() {
  const data = await api("GET", "/about");
  $("#about-version").textContent = data.version;
  $("#about-attributions").innerHTML = data.attributions.length ? data.attributions.map((a) =>
    `<p><strong>${esc(a.name)}</strong>: ${esc(a.text)} ${a.url ? `<a href="${esc(a.url)}" rel="noopener noreferrer" target="_blank">${esc(a.url)}</a>` : ""}</p>`).join("")
    : '<p class="muted">No metadata scrapers installed.</p>';
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
    const res = await api("POST", kind === "scrape" ? "/metadata/scrape" : "/" + kind, body);
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
  "scrape": (btn) => startJob("scrape", btn),
  "scraper-move": async (btn) => { await api("POST", `/scrapers/${encodeURIComponent(btn.dataset.id)}/move`, { direction: btn.dataset.dir }); loadScrapers(); },
  "scraper-configure": (btn) => openScraperConfig(btn.dataset.id),
  "scraper-test": async (btn) => { btn.disabled = true; try { const html = await testScraper(btn.dataset.id); toast(btn.closest("tr").querySelector("strong").textContent + ": " + html.replace(/<[^>]+>/g, "")); } finally { btn.disabled = false; } },
  "scraper-test-extra": async (btn) => { $("#scraper-config-result").textContent = "Testing…"; $("#scraper-config-result").innerHTML = await testScraper(scraperPage.id, scraperFormValues(), btn.dataset.test); },
  "scraper-test-form": async () => { $("#scraper-config-result").textContent = "Testing…"; $("#scraper-config-result").innerHTML = await testScraper(scraperPage.id, scraperFormValues()); },
  "scraper-back": () => showTab("scrapers"),
  "reload-metadata-history": () => loadMetadataHistory(),
  "um-reload": () => loadUnmatched(false),
  "um-prev": () => { unmatched.offset = Math.max(0, unmatched.offset - unmatched.limit); return loadUnmatched(false); },
  "um-next": () => { unmatched.offset += unmatched.limit; return loadUnmatched(false); },
  "um-open": (btn) => openUnmatched(btn.dataset.index),
  "um-close": () => $("#um-detail").classList.add("hidden"),
  "um-change": (btn) => { if (selectPlugin(btn.dataset.plugin)) $("#um-form").id_value.focus(); },
  "um-remove": (btn) => removeManual(btn.dataset.plugin),
  "um-candidate": (btn) => {
    const b = unmatched.current.plugins.find((p) => p.plugin_id === btn.dataset.plugin);
    const plugin = unmatched.types.find((p) => p.plugin_id === btn.dataset.plugin);
    if (!b || !plugin || !selectPlugin(plugin.plugin_id, plugin.native_namespace)) return;
    $("#um-form").id_value.value = b.candidates[Number(btn.dataset.candidate)].remote_id;
    return verifyManual();
  },
  "um-confirm": () => confirmManual(),
  "um-cancel": () => { unmatched.request = null; $("#um-preview").classList.add("hidden"); },
  "reload-artwork-history": () => loadArtworkHistory(),
  "artwork-reconcile": () => startArtworkJob(false),
  "artwork-force": () => startArtworkJob(true),
  "artwork-detail": async (btn) => {
    const data = await api("GET", `/history/artwork/${btn.dataset.id}`);
    const el = $("#artwork-detail"); el.innerHTML = detailHtml(data.item, "Artwork job"); el.classList.remove("hidden"); el.scrollIntoView({ behavior: "smooth" });
  },
  "metadata-detail": async (btn) => {
    const data = await api("GET", `/history/metadata/${btn.dataset.id}`);
    const el = $("#metadata-detail"); el.innerHTML = detailHtml(data.item, "Metadata scrape"); el.classList.remove("hidden"); el.scrollIntoView({ behavior: "smooth" });
  },
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
    t.id.startsWith("sync") ? loadSyncHistory() : t.id.startsWith("probe") ? loadProbeHistory()
      : t.id.startsWith("artwork") ? loadArtworkHistory() : loadMetadataHistory();
  } else if (t.classList.contains("um-filter")) {
    loadUnmatched(true).catch((e) => toast(e.message, true));
  } else if (t.form && t.form.id === "um-form" && t.name === "plugin_id") {
    fillNamespaces();
  } else if (t.form && t.form.id === "um-form" && t.name === "namespace") {
    showFieldHelp();
  } else if (t.dataset.action === "scraper-enabled" || t.dataset.action === "scraper-overwrite") {
    const field = t.dataset.action === "scraper-enabled" ? "enabled" : "overwrite";
    try { await api("POST", `/scrapers/${encodeURIComponent(t.dataset.id)}/${field}`, { [field]: t.checked }); } catch (e) { toast(e.message, true); }
    loadScrapers();
  }
});

document.addEventListener("input", (ev) => {
  if (ev.target.dataset.filter) { captureSelection(); renderCategories(); }
  if (ev.target.id === "um-q") {
    clearTimeout(unmatched.qTimer);
    unmatched.qTimer = setTimeout(() => loadUnmatched(true).catch((e) => toast(e.message, true)), 300);
  }
  if (ev.target.form && ev.target.form.id === "um-form" && ev.target.name === "id_value") {
    unmatched.request = null;
    $("#um-preview").classList.add("hidden");
  }
});

$("#provider-edit").addEventListener("submit", saveProvider);
$("#settings-form").addEventListener("submit", saveSettings);
$("#scraper-config-form").addEventListener("submit", saveScraperConfig);
$("#artwork-form").addEventListener("submit", saveArtwork);
refreshStatus();
