// Career Agent web UI: plain JS, no build step.
const $ = (s) => document.querySelector(s);
const state = { user: null, profile: null, prefs: null };

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { return null; }
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (state.user) headers["X-User-Id"] = state.user;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = typeof data.detail === "string" ? data.detail : `Request failed (${res.status})`;
    throw Object.assign(new Error(msg), { status: res.status });
  }
  return data;
}

async function ensureUser() {
  state.user = store("career-agent-user");
  if (state.user) {
    try {
      const d = await api("/api/profile");
      state.profile = d.profile; state.prefs = d.preferences;
      return;
    } catch (e) { if (e.status !== 401) throw e; }
  }
  state.user = (await api("/api/users", { method: "POST" })).user_id;
  store("career-agent-user", state.user);
}

// ---------------------------------------------------------------- rendering helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Tiny, safe Markdown: escape first, then bold / italics / links / bullets.
function md(text) {
  const lines = esc(text).split("\n");
  let html = "", inList = false;
  for (const raw of lines) {
    let l = raw
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|\s)\*(\S.+?)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">link</a>');
    const item = l.match(/^\s*[-*]\s+(.*)/);
    const heading = l.match(/^\s*#{1,4}\s+(.*)/);
    if (heading) {
      if (inList) { html += "</ul>"; inList = false; }
      html += `<p><strong>${heading[1]}</strong></p>`;
    } else if (item) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${item[1]}</li>`;
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      if (l.trim()) html += `<p>${l}</p>`;
    }
  }
  return html + (inList ? "</ul>" : "");
}

function jobCard(j, { showRanks = false } = {}) {
  const node = $("#job-tpl").content.firstElementChild.cloneNode(true);
  const a = node.querySelector(".job-title");
  a.href = j.url; a.textContent = j.title;
  node.querySelector(".job-meta").textContent = [j.company, j.location || "Location n/a", j.department].filter(Boolean).join(" · ");
  node.querySelector(".job-reason").textContent = j.reason || "";
  const badges = node.querySelector(".badges");
  const add = (t) => { const b = document.createElement("span"); b.className = "badge"; b.textContent = t; badges.append(b); };
  if (j.remote) add("remote");
  if (showRanks) {
    add(`kw ${j.keyword_rank ?? "–"}`);
    add(`vec ${j.vector_rank ?? "–"}`);
  }
  node.querySelectorAll("[data-act]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api("/api/feedback", { method: "POST", json: { job_id: j.id, action: btn.dataset.act } });
        if (btn.dataset.act === "dismiss") node.remove();
        else btn.textContent = "Saved";
      } catch (e) { btn.textContent = e.message; }
    }));
  return node;
}

function renderList(el, jobs, opts) {
  el.replaceChildren();
  if (!jobs.length) { el.innerHTML = '<p class="muted">No matching jobs.</p>'; return; }
  jobs.forEach((j) => el.append(jobCard(j, opts)));
}

function setStatus(msg, error = false) {
  const s = $("#profile-status");
  s.textContent = msg; s.classList.toggle("error", error);
}

// ---------------------------------------------------------------- profile + preferences
function renderProfile() {
  const p = state.profile;
  $("#profile-empty").hidden = !!p;
  $("#profile-view").hidden = !p;
  $("#prefs").hidden = !p;
  if (!p) return;
  const chips = (xs) => `<div class="chips">${xs.map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</div>`;
  $("#profile-view").innerHTML = `
    <p><strong>${esc(p.name || "You")}</strong><br><span class="muted small">${esc(p.headline)}</span></p>
    <p class="small muted">Level: ${esc(p.seniority)} · ${esc(p.years_experience)} yrs</p>
    <h3>Target roles</h3>${chips(p.target_roles)}
    <h3>Skills</h3>${chips(p.skills.slice(0, 18))}
    <button class="btn ghost small" id="replace-resume">Replace resume</button>`;
  $("#replace-resume").onclick = () => { state.profile = null; renderProfile(); };
  renderPrefs();
}

function renderPrefs() {
  const p = state.prefs || {};
  $("#pref-india").checked = !!p.india_only;
  $("#pref-remote").checked = p.remote_ok !== false;
  const mem = [
    p.locations?.length && `Cities: ${p.locations.join(", ")}`,
    p.roles?.length && `Roles: ${p.roles.join(", ")}`,
    p.avoid_companies?.length && `Avoiding: ${p.avoid_companies.join(", ")}`,
    ...(p.notes || []),
  ].filter(Boolean);
  $("#prefs-memory").textContent = mem.length ? "Agent memory: " + mem.join(" · ") : "Tell the agent your preferences and it will remember them.";
}

async function savePrefs() {
  const prefs = { ...(state.prefs || {}), india_only: $("#pref-india").checked, remote_ok: $("#pref-remote").checked };
  state.prefs = (await api("/api/preferences", { method: "PUT", json: prefs })).preferences;
  loadFeed();
}

async function uploadResume(form) {
  setStatus("Analysing resume…");
  try {
    const d = await api("/api/profile", { method: "POST", body: form });
    state.profile = d.profile; state.prefs = d.preferences;
    setStatus("");
    renderProfile();
    loadFeed();
  } catch (e) { setStatus(e.message, true); }
}

// ---------------------------------------------------------------- jobs
async function loadFeed() {
  const el = $("#feed");
  if (!state.profile) return;
  el.innerHTML = '<p class="muted">Ranking jobs for you…</p>';
  try {
    renderList(el, (await api("/api/recommendations?limit=20")).jobs);
  } catch (e) { el.innerHTML = `<p class="status error">${esc(e.message)}</p>`; }
}

async function runSearch(ev) {
  ev?.preventDefault();
  const params = new URLSearchParams({
    q: $("#q").value, method: $("#method").value,
    india_only: $("#s-india").checked, remote_only: $("#s-remote").checked, limit: 20,
  });
  const el = $("#results");
  el.innerHTML = '<p class="muted">Searching…</p>';
  try {
    renderList(el, (await api(`/api/search?${params}`)).jobs, { showRanks: true });
  } catch (e) { el.innerHTML = `<p class="status error">${esc(e.message)}</p>`; }
}

async function loadSaved() {
  try { renderList($("#saved"), (await api("/api/saved")).jobs); }
  catch (e) { $("#saved").innerHTML = `<p class="status error">${esc(e.message)}</p>`; }
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on); t.setAttribute("aria-selected", on);
  });
  document.querySelectorAll(".tabpane").forEach((p) => (p.hidden = p.id !== `tab-${name}`));
  if (name === "saved") loadSaved();
}

// ---------------------------------------------------------------- chat
function addMessage(role, text, trace) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = role === "assistant" ? md(text) : esc(text);
  wrap.append(bubble);
  if (trace?.length) {
    const d = document.createElement("details");
    d.className = "trace";
    d.innerHTML = `<summary>${trace.length} tool call${trace.length > 1 ? "s" : ""}</summary>` +
      trace.map((t) => `<div class="${t.ok ? "" : "bad"}">${t.step}. <code>${esc(t.tool)}(${esc(JSON.stringify(t.args))})</code> → ${esc(t.result)} · ${t.ms} ms</div>`).join("");
    wrap.append(d);
  }
  $("#messages").append(wrap);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return wrap;
}

async function sendChat(ev) {
  ev.preventDefault();
  const input = $("#chat-msg");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addMessage("user", text);
  const pending = addMessage("assistant", "Thinking…");
  try {
    const d = await api("/api/chat", { method: "POST", json: { message: text } });
    pending.remove();
    addMessage("assistant", d.reply, d.trace);
    state.prefs = d.preferences; renderPrefs();
  } catch (e) {
    pending.querySelector(".bubble").textContent = e.message;
  }
}

async function loadHistory() {
  try {
    const { messages } = await api("/api/chat");
    messages.forEach((m) => addMessage(m.role, m.content));
  } catch { /* fresh user */ }
}

async function loadStats() {
  try {
    const { jobs, last_ingest } = await api("/api/stats");
    const when = last_ingest?.finished_at ? new Date(last_ingest.finished_at).toLocaleString() : "never";
    $("#stats").textContent = `${jobs.active.toLocaleString()} live jobs · ${jobs.companies} companies · ${jobs.india.toLocaleString()} in India · refreshed ${when}`;
  } catch { $("#stats").textContent = "Index unavailable"; }
}

// ---------------------------------------------------------------- wire up
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
$("#resume-file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const form = new FormData(); form.append("file", f);
  uploadResume(form);
});
$("#resume-text-btn").addEventListener("click", () => {
  const form = new FormData(); form.append("text", $("#resume-text").value);
  uploadResume(form);
});
$("#pref-india").addEventListener("change", savePrefs);
$("#pref-remote").addEventListener("change", savePrefs);
$("#refresh-feed").addEventListener("click", loadFeed);
$("#search-form").addEventListener("submit", runSearch);
$("#chat-form").addEventListener("submit", sendChat);
$("#clear-chat").addEventListener("click", async () => {
  await api("/api/chat", { method: "DELETE" });
  [...$("#messages").children].slice(1).forEach((c) => c.remove());
});

(async () => {
  loadStats();
  try {
    await ensureUser();
    renderProfile();
    loadFeed();
    loadHistory();
  } catch (e) { setStatus(e.message, true); }
})();
