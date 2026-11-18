// Career Agent web UI: plain JS, no build step.
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const state = { user: null, profile: null, prefs: null, saved: new Set(), method: "hybrid", tab: "feed", jobs: new Map() };
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

// ---------------------------------------------------------------- storage + api
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

// ---------------------------------------------------------------- helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name, cls = "icon") => `<svg class="${cls}"><use href="#i-${name}"/></svg>`;

function hash(s) {
  let h = 0;
  for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) | 0;
  return Math.abs(h);
}
// Monochrome company marks: a few distinct shades of ink, picked by name so each company stays consistent.
const MARKS = ["#0a0a0a", "#262626", "#404040", "#525252", "#171717"];
function avatarStyle(name) {
  return `background: ${MARKS[hash(name) % MARKS.length]}`;
}
function initials(name) {
  const words = String(name).replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
  return (words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2)).toUpperCase();
}
function ago(iso) {
  if (!iso) return null;
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  if (Number.isNaN(days) || days < 0) return null;
  if (days === 0) return { text: "today", fresh: true };
  if (days < 7) return { text: `${days}d ago`, fresh: true };
  if (days < 60) return { text: `${Math.floor(days / 7)}w ago`, fresh: false };
  return { text: `${Math.floor(days / 30)}mo ago`, fresh: false };
}

// Safe Markdown subset: escape first, then headings / bold / italics / links / lists.
function md(text) {
  const inline = (l) => l
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*(\S[^*]*?)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">link</a>');
  let html = "", list = null;
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of esc(text).split("\n")) {
    const ul = raw.match(/^\s*[-*]\s+(.*)/), ol = raw.match(/^\s*\d+[.)]\s+(.*)/), h = raw.match(/^\s*#{1,4}\s+(.*)/);
    if (h) { close(); html += `<h4>${inline(h[1])}</h4>`; }
    else if (ul || ol) {
      const kind = ul ? "ul" : "ol";
      if (list !== kind) { close(); html += `<${kind}>`; list = kind; }
      html += `<li>${inline((ul || ol)[1])}</li>`;
    } else { close(); if (raw.trim()) html += `<p>${inline(raw)}</p>`; }
  }
  close();
  return html;
}

function toast(message, { error = false, action, onAction, ms = 3200 } = {}) {
  const t = document.createElement("div");
  t.className = `toast${error ? " error" : ""}`;
  t.innerHTML = `<span>${esc(message)}</span>`;
  if (action) {
    const b = document.createElement("button");
    b.className = "btn ghost small";
    b.style.pointerEvents = "auto";
    b.textContent = action;
    b.onclick = () => { onAction?.(); dismissToast(t); };
    t.append(b);
  }
  $("#toasts").append(t);
  setTimeout(() => dismissToast(t), ms);
}
function dismissToast(t) {
  if (!t.isConnected) return;
  t.classList.add("out");
  t.addEventListener("animationend", () => t.remove(), { once: true });
}

function countUp(el, to) {
  if (reduceMotion) { el.textContent = to.toLocaleString(); return; }
  const start = performance.now(), dur = 1100;
  const step = (now) => {
    const p = Math.min(1, (now - start) / dur), eased = 1 - Math.pow(1 - p, 3);
    el.textContent = Math.round(to * eased).toLocaleString();
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// ---------------------------------------------------------------- theme
function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  const dark = theme ? theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  $("#theme-toggle").innerHTML = icon(dark ? "sun" : "moon");
}
$("#theme-toggle").addEventListener("click", () => {
  const current = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  store("ca-theme", next);
  applyTheme(next);
});
applyTheme(store("ca-theme"));

// ---------------------------------------------------------------- sliding indicators
function moveIndicator(container, activeSel, indicatorSel) {
  const active = $(activeSel, container), ind = $(indicatorSel, container);
  if (!active || !ind) return;
  ind.style.width = `${active.offsetWidth}px`;
  ind.style.transform = `translateX(${active.offsetLeft}px)`;
}
const syncIndicators = () => {
  moveIndicator($("#tabs"), ".tab.active", ".tab-indicator");
  moveIndicator($("#method"), ".seg.active", ".seg-indicator");
};
addEventListener("resize", syncIndicators);

// ---------------------------------------------------------------- user + profile
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

function chips(items, cls = "", start = 0) {
  return `<div class="chips">${items.map((x, i) => `<span class="chip ${cls}" style="animation-delay:${(start + i) * 35}ms">${esc(x)}</span>`).join("")}</div>`;
}

function renderProfile() {
  const p = state.profile;
  $("#profile-empty").hidden = !!p;
  $("#profile-view").hidden = !p;
  $("#prefs").hidden = !p;
  $("#onboarding").hidden = !!p;
  if (!p) return;
  $("#profile-view").innerHTML = `
    <div class="profile-head">
      <div class="me-avatar">${esc(initials(p.name || "You"))}</div>
      <div class="grow">
        <h2>${esc(p.name || "You")}</h2>
        <span class="level">${esc(p.seniority)} · ${esc(p.years_experience)} yrs</span>
      </div>
    </div>
    <p class="muted small">${esc(p.headline)}</p>
    <div class="profile-section"><h3>Target roles</h3>${chips(p.target_roles, "accent")}</div>
    <div class="profile-section"><h3>Skills</h3>${chips(p.skills.slice(0, 18), "", p.target_roles.length)}</div>
    <div class="profile-section"><button class="btn ghost small" id="replace-resume">${icon("upload")} Replace resume</button></div>`;
  $("#replace-resume").onclick = () => { $("#onboarding").hidden = false; $("#onboarding").scrollIntoView({ behavior: "smooth" }); };
  renderPrefs();
}

function renderPrefs() {
  const p = state.prefs || {};
  $("#pref-india").checked = !!p.india_only;
  $("#pref-remote").checked = p.remote_ok !== false;
  const items = [
    p.locations?.length && `Prefers ${p.locations.join(", ")}`,
    p.roles?.length && `Roles: ${p.roles.join(", ")}`,
    p.avoid_companies?.length && `Avoids ${p.avoid_companies.join(", ")}`,
    p.india_only && "India-based roles only",
    p.remote_ok === false && "No remote roles",
    ...(p.notes || []),
  ].filter(Boolean);
  $("#memory-list").innerHTML = items.length
    ? items.map((x, i) => `<li style="animation-delay:${i * 60}ms">${esc(x)}</li>`).join("")
    : `<li class="none">Tell the agent a preference, e.g. “only Bengaluru”.</li>`;
}

async function savePrefs() {
  const prefs = { ...(state.prefs || {}), india_only: $("#pref-india").checked, remote_ok: $("#pref-remote").checked };
  try {
    state.prefs = (await api("/api/preferences", { method: "PUT", json: prefs })).preferences;
    renderPrefs();
    loadFeed();
  } catch (e) { toast(e.message, { error: true }); }
}

async function uploadResume(form) {
  const dz = $("#dropzone");
  dz.classList.add("busy");
  $(".dz-title", dz).textContent = "Reading your resume…";
  try {
    const d = await api("/api/profile", { method: "POST", body: form });
    state.profile = d.profile; state.prefs = d.preferences;
    renderProfile();
    switchTab("feed");
    loadFeed();
    toast(`Profile ready: ${d.profile.skills.length} skills found`);
  } catch (e) {
    toast(e.message, { error: true, ms: 5000 });
  } finally {
    dz.classList.remove("busy");
    $(".dz-title", dz).textContent = "Drop your resume PDF here";
  }
}

// ---------------------------------------------------------------- job cards
function jobCard(j, i, { showRanks = false } = {}) {
  state.jobs.set(j.id, j);
  const node = $("#job-tpl").content.firstElementChild.cloneNode(true);
  node.style.setProperty("--i", i);
  node.dataset.id = j.id;
  const av = $(".avatar", node);
  av.textContent = initials(j.company);
  av.setAttribute("style", avatarStyle(j.company));
  $(".job-title", node).textContent = j.title;
  $(".job-company", node).textContent = [j.company, j.department].filter(Boolean).join(" · ");

  const tags = [];
  if (j.location) tags.push(`<span class="tag">${icon("pin")}${esc(j.location.length > 42 ? j.location.slice(0, 40) + "…" : j.location)}</span>`);
  if (j.remote) tags.push(`<span class="tag remote">${icon("globe")}Remote</span>`);
  const when = ago(j.posted_at);
  if (when) tags.push(`<span class="tag ${when.fresh ? "fresh" : ""}">${icon("clock")}${when.text}</span>`);
  $(".tags", node).innerHTML = tags.join("");

  if (j.reason) {
    const why = $(".why", node);
    why.hidden = false;
    $("span", why).textContent = j.reason;
    const ring = $(".ring", node), score = Math.max(0, Math.min(10, Math.round(j.score)));
    ring.hidden = false;
    ring.classList.toggle("high", score >= 8);
    $("b", ring).textContent = score;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      $(".ring-fg", ring).style.strokeDashoffset = 97.4 * (1 - score / 10);
    }));
  }
  if (showRanks) {
    const r = $(".ranks", node);
    r.hidden = false;
    const bar = (label, rank) => `<span class="rank">${label} <i style="--w:${rank ? Math.max(6, 100 - (rank - 1) * 2) : 0}%"></i> ${rank ? "#" + rank : "–"}</span>`;
    r.innerHTML = bar("keyword", j.keyword_rank) + bar("vector", j.vector_rank);
  }

  const save = $(".save", node);
  const paintSave = () => {
    const on = state.saved.has(j.id);
    save.classList.toggle("on", on);
    save.title = on ? "Saved" : "Save";
  };
  paintSave();
  save.addEventListener("click", async (e) => {
    e.stopPropagation();
    const on = state.saved.has(j.id);
    try {
      await api("/api/feedback", { method: "POST", json: { job_id: j.id, action: on ? "clear" : "save" } });
      on ? state.saved.delete(j.id) : state.saved.add(j.id);
      paintSave();
      updateSavedCount(true);
      toast(on ? "Removed from saved" : "Saved for later");
      if (state.tab === "saved" && on) removeCard(node);
    } catch (err) { toast(err.message, { error: true }); }
  });
  $(".dismiss", node).addEventListener("click", async (e) => {
    e.stopPropagation();
    try {
      await api("/api/feedback", { method: "POST", json: { job_id: j.id, action: "dismiss" } });
      state.saved.delete(j.id);
      updateSavedCount();
      removeCard(node);
      toast("Hidden. It won't be recommended again.", {
        action: "Undo",
        onAction: async () => { await api("/api/feedback", { method: "POST", json: { job_id: j.id, action: "clear" } }); refreshActive(); },
      });
    } catch (err) { toast(err.message, { error: true }); }
  });
  node.addEventListener("click", () => openDrawer(j.id));
  node.addEventListener("keydown", (e) => { if (e.key === "Enter") openDrawer(j.id); });
  node.addEventListener("pointermove", (e) => {
    const r = node.getBoundingClientRect();
    node.style.setProperty("--mx", `${e.clientX - r.left}px`);
    node.style.setProperty("--my", `${e.clientY - r.top}px`);
  });
  return node;
}

function removeCard(node) {
  node.style.height = `${node.offsetHeight}px`;
  node.classList.add("leaving");
  node.addEventListener("animationend", () => {
    node.classList.add("collapse");
    setTimeout(() => node.remove(), 360);
  }, { once: true });
}

function renderList(el, jobs, opts) {
  el.replaceChildren();
  if (!jobs.length) {
    el.innerHTML = emptyState("search", "No matching jobs", "Try a broader query or turn off a filter.");
    return;
  }
  jobs.forEach((j, i) => el.append(jobCard(j, i, opts)));
}

function skeletons(el, n = 5) {
  el.innerHTML = Array.from({ length: n }, (_, i) => `
    <div class="skeleton" style="opacity:${1 - i * 0.14}">
      <span class="sk" style="left:16px;top:16px;width:44px;height:44px;border-radius:12px"></span>
      <span class="sk" style="left:74px;top:18px;width:46%;height:14px"></span>
      <span class="sk" style="left:74px;top:42px;width:28%;height:11px"></span>
      <span class="sk" style="left:74px;top:72px;width:60%;height:22px;border-radius:999px"></span>
    </div>`).join("");
}

function emptyState(iconName, title, text) {
  return `<div class="empty">${icon(iconName, "icon big")}<strong>${esc(title)}</strong>${esc(text)}</div>`;
}

function updateSavedCount(bump = false) {
  const c = $("#saved-count");
  c.textContent = state.saved.size;
  if (bump) { c.classList.remove("bump"); void c.offsetWidth; c.classList.add("bump"); }
}

// ---------------------------------------------------------------- feeds
async function loadFeed() {
  const el = $("#feed");
  if (!state.profile) {
    el.innerHTML = emptyState("sparkle", "Your feed appears here", "Upload a resume above and the agent will rank live jobs for you.");
    return;
  }
  const run = ++feedRun;  // ignore stale responses if the feed is reloaded mid-flight
  const status = $("#feed-status");
  skeletons(el);
  // Phase 1: instant hybrid ranking (no AI call). Phase 2: AI rerank with reasons, swapped in when ready.
  try {
    const fast = await api("/api/recommendations?limit=20&rerank=false");
    if (run !== feedRun) return;
    renderList(el, fast.jobs);
  } catch (e) { el.innerHTML = `<div class="empty error">${esc(e.message)}</div>`; return; }
  status.innerHTML = `<span class="spinner"></span> AI is reranking…`;
  try {
    const smart = await api("/api/recommendations?limit=20&rerank=true");
    if (run !== feedRun) return;
    renderList(el, smart.jobs);
    status.textContent = "Ranked against your profile · AI-reranked";
  } catch (e) {
    if (run !== feedRun) return;
    status.textContent = "Ranked against your profile · AI rerank unavailable";
    toast(e.message, { error: true });
  }
}
let feedRun = 0;

async function runSearch(ev) {
  ev?.preventDefault();
  const q = $("#q").value.trim();
  if (!q) return;
  const params = new URLSearchParams({
    q, method: state.method, india_only: $("#s-india").checked, remote_only: $("#s-remote").checked, limit: 20,
  });
  const el = $("#results");
  skeletons(el);
  try {
    renderList(el, (await api(`/api/search?${params}`)).jobs, { showRanks: state.method !== "hybrid_rerank" });
  } catch (e) { el.innerHTML = `<div class="empty error">${esc(e.message)}</div>`; }
}

async function loadSaved(render = true) {
  try {
    const { jobs } = await api("/api/saved");
    state.saved = new Set(jobs.map((j) => j.id));
    updateSavedCount();
    if (render) {
      const el = $("#saved");
      if (!jobs.length) el.innerHTML = emptyState("bookmark", "Nothing saved yet", "Tap the bookmark on any job to keep it here.");
      else renderList(el, jobs);
    }
  } catch { /* user not ready */ }
}

function refreshActive() {
  if (state.tab === "feed") loadFeed();
  else if (state.tab === "saved") loadSaved();
  else runSearch();
}

function switchTab(name) {
  state.tab = name;
  $$(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on);
  });
  $$(".tabpane").forEach((p) => {
    const show = p.id === `tab-${name}`;
    if (show && p.hidden) { p.hidden = false; p.style.animation = "none"; void p.offsetWidth; p.style.animation = ""; }
    else if (!show) p.hidden = true;
  });
  syncIndicators();
  if (name === "saved") loadSaved();
  if (name === "search") setTimeout(() => $("#q").focus(), 50);
}

// ---------------------------------------------------------------- drawer
async function openDrawer(id) {
  const drawer = $("#drawer"), body = $("#drawer-body");
  $("#scrim").hidden = false;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  body.innerHTML = `<div class="skeleton" style="height:90px;margin-bottom:16px"></div><div class="skeleton" style="height:340px"></div>`;
  try {
    const j = await api(`/api/jobs/${encodeURIComponent(id)}`);
    const when = ago(j.posted_at);
    const saved = state.saved.has(j.id);
    body.innerHTML = `
      <div class="d-head">
        <div class="avatar" style="${avatarStyle(j.company)}">${esc(initials(j.company))}</div>
        <div><h2 class="d-title" id="d-title">${esc(j.title)}</h2><p class="muted">${esc([j.company, j.department].filter(Boolean).join(" · "))}</p></div>
      </div>
      <div class="tags">
        ${j.location ? `<span class="tag">${icon("pin")}${esc(j.location)}</span>` : ""}
        ${j.remote ? `<span class="tag remote">${icon("globe")}Remote</span>` : ""}
        ${j.employment_type ? `<span class="tag">${esc(j.employment_type)}</span>` : ""}
        ${when ? `<span class="tag ${when.fresh ? "fresh" : ""}">${icon("clock")}Posted ${when.text}</span>` : ""}
      </div>
      <div class="d-actions">
        <a class="btn primary" href="${esc(j.url)}" target="_blank" rel="noopener">Apply on ${esc(j.company)} ${icon("arrow")}</a>
        <button class="btn" id="d-match" ${state.profile ? "" : "disabled title='Upload a resume first'"}>${icon("sparkle")} Analyse my match</button>
        <button class="btn ghost" id="d-save">${icon("bookmark")} ${saved ? "Saved" : "Save"}</button>
      </div>
      <div id="d-match-panel"></div>
      <div class="description-wrap"><h4>About the role</h4><div class="description">${esc(j.description)}</div></div>`;
    $("#d-match").onclick = () => analyseMatch(j.id);
    $("#d-save").onclick = async () => {
      const on = state.saved.has(j.id);
      await api("/api/feedback", { method: "POST", json: { job_id: j.id, action: on ? "clear" : "save" } });
      on ? state.saved.delete(j.id) : state.saved.add(j.id);
      $("#d-save").innerHTML = `${icon("bookmark")} ${on ? "Save" : "Saved"}`;
      updateSavedCount(true);
      $$(`.job[data-id="${CSS.escape(j.id)}"] .save`).forEach((b) => b.classList.toggle("on", !on));
    };
  } catch (e) { body.innerHTML = `<div class="empty error">${esc(e.message)}</div>`; }
}

async function analyseMatch(id) {
  const panel = $("#d-match-panel"), btn = $("#d-match");
  btn.disabled = true;
  panel.innerHTML = `<div class="skeleton" style="height:150px;margin-bottom:20px"></div>`;
  try {
    const m = await api(`/api/jobs/${encodeURIComponent(id)}/match`, { method: "POST" });
    panel.innerHTML = `
      <div class="match-panel">
        <span class="verdict ${esc(m.verdict)}">${icon(m.verdict === "weak" ? "minus" : "check")} ${esc(m.verdict)} match</span>
        <p>${esc(m.summary)}</p>
        ${m.matching_skills.length ? `<h4>You have</h4>${chips(m.matching_skills, "good")}` : ""}
        ${m.missing_skills.length ? `<h4>Gaps to close</h4>${chips(m.missing_skills, "bad", m.matching_skills.length)}` : ""}
      </div>`;
  } catch (e) {
    panel.innerHTML = "";
    toast(e.message, { error: true });
  } finally { btn.disabled = false; }
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").hidden = true;
}
$("#drawer-close").addEventListener("click", closeDrawer);
$("#scrim").addEventListener("click", closeDrawer);
addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// ---------------------------------------------------------------- chat
const TOOL_LABELS = {
  search_jobs: "Searched jobs", recommend_jobs: "Ranked jobs for you", get_job_details: "Read the posting",
  explain_match: "Compared with your resume", save_job: "Saved a job", dismiss_job: "Hid a job",
  list_saved_jobs: "Checked saved jobs", update_preferences: "Updated memory", draft_application_answer: "Drafted an answer",
};

function addMessage(role, text, trace) {
  $("#welcome")?.remove();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = role === "assistant" ? md(text) : esc(text);
  wrap.append(bubble);
  if (trace?.length) {
    const d = document.createElement("details");
    d.className = "trace";
    const total = trace.reduce((s, t) => s + t.ms, 0);
    d.innerHTML = `<summary>${icon("bolt")} ${trace.length} step${trace.length > 1 ? "s" : ""} · ${total} ms</summary>
      <ol class="timeline">${trace.map((t, i) => `
        <li class="${t.ok ? "" : "bad"}" style="animation-delay:${i * 70}ms">
          <span class="dot">${icon(t.ok ? "check" : "x")}</span>
          <div><div>${esc(TOOL_LABELS[t.tool] || t.tool)} <span class="meta">· ${esc(t.result)} · ${t.ms} ms</span></div>
          <code>${esc(t.tool)}(${esc(JSON.stringify(t.args))})</code></div>
        </li>`).join("")}</ol>`;
    wrap.append(d);
  }
  $("#messages").append(wrap);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return wrap;
}

function typing() {
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  wrap.innerHTML = `<div class="bubble typing"><span></span><span></span><span></span></div>`;
  $("#messages").append(wrap);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return wrap;
}

async function sendChat(text) {
  if (!text) return;
  addMessage("user", text);
  const pending = typing();
  const orb = $(".chat-head .orb");
  orb.classList.add("thinking");
  $("#agent-status").textContent = "Thinking…";
  $("#chat-msg").disabled = true;
  try {
    const d = await api("/api/chat", { method: "POST", json: { message: text } });
    pending.remove();
    addMessage("assistant", d.reply, d.trace);
    state.prefs = d.preferences; renderPrefs();
    const tools = new Set(d.trace.map((t) => t.tool));
    if (tools.has("update_preferences")) { toast("Preference saved to memory"); if (state.tab === "feed") loadFeed(); }
    if (tools.has("save_job") || tools.has("dismiss_job")) loadSaved(state.tab === "saved");
  } catch (e) {
    pending.remove();
    addMessage("assistant", `⚠️ ${e.message}`);
  } finally {
    orb.classList.remove("thinking");
    $("#agent-status").textContent = "Searches, compares and remembers";
    $("#chat-msg").disabled = false;
    $("#chat-msg").focus();
  }
}

async function loadHistory() {
  try {
    const { messages } = await api("/api/chat");
    messages.forEach((m) => addMessage(m.role, m.content));
  } catch { /* fresh user */ }
}

function openChat(open) {
  $("#chat").classList.toggle("open", open);
  $("#open-chat").classList.toggle("hidden", open);
  if (open) setTimeout(() => $("#chat-msg").focus(), 300);
}

// ---------------------------------------------------------------- stats
async function loadStats() {
  try {
    const { jobs, last_ingest } = await api("/api/stats");
    countUp($("#stat-jobs"), jobs.active);
    countUp($("#stat-companies"), jobs.companies);
    const when = last_ingest?.finished_at ? ago(last_ingest.finished_at) : null;
    $("#stat-updated").textContent = when ? `updated ${when.text}` : "not synced yet";
  } catch { $("#stat-updated").textContent = "index offline"; }
}

// ---------------------------------------------------------------- wire up
$$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
$$("#method .seg").forEach((s) => s.addEventListener("click", () => {
  $$("#method .seg").forEach((x) => { x.classList.toggle("active", x === s); x.setAttribute("aria-checked", x === s); });
  state.method = s.dataset.value;
  syncIndicators();
  if ($("#q").value.trim()) runSearch();
}));

const dz = $("#dropzone");
$("#resume-file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const form = new FormData(); form.append("file", f);
  uploadResume(form);
  e.target.value = "";
});
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (!f) return;
  if (f.type !== "application/pdf") { toast("Please drop a PDF file", { error: true }); return; }
  const form = new FormData(); form.append("file", f);
  uploadResume(form);
});
$("#resume-text-btn").addEventListener("click", () => {
  const form = new FormData(); form.append("text", $("#resume-text").value);
  uploadResume(form);
});
$("#open-upload").addEventListener("click", () => { $("#onboarding").scrollIntoView({ behavior: "smooth" }); $("#resume-file").click(); });
$("#pref-india").addEventListener("change", savePrefs);
$("#pref-remote").addEventListener("change", savePrefs);
$("#refresh-feed").addEventListener("click", loadFeed);
$("#search-form").addEventListener("submit", runSearch);
$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("#chat-msg").value.trim();
  $("#chat-msg").value = "";
  sendChat(text);
});
$$("#suggestions .chip-btn").forEach((b) => b.addEventListener("click", () => sendChat(b.textContent)));
$("#clear-chat").addEventListener("click", async () => {
  await api("/api/chat", { method: "DELETE" });
  $$("#messages .msg").forEach((m) => m.remove());
  toast("Conversation cleared");
});
$("#open-chat").addEventListener("click", () => openChat(true));
$("#close-chat").addEventListener("click", () => openChat(false));

(async () => {
  loadStats();
  syncIndicators();
  document.fonts?.ready.then(syncIndicators);
  try {
    await ensureUser();
    renderProfile();
    await loadSaved(false);
    loadFeed();
    loadHistory();
  } catch (e) { toast(e.message, { error: true, ms: 6000 }); }
})();
