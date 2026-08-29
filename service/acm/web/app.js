const $ = selector => document.querySelector(selector);
const state = {
  dashboard: null,
  browse: { scope: "", kind: "", status: "", q: "", offset: 0, limit: 25, total: 0, items: [] },
  detail: null,
  usage: { total: 0, items: [], offset: 0 },
};

function esc(value = "") {
  return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}
function date(value) { return value ? new Date(value).toLocaleString() : "—"; }
function snippet(value, length = 100) { return value.length > length ? `${value.slice(0, length - 1)}…` : value; }
function sum(values = {}) { return Object.values(values).reduce((total, value) => total + Number(value), 0); }
function message(text, error = false) { $("#conn").textContent = text; $("#conn").className = error ? "error" : ""; }

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "content-type": "application/json" } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  return response.json();
}
function show(view) {
  for (const id of ["dashboard", "browse", "detail"]) $(`#${id}`).hidden = id !== view;
  for (const link of document.querySelectorAll("nav a")) link.classList.toggle("active", link.dataset.view === view);
}
function memoryRow(memory) {
  return `<tr data-id="${memory.id}"><td>#${memory.id}</td><td><span class="pill">${esc(memory.kind)}</span>${memory.pinned ? '<span class="pill pinned">pinned</span>' : ""}</td><td>${esc(snippet(memory.content))}</td><td>${esc(memory.scope)}</td><td><span class="pill ${esc(memory.status)}">${esc(memory.status)}</span></td><td class="num">${esc(memory.access_count ?? 0)}</td></tr>`;
}

async function loadDashboard() {
  state.dashboard = await api("/api/ui/dashboard");
  message("connected");
  renderDashboard();
}
function renderDashboard() {
  const dashboard = state.dashboard;
  if (!dashboard) return;
  show("dashboard");
  const totals = dashboard.totals ?? {};
  const allScopes = Object.entries(totals.scope ?? {}).sort(([leftName, leftCount], [rightName, rightCount]) => Number(rightCount) - Number(leftCount) || leftName.localeCompare(rightName));
  const scopes = allScopes.slice(0, 50);
  const moreScopes = allScopes.length - scopes.length;
  const scores = dashboard.compaction_scores ?? [];
  $("#dashboard").innerHTML = `
    <h2>Overview</h2>
    <div class="cards">
      <div class="card"><div class="label">Memories</div><div class="value">${sum(totals.kind)}</div><div class="sub">${Object.entries(totals.kind ?? {}).map(([kind, count]) => `${esc(kind)} ${count}`).join(" · ") || "none"}</div></div>
      <div class="card"><div class="label">Active / archived</div><div class="value">${Number(totals.status?.active ?? 0)} / ${Number(totals.status?.archived ?? 0)}</div><div class="sub">no hard delete</div></div>
      <div class="card"><div class="label">Bundles ✓ / ✗</div><div class="value">${Number(dashboard.bundles?.hit ?? 0)} / ${Number(dashboard.bundles?.miss ?? 0)}</div><div class="sub">${Number(dashboard.bundles?.served ?? 0)} served</div></div>
      <div class="card"><div class="label">Explicit fetches</div><div class="value">${Number(dashboard.explicit_fetch ?? 0)}</div><div class="sub">${Object.entries(totals.usage ?? {}).map(([type, count]) => `${esc(type)} ${count}`).join(" · ") || "no usage"}</div></div>
      <div class="card"><div class="label">Ingest outcomes</div><div class="value">${sum(dashboard.ingest_outcomes)}</div><div class="sub">${Object.entries(dashboard.ingest_outcomes ?? {}).map(([kind, count]) => `${esc(kind)} ${count}`).join(" · ") || "none"}</div></div>
    </div>
    <div class="detail-grid">
      <div><h2>Scopes</h2><table><thead><tr><th>Scope</th><th class="num">Memories</th></tr></thead><tbody id="scope-list">${scopes.map(([scope, count]) => `<tr data-scope="${esc(scope)}"><td>${esc(scope)}</td><td class="num">${count}</td></tr>`).join("") || '<tr><td colspan="2" class="empty">No memories yet</td></tr>'}${moreScopes ? `<tr><td colspan="2" class="muted">Top 50 by count. ${moreScopes} more: enter exact scope in Browse.</td></tr>` : ""}</tbody></table></div>
      <div><h2>Compaction validation</h2><div class="panel">${scores.length ? scores.map(score => `<div><span class="score-bar"><div style="width:${Math.max(0, Math.min(100, Number(score.validation_score) * 100))}%"></div></span> ${Math.round(Number(score.validation_score) * 100)}% <span class="muted">${esc(score.status)} · ${esc(date(score.created_at))}</span></div>`).join("") : '<span class="muted">No completed compactions.</span>'}</div></div>
    </div>
    <h2>Top used</h2><table><thead><tr><th>ID</th><th>Kind</th><th>Memory</th><th>Scope</th><th>Status</th><th>Usage</th></tr></thead><tbody id="top-used">${(dashboard.top_used ?? []).map(memoryRow).join("") || '<tr><td colspan="6" class="empty">No active memories.</td></tr>'}</tbody></table>`;
  $("#scope-list").onclick = event => {
    const row = event.target.closest("tr[data-scope]");
    if (!row) return;
    state.browse = { ...state.browse, scope: row.dataset.scope, offset: 0 };
    location.hash = "browse";
  };
  $("#top-used").onclick = event => openDetail(event.target.closest("tr[data-id]")?.dataset.id);
}

async function loadBrowse() {
  const browse = state.browse;
  const params = new URLSearchParams({ limit: String(browse.limit), offset: String(browse.offset) });
  for (const key of ["scope", "kind", "status", "q"]) if (browse[key]) params.set(key, browse[key]);
  const result = await api(`/api/ui/memories?${params}`);
  state.browse = { ...browse, total: result.total, items: result.items };
  renderBrowse();
}
function renderBrowse() {
  show("browse");
  const browse = state.browse;
  const kinds = Object.keys(state.dashboard?.totals?.kind ?? {}).sort();
  
  $("#browse").innerHTML = `
    <h2>Browse memories</h2>
    <form class="filters" id="browse-filters">
      <input name="scope" value="${esc(browse.scope)}" placeholder="Scope (exact)" aria-label="Scope filter">
      <select name="kind"><option value="">All kinds</option>${kinds.map(kind => `<option value="${esc(kind)}" ${kind === browse.kind ? "selected" : ""}>${esc(kind)}</option>`).join("")}</select>
      <select name="status"><option value="">All status</option><option value="active" ${browse.status === "active" ? "selected" : ""}>active</option><option value="archived" ${browse.status === "archived" ? "selected" : ""}>archived</option></select>
      <input name="q" value="${esc(browse.q)}" placeholder="Search content" aria-label="Search memory content">
      <button class="primary">Filter</button>
    </form>
    <table><thead><tr><th>ID</th><th>Kind</th><th>Content</th><th>Scope</th><th>Status</th><th>Usage</th></tr></thead><tbody id="memory-list">${browse.items.map(memoryRow).join("") || '<tr><td colspan="6" class="empty">No matching memories.</td></tr>'}</tbody></table>
    <div class="pager"><button id="previous" ${browse.offset === 0 ? "disabled" : ""}>Previous</button><span class="muted">${browse.total ? `${browse.offset + 1}–${Math.min(browse.offset + browse.limit, browse.total)} of ${browse.total}` : "0 memories"}</span><button id="next" ${browse.offset + browse.limit >= browse.total ? "disabled" : ""}>Next</button></div>`;
  $("#browse-filters").onsubmit = event => {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    state.browse = { ...browse, scope: String(values.get("scope") ?? ""), kind: String(values.get("kind") ?? ""), status: String(values.get("status") ?? ""), q: String(values.get("q") ?? ""), offset: 0 };
    loadBrowse().catch(displayError);
  };
  $("#memory-list").onclick = event => openDetail(event.target.closest("tr[data-id]")?.dataset.id);
  $("#previous").onclick = () => { state.browse.offset -= browse.limit; loadBrowse().catch(displayError); };
  $("#next").onclick = () => { state.browse.offset += browse.limit; loadBrowse().catch(displayError); };
}

async function openDetail(id) {
  if (!id) return;
  const [detail, usage] = await Promise.all([api(`/api/ui/memories/${id}`), api(`/api/ui/usage/${id}?limit=20`)]);
  state.detail = detail;
  state.usage = { total: usage.total, items: usage.items, offset: 0 };
  location.hash = `memory/${id}`;
  renderDetail();
}
function entityMarkup(entity) {
  return `<div class="alias-row"><span>${esc(entity.canonical_name)}${entity.role ? ` <span class="muted">${esc(entity.role)}</span>` : ""}</span>${(entity.aliases ?? []).map(alias => `<span class="pill">${esc(alias)} <button data-remove-alias="${entity.id}" data-alias="${esc(alias)}" title="Remove alias">×</button></span>`).join("")}<button data-add-alias="${entity.id}">+ alias</button></div>`;
}
function renderDetail() {
  const memory = state.detail;
  if (!memory) return;
  show("detail");
  const usage = state.usage;
  $("#detail").innerHTML = `
    <button id="back">← Browse</button>
    <h2>Memory #${memory.id}</h2>
    <div class="detail-grid">
      <div>
        <div class="panel"><pre>${esc(memory.content)}</pre></div>
        <div class="actions">
          <button id="edit">Edit</button><button id="pin">${memory.pinned ? "Unpin" : "Pin"}</button><button id="importance">Set importance</button>
          ${memory.status === "active" ? '<button id="archive" class="danger">Archive</button>' : '<button id="restore">Restore</button>'}
          <button id="merge" class="danger">Merge into…</button>
        </div>
        <div id="edit-form" hidden><textarea id="edit-content">${esc(memory.content)}</textarea><div class="actions"><button id="save-edit" class="primary">Save content</button><button id="cancel-edit">Cancel</button></div></div>
        <div id="merge-form" hidden></div>
        <h2>Usage timeline</h2><div class="panel" id="usage-list">${usage.items.map(item => `<div><strong>${esc(item.use_type)}</strong> <span class="muted">${esc(date(item.ts))} · ${esc(item.scope ?? "no scope")} · ${esc(item.session_id ?? "no session")}</span></div>`).join("") || '<span class="muted">No recorded usage.</span>'}${usage.items.length < usage.total ? '<div class="actions"><button id="more-usage">Load more</button></div>' : ""}</div>
      </div>
      <aside><div class="panel"><dl class="kv"><dt>Scope</dt><dd>${esc(memory.scope)}</dd><dt>Kind</dt><dd>${esc(memory.kind)}</dd><dt>Importance</dt><dd>${Number(memory.importance).toFixed(2)}</dd><dt>Status</dt><dd>${esc(memory.status)}</dd><dt>Validity</dt><dd>${esc(memory.valid_from ?? "—")} → ${esc(memory.valid_until ?? "—")}</dd><dt>Provenance</dt><dd>${esc(memory.source_ref ?? "—")}</dd><dt>Created</dt><dd>${esc(date(memory.created_at))}</dd><dt>Updated</dt><dd>${esc(date(memory.updated_at))}</dd></dl></div><h2>Entities</h2><div class="panel" id="entities">${(memory.entities ?? []).map(entityMarkup).join("") || '<span class="muted">No entities linked.</span>'}</div></aside>
    </div>`;
  $("#back").onclick = () => { location.hash = "browse"; };
  $("#edit").onclick = () => { $("#edit-form").hidden = false; };
  $("#cancel-edit").onclick = () => { $("#edit-form").hidden = true; };
  $("#save-edit").onclick = () => mutate(`/memories/${memory.id}`, "PATCH", { content: $("#edit-content").value });
  $("#pin").onclick = () => mutate(`/memories/${memory.id}`, "PATCH", { pinned: !memory.pinned });
  $("#importance").onclick = () => {
    const value = Number(prompt("Importance (0–1)", String(memory.importance)));
    if (Number.isFinite(value) && value >= 0 && value <= 1) mutate(`/memories/${memory.id}`, "PATCH", { importance: value });
  };
  $("#archive")?.addEventListener("click", () => { if (confirm("Archive this memory? It remains recoverable.")) mutate(`/memories/${memory.id}/archive`, "POST"); });
  $("#restore")?.addEventListener("click", () => mutate(`/memories/${memory.id}/restore`, "POST"));
  $("#merge").onclick = () => showMergeTargets(memory).catch(displayError);
  $("#more-usage")?.addEventListener("click", () => loadMoreUsage(memory.id).catch(displayError));
  $("#entities").onclick = event => {
    const remove = event.target.closest("button[data-remove-alias]");
    if (remove && confirm(`Remove alias ${remove.dataset.alias}?`)) api(`/entities/${remove.dataset.removeAlias}/aliases`, { method: "DELETE", body: { alias: remove.dataset.alias } }).then(() => openDetail(memory.id)).catch(displayError);
    const add = event.target.closest("button[data-add-alias]");
    if (add) { const alias = prompt("Alias"); if (alias?.trim()) api(`/entities/${add.dataset.addAlias}/aliases`, { method: "POST", body: { alias } }).then(() => openDetail(memory.id)).catch(displayError); }
  };
}
async function mutate(path, method, body) { await api(path, { method, body }); await Promise.all([loadDashboard(), openDetail(state.detail.id)]); }
async function showMergeTargets(memory) {
  const result = await api(`/api/ui/memories?${new URLSearchParams({ scope: memory.scope, status: "active", limit: "200" })}`);
  const targets = result.items.filter(item => item.id !== memory.id);
  const form = $("#merge-form");
  form.hidden = false;
  form.innerHTML = targets.length ? `<div class="panel"><label>Merge #${memory.id} into <select id="merge-target">${targets.map(target => `<option value="${target.id}">#${target.id} ${esc(snippet(target.content, 70))}</option>`).join("")}</select></label><button id="confirm-merge" class="danger">Merge</button></div>` : '<div class="panel muted">No active target in this scope.</div>';
  $("#confirm-merge")?.addEventListener("click", () => { const target = Number($("#merge-target").value); if (confirm(`Merge memory #${memory.id} into #${target}? Source becomes archived.`)) api(`/memories/${memory.id}/merge`, { method: "POST", body: { target_id: target } }).then(() => openDetail(target)).then(loadDashboard).catch(displayError); });
}
async function loadMoreUsage(id) {
  const offset = state.usage.items.length;
  const more = await api(`/api/ui/usage/${id}?limit=20&offset=${offset}`);
  state.usage = { total: more.total, items: [...state.usage.items, ...more.items], offset };
  renderDetail();
}
function displayError(error) { console.error(error); message(`error: ${error.message}`, true); }
async function route() {
  const match = location.hash.match(/^#memory\/(\d+)$/);
  if (match) { if (!state.detail || String(state.detail.id) !== match[1]) await openDetail(match[1]); return; }
  if (location.hash === "#browse") { await loadBrowse(); return; }
  location.hash = "dashboard";
  await loadDashboard();
}
window.addEventListener("hashchange", () => route().catch(displayError));
route().catch(displayError);
