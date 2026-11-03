/* RIO — Personal AI Assistant · premium command-center shell
 * History-API router + honest data: every value on screen comes from a real
 * backend endpoint or is the real client clock. Nothing simulated.
 *
 * Structure:
 *   1. routes · sidebar · compact mode
 *   2. router + page dispatch
 *   3. Activity page (real /api/rio/system/activity)
 *   4. Settings page (real /api/settings/*)
 *   5. Notifications popover (real /api/rio/system/notifications)
 *   6. Home command center (RIOCore + one-shot side panels)
 *   7. Health polling + bottom status bar
 *   8. boot
 */
"use strict";

/* ── 1. Route table ──────────────────────────────────────────────────── */
const ROUTES = [
  { path: "/home",     icon: "home",       label: "Home",      title: "Home",
    blurb: "Command center: live core status, health, models and activity.",
    kind: "home" },
  { path: "/news",     icon: "newspaper",  label: "News",      title: "News & Impact",
    blurb: "Sourced market briefing: outlook, flows, sectors, results and levels.",
    kind: "news" },
  { path: "/chat",     icon: "chat",       label: "Chat",      title: "Chat",
    blurb: "Conversation with RIO over the live session WebSocket.",
    kind: "chat" },
  { path: "/agents",   icon: "bot",        label: "Agents",    title: "Agents",
    blurb: "Registry of routed agents and their task lanes.",
    kind: "agents" },
  { path: "/workflows", icon: "zap",       label: "Workflows", title: "Workflows",
    blurb: "One page per agent: its pipeline, tools and live status.",
    kind: "workflows" },
  { path: "/ops",      icon: "activity",   label: "Live Ops",  title: "Live Ops",
    blurb: "Every agent and every task, measured live: status, timings, runs.",
    kind: "ops" },
  { path: "/brain",    icon: "brain",      label: "Brain",     title: "Brain",
    blurb: "Model routing: local Qwen, Gemini, FreeLLMAPI and OpenRouter rungs.",
    kind: "brain" },
  { path: "/memory",   icon: "database",   label: "Memory",    title: "Memory",
    blurb: "Long-term memory, conversation journal and reflexion lessons.",
    kind: "memory" },
  { path: "/files",    icon: "folder",     label: "Files",     title: "Files",
    blurb: "Uploads, downloads and documents shared with RIO.",
    kind: "files" },
  { path: "/hermes",   icon: "radio",      label: "Hermes",    title: "Hermes",
    blurb: "Telegram / WhatsApp gateway: sessions, hooks and message flow.",
    kind: "hermes" },
  { path: "/missions", icon: "target",     label: "Missions",  title: "Missions",
    blurb: "Scheduled and autonomous missions (autonomy engine).",
    kind: "missions" },
  { path: "/tools",   icon: "sparkles",   label: "Tools",    title: "Tools & Skills",
    blurb: "Managed GitHub repositories, skills and their job history.",
    kind: "tools" },
  { path: "/video",   icon: "film",       label: "Video",    title: "Video Studio",
    blurb: "Create documentaries, Shorts and cinematic renders; live jobs and library.",
    kind: "video" },
  { path: "/activity", icon: "activity",   label: "Activity",  title: "Activity",
    blurb: "Unified timeline: what ran, what failed, what was approved.",
    kind: "activity" },
  { path: "/system",   icon: "monitor",    label: "System",    title: "System",
    blurb: "Host resources, services and audit surface.",
    kind: "system" },
  { path: "/settings", icon: "settings",   label: "Settings",  title: "Settings",
    blurb: "Pairing, channels and preferences.",
    kind: "settings" },
];
const ROUTE_BY_PATH = Object.fromEntries(ROUTES.map(r => [r.path, r]));

/* ── Elements ────────────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
const navEl = $("nav"), viewEl = $("view"), clockEl = $("clock"),
      sysDot = $("sys-dot"), sysLabel = $("sys-label"),
      stackEl = $("status-stack"), sidebar = $("sidebar");

/* ── Sidebar ─────────────────────────────────────────────────────────── */
function renderNav(activePath) {
  navEl.replaceChildren(...ROUTES.map(r => {
    const a = document.createElement("a");
    a.className = "nav-item" + (r.path === activePath ? " active" : "");
    a.href = r.path;
    a.title = r.blurb || r.label;
    a.innerHTML = `<span class="ico">${RIOIcons.get(r.icon)}</span>` +
                  `<span class="nav-label">${r.label}</span>`;
    a.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      navigate(r.path);
    });
    return a;
  }));
}

/* compact (icon-rail) sidebar — persisted preference */
function loadCompact() {
  try { return localStorage.getItem("rio_sidebar_compact") === "1"; }
  catch (_) { return false; }
}
function applyCompact(on) {
  sidebar.classList.toggle("compact", on);
  const btn = $("compact-btn");
  if (btn) btn.title = on ? "Expand sidebar" : "Collapse sidebar";
}
$("compact-btn").innerHTML = RIOIcons.get("panel-left");
$("compact-btn").addEventListener("click", () => {
  const on = !sidebar.classList.contains("compact");
  applyCompact(on);
  try { localStorage.setItem("rio_sidebar_compact", on ? "1" : "0"); } catch (_) {}
});

/* ── 2. Router (each of the 12 paths is a real server route) ─────────── */
function navigate(path, { replace = false } = {}) {
  if (location.pathname === path) { render(path); return; }
  history[replace ? "replaceState" : "pushState"](null, "", path);
  render(path);
}

function render(path) {
  const route = ROUTE_BY_PATH[path];
  if (!route) {
    // Deep link: /agents/<key> — a per-agent workflow page. The slug is not
    // validated here; RIOWorkflows renders an honest "no such agent" view.
    if (path.startsWith("/agents/") && path.length > "/agents/".length) {
      const slug = decodeURIComponent(path.slice("/agents/".length));
      document.title = `${slug} · workflow · RIO`;
      renderNav("/workflows");
      const wfPage = document.createElement("div");
      wfPage.className = "page";
      wfPage.append(window.RIOWorkflows && window.RIOWorkflows.build
        ? window.RIOWorkflows.build(slug)
        : Object.assign(document.createElement("div"),
            { className: "empty", textContent: "Workflows module failed to load — reload the page." }));
      viewEl.replaceChildren(wfPage);
      viewEl.scrollTop = 0;
      viewEl.focus({ preventScroll: true });
      return;
    }
    renderNotFound(path);
    return;
  }
  document.title = `${route.title} · RIO`;
  renderNav(route.path);

  const page = document.createElement("div");
  page.className = "page";
  const MODULES = {
    news: "RIONews", chat: "RIOChat", agents: "RIOAgents", brain: "RIOBrain",
    memory: "RIOMemory", files: "RIOFiles", hermes: "RIOHermes",
    missions: "RIOMissions", system: "RIOSystem", tools: "RIOTools", video: "RIOVideo",
    workflows: "RIOWorkflows", ops: "RIOOps",
  };
  if (route.kind === "home") {
    page.append(buildHome());
  } else if (MODULES[route.kind]) {
    const mod = window[MODULES[route.kind]];
    if (mod && typeof mod.build === "function") page.append(mod.build());
    else {
      const box = document.createElement("div");
      box.className = "empty";
      box.textContent = `${route.title} module failed to load — reload the page.`;
      page.append(box);
    }
  } else if (route.kind === "activity") {
    page.append(buildActivity());
  } else if (route.kind === "settings") {
    page.append(buildSettings());
  } else {
    const h1 = document.createElement("h1");
    h1.textContent = route.title;
    const sub = document.createElement("p");
    sub.className = "sub";
    sub.textContent = route.blurb;
    page.append(h1, sub, buildHonestEmpty(route));
  }

  viewEl.replaceChildren(page);
  viewEl.scrollTop = 0;
  viewEl.focus({ preventScroll: true });
}

function renderNotFound(path) {
  document.title = "Not found · RIO";
  renderNav("");
  const page = document.createElement("div");
  page.className = "page";
  page.innerHTML = `<h1>404</h1><p class="sub">No route <code>${escapeHtml(path)}</code>.</p>`;
  const box = document.createElement("div");
  box.className = "empty";
  box.innerHTML = `Try one of the sections in the sidebar.`;
  page.append(box);
  viewEl.replaceChildren(page);
}

function buildHonestEmpty(route) {
  const box = document.createElement("div");
  box.className = "empty";
  box.innerHTML = `<strong>${escapeHtml(route.title)}</strong> — nothing wired here yet. ` +
    `This panel will render live data from the RIO backend once that phase ships; ` +
    `it deliberately shows no placeholder numbers.`;
  return box;
}

/* ── 3. Activity page — REAL unified timeline ─────────────────────────── */
let actCategory = "all";

function buildActivity() {
  const frag = document.createDocumentFragment();
  const wrap = document.createElement("div");
  wrap.className = "act-wrap";
  wrap.innerHTML = `
    <h1>Activity & Logs</h1>
    <p class="sub">Unified journal timeline — AI sessions, agents, Hermes, files,
    trading and system events. Application logs only; conversation history
    lives in Memory. Sensitive values are never included.</p>
    <div class="act-toolbar">
      <div class="act-filters" id="act-filters"></div>
      <span class="act-count" id="act-count"></span>
      <button class="btn" id="act-refresh">${RIOIcons.get("refresh")} Refresh</button>
    </div>
    <div class="act-list" id="act-list">
      <div class="empty"><span class="skel">loading the journal timeline…</span></div>
    </div>`;
  frag.append(wrap);

  queueMicrotask(() => {
    $("act-refresh").addEventListener("click", () => loadActivity(actCategory));
    loadActivity(actCategory);
  });
  return frag;
}

const ACT_SEV = (it) => it.error ? "error" : "information";

async function loadActivity(category) {
  actCategory = category;
  const list = $("act-list"), filters = $("act-filters"), count = $("act-count");
  if (!list) return;
  try {
    const res = await apiSend(`/api/rio/system/activity?category=${encodeURIComponent(category)}&limit=80`);
    if (res.status !== 200 || !res.body || !res.body.ok) {
      list.innerHTML = `<div class="empty">Journal unavailable — ` +
        `${escapeHtml(failMsg("activity", res))}</div>`;
      return;
    }
    const d = res.body;
    if (filters) {
      filters.replaceChildren(...d.categories.map(c => {
        const b = document.createElement("button");
        b.className = "act-filter" + (c === actCategory ? " on" : "");
        const n = d.counts[c] || 0;
        b.textContent = c === "all" ? `All (${n})` : `${c[0].toUpperCase() + c.slice(1)} (${n})`;
        b.addEventListener("click", () => loadActivity(c));
        return b;
      }));
    }
    if (count) count.textContent = `${d.items.length} shown · journal ts ${d.ts}`;
    if (!d.items.length) {
      list.innerHTML = `<div class="empty">No ${escapeHtml(category)} entries in the journals yet.</div>`;
      return;
    }
    list.replaceChildren(...d.items.map(it => {
      const row = document.createElement("div");
      row.className = "act-row";
      const when = fmtClockAge(it.ts, it.age_s);
      row.innerHTML = `<span class="act-when">${escapeHtml(when)}</span>` +
        `<span class="act-src">${escapeHtml(it.category)}</span>` +
        `<span class="act-sev s-${ACT_SEV(it)}">${it.error ? "ERROR" : "EVENT"}</span>` +
        `<span class="act-txt" title="${escapeHtml(it.text)}">${escapeHtml(it.text)}</span>`;
      return row;
    }));
  } catch (_) {
    list.innerHTML = `<div class="empty">Backend unreachable — the timeline cannot be read right now.</div>`;
  }
}

function fmtClockAge(ts, ageS) {
  const t = ts ? String(ts).replace("T", " ").slice(5, 19) : "";
  return t + (ageS != null ? ` · ${fmtAge(ageS)}` : "");
}

/* ── 4. Settings page — REAL preference surfaces ─────────────────────── */
function buildSettings() {
  const frag = document.createDocumentFragment();
  const wrap = document.createElement("div");
  wrap.className = "set-wrap";
  wrap.innerHTML = `
    <h1>Settings</h1>
    <p class="sub">Live preference surfaces backed by the running backend.
    Nothing here is stored in the browser except your pairing token.</p>

    <section class="set-card">
      <h2>${RIOIcons.get("key")} Pairing & access</h2>
      <p class="set-desc">Static dashboard token and this browser's session.</p>
      <div class="set-body" id="set-access"><span class="skel">loading…</span></div>
    </section>

    <section class="set-card">
      <h2>${RIOIcons.get("mic")} Push-to-talk</h2>
      <p class="set-desc">Hold-the-hotkey voice capture. Toggling persists to the
      assistant's config; when the hook is live the change applies immediately.</p>
      <div class="set-body" id="set-ptt"><span class="skel">loading…</span></div>
    </section>

    <section class="set-card">
      <h2>${RIOIcons.get("zap")} Gemini client</h2>
      <p class="set-desc">Fallback ladder status and cooldown state (real probe).
      Keys are never displayed.</p>
      <div class="set-body" id="set-gemini"><span class="skel">loading…</span></div>
      <div class="set-actions">
        <button class="btn" id="set-gem-refresh">${RIOIcons.get("refresh")} Re-read key from disk</button>
        <button class="btn" id="set-gem-cool">${RIOIcons.get("rotate-ccw")} Clear 429 cooldowns</button>
      </div>
    </section>

    <section class="set-card">
      <h2>${RIOIcons.get("newspaper")} News API key</h2>
      <p class="set-desc">CurrentsAPI key used by the news tools. Stored
      server-side, always shown masked.</p>
      <div class="set-body" id="set-news"><span class="skel">loading…</span></div>
    </section>

    <section class="set-card">
      <h2>${RIOIcons.get("trending-up")} Delta Exchange India</h2>
      <p class="set-desc">Delta Exchange India API key + secret. Quotes,
      candles and funding work without them; saving marks the venue as
      configured. These credentials are used by the Trading page's
      <b>Live Execution</b> switch only after you arm it there with the
      explicit phrase and a read-only signed probe.</p>
      <div class="set-body" id="set-delta"><span class="skel">loading…</span></div>
    </section>

    <div class="feedback" id="set-feedback" role="status"></div>`;
  frag.append(wrap);

  queueMicrotask(() => wireSettings(wrap));
  return frag;
}

function setSay(msg, kind) {
  const fb = document.getElementById("set-feedback");
  if (!fb) return;
  fb.textContent = msg || "";
  fb.className = "feedback" + (kind ? " fb-" + kind : "");
}

function kvLine(k, v, cls) {
  const line = document.createElement("div");
  line.className = "set-line";
  line.innerHTML = `<span class="k">${escapeHtml(k)}</span>` +
    `<span class="v${cls ? " " + cls : ""}">${v}</span>`;
  return line;
}

async function wireSettings(root) {
  // — pairing & access —
  const access = root.querySelector("#set-access");
  apiSend("/api/settings/dashboard-token").then(res => {
    if (!access) return;
    access.replaceChildren();
    if (res.status === 200 && res.body) {
      access.append(kvLine("Static dashboard token",
        res.body.set ? `set (${escapeHtml(res.body.masked)})` : "not set",
        res.body.set ? "" : "fb-warn"));
    } else {
      access.append(kvLine("Static dashboard token", "unavailable", "fb-warn"));
    }
    const hasTok = !!token();
    access.append(kvLine("This browser session", hasTok ? "paired" : "not paired",
      hasTok ? "" : "fb-warn"));
    const row = document.createElement("div");
    row.className = "set-actions";
    row.innerHTML = `<a class="btn btn-primary" href="/login">${RIOIcons.get("key")} Pair a device / re-pair</a>`;
    access.append(row);
  });

  // — push-to-talk —
  const ptt = root.querySelector("#set-ptt");
  apiSend("/api/settings/push-to-talk").then(res => {
    if (!ptt) return;
    ptt.replaceChildren();
    if (res.status !== 200 || !res.body) {
      ptt.append(kvLine("State", "unavailable", "fb-warn"));
      return;
    }
    const line = document.createElement("div");
    line.className = "set-line";
    line.innerHTML = `<span class="k">Enabled${res.body.chord ? ` (${escapeHtml(res.body.chord)})` : ""}</span>`;
    const sw = document.createElement("button");
    sw.className = "sw" + (res.body.enabled ? " on" : "");
    sw.setAttribute("role", "switch");
    sw.setAttribute("aria-checked", String(!!res.body.enabled));
    sw.setAttribute("aria-label", "Toggle push-to-talk");
    sw.addEventListener("click", async () => {
      const want = !sw.classList.contains("on");
      sw.disabled = true;
      const r = await apiPost("/api/settings/push-to-talk", { enabled: want });
      sw.disabled = false;
      if (r.status === 200 && r.body && r.body.ok !== false) {
        sw.classList.toggle("on", want);
        sw.setAttribute("aria-checked", String(want));
        setSay(want ? "Push-to-talk enabled." : "Push-to-talk disabled.", "ok");
      } else {
        setSay(failMsg("push-to-talk", r), "err");
      }
    });
    line.append(sw);
    ptt.append(line);
    ptt.append(kvLine("Hotkey hook",
      res.body.live ? "live — changes apply now" : "not registered — applies after restart",
      res.body.live ? "" : "fb-warn"));
  });

  // — gemini client —
  const gem = root.querySelector("#set-gemini");
  const loadGem = async () => {
    if (!gem) return;
    const res = await apiSend("/api/settings/gemini-client");
    gem.replaceChildren();
    if (res.status !== 200 || !res.body || !res.body.ok) {
      gem.append(kvLine("Status", failMsg("gemini", res), "fb-err"));
      return;
    }
    const st = res.body;
    for (const [k, v] of Object.entries(st)) {
      if (k === "ok") continue;
      if (v == null || typeof v === "object") continue;
      gem.append(kvLine(k.replace(/_/g, " "), escapeHtml(String(v))));
    }
  };
  loadGem();
  root.querySelector("#set-gem-refresh")?.addEventListener("click", async () => {
    const r = await apiPost("/api/settings/gemini-client", { action: "refresh-key" });
    setSay(r.status === 200 && r.body?.ok ? "Key re-read from disk." : failMsg("gemini", r),
           r.status === 200 ? "ok" : "err");
    loadGem();
  });
  root.querySelector("#set-gem-cool")?.addEventListener("click", async () => {
    const r = await apiPost("/api/settings/gemini-client", { action: "clear-cooldowns" });
    setSay(r.status === 200 && r.body?.ok ? "Cooldowns cleared." : failMsg("gemini", r),
           r.status === 200 ? "ok" : "err");
    loadGem();
  });

  // — news api —
  const news = root.querySelector("#set-news");
  apiSend("/api/settings/newsapi").then(res => {
    if (!news) return;
    news.replaceChildren();
    if (res.status !== 200 || !res.body) {
      news.append(kvLine("Key", "unavailable", "fb-warn"));
      return;
    }
    news.append(kvLine("Current key",
      res.body.set ? `set (${escapeHtml(res.body.masked)})` : "not set",
      res.body.set ? "" : "fb-warn"));
    const row = document.createElement("div");
    row.className = "set-actions";
    row.innerHTML = `
      <input class="set-input" id="news-key" type="password" placeholder="paste a new key"
             autocomplete="off" style="flex:1 1 220px">
      <button class="btn" id="news-test">Test</button>
      <button class="btn btn-primary" id="news-save">Save</button>`;
    news.append(row);
    const keyIn = row.querySelector("#news-key");
    row.querySelector("#news-test").addEventListener("click", async () => {
      const key = keyIn.value.trim();
      if (!key) { setSay("Paste a key first.", "warn"); return; }
      const r = await apiPost("/api/settings/newsapi/test", { key });
      setSay(r.body?.ok ? `Valid — ${r.body.message}` : (r.body?.error || "invalid"),
             r.body?.ok ? "ok" : "err");
    });
    row.querySelector("#news-save").addEventListener("click", async () => {
      const key = keyIn.value.trim();
      if (!key) { setSay("Paste a key first.", "warn"); return; }
      const r = await apiPost("/api/settings/newsapi", { key });
      if (r.status === 200 && r.body?.ok) {
        setSay(r.body.message || "Key saved.", "ok");
        keyIn.value = "";
        buildSettingsRefreshNews(news);
      } else {
        setSay(r.body?.error || failMsg("newsapi", r), "err");
      }
    });
  });

  wireDeltaSettings(root);
}

async function buildSettingsRefreshNews(news) {
  const res = await apiSend("/api/settings/newsapi");
  if (res.status === 200 && res.body && news) {
    const line = news.querySelector(".set-line .v");
    if (line) {
      line.textContent = res.body.set ? `set (${res.body.masked})` : "not set";
      line.className = "v" + (res.body.set ? "" : " fb-warn");
    }
  }
}

/* Delta Exchange India — venue credentials. Public market data stays
   unsigned; the same pair authenticates the Trading page's explicitly armed
   live path. The backend stores key+secret in config/api_keys.json and
   reports them masked; env vars (DELTA_API_KEY / DELTA_API_SECRET) override
   the file, so the card names which source is actually in force. */
function wireDeltaSettings(root) {
  const box = root.querySelector("#set-delta");
  if (!box) return;

  // The form is built ONCE and kept, independent of the status probe. If the
  // backend route is missing (e.g. the app was started before this feature
  // shipped), the user still sees the fields and the error instead of an
  // empty card — that is exactly the "there is no field" failure mode.
  box.replaceChildren();

  const status = document.createElement("div");
  status.className = "set-body";
  status.append(kvLine("Status", "reading…", ""));
  box.append(status);

  const row = document.createElement("div");
  row.className = "set-actions";
  row.innerHTML = `
    <input class="set-input" id="delta-key" type="password" placeholder="API key"
           autocomplete="off" spellcheck="false" style="flex:1 1 200px">
    <input class="set-input" id="delta-secret" type="password" placeholder="API secret"
           autocomplete="off" spellcheck="false" style="flex:1 1 200px">
    <button class="btn btn-primary" id="delta-save">Save</button>
    <button class="btn" id="delta-check">Check venue</button>`;
  box.append(row);

  const clearRow = document.createElement("div");
  clearRow.className = "set-actions";
  clearRow.hidden = true;
  clearRow.innerHTML = `<button class="btn" id="delta-clear">` +
    `${RIOIcons.get("trash")} Clear stored credentials</button>`;
  box.append(clearRow);

  const paint = (res) => {
    status.replaceChildren();
    if (res.status !== 200 || !res.body || res.body.ok === false) {
      status.append(kvLine("Status", failMsg("delta settings", res), "fb-err"));
      if (res.status === 404) {
        status.append(kvLine("Fix",
          "the running app predates this endpoint — restart JARVIS, then reload this page",
          "fb-warn"));
      }
      clearRow.hidden = true;
      return;
    }
    const st = res.body;
    status.append(kvLine("Stored API key",
      st.key_set ? `set (${escapeHtml(st.masked_key)})` : "not set",
      st.key_set ? "" : "fb-warn"));
    status.append(kvLine("Stored API secret",
      st.secret_set ? `set (${escapeHtml(st.masked_secret)})` : "not set",
      st.secret_set ? "" : "fb-warn"));
    status.append(kvLine("Source",
      st.source === "env" ? "environment (DELTA_API_KEY / DELTA_API_SECRET)"
        : st.source === "settings" ? "saved here"
        : "none — public data works without credentials",
      st.source === "none" ? "fb-warn" : ""));
    status.append(kvLine("Venue mode",
      escapeHtml(st.mode || "public data + paper only")));
    clearRow.hidden = !st.set;
  };

  const load = async () => paint(await apiSend("/api/settings/delta"));

  row.querySelector("#delta-save").addEventListener("click", async () => {
    const key = row.querySelector("#delta-key").value.trim();
    const secret = row.querySelector("#delta-secret").value.trim();
    if (!key || !secret) {
      setSay("Both an API key and an API secret are required.", "warn");
      return;
    }
    const r = await apiPost("/api/settings/delta", { key, secret });
    if (r.status === 200 && r.body?.ok) {
      setSay(r.body.message || "Delta credentials saved.", "ok");
      row.querySelector("#delta-key").value = "";
      row.querySelector("#delta-secret").value = "";
      load();
    } else {
      setSay(r.body?.error || failMsg("delta", r), "err");
    }
  });

  row.querySelector("#delta-check").addEventListener("click", async () => {
    const r = await apiSend("/api/rio/trading/delta/status");
    const st2 = r.body || {};
    if (r.status === 200 && st2.ok) {
      setSay(`Delta ${st2.status} — ${st2.products || 0} live products` +
        ` (${st2.perpetuals || 0} perps, ${st2.spot || 0} spot).`, "ok");
    } else {
      setSay(failMsg("delta venue", r) +
        (st2.products_error ? ` — ${st2.products_error}` : ""), "err");
    }
  });

  clearRow.querySelector("#delta-clear").addEventListener("click", async () => {
    const r = await apiPost("/api/settings/delta", { clear: true });
    if (r.status === 200 && r.body?.ok) {
      setSay(r.body.message || "Stored credentials cleared.", "ok");
      load();
    } else {
      setSay(r.body?.error || failMsg("delta", r), "err");
    }
  });

  load();
}

/* ── 5. Notifications popover — REAL derived notification centre ─────── */
let notifOpen = false;
let notifLoaded = false;

async function fetchNotifications() {
  const res = await apiSend("/api/rio/system/notifications?limit=20");
  if (res.status !== 200 || !res.body || !res.body.ok) return null;
  notifLoaded = true;
  paintNotifCount(res.body);
  return res.body;
}

function paintNotifCount(d) {
  const n = (d.items || []).length;
  const badge = $("notif-count");
  if (!badge) return;
  badge.hidden = n === 0;
  badge.textContent = String(n);
}

async function toggleNotifPop() {
  if (notifOpen) { closeNotifPop(); return; }
  notifOpen = true;
  const pop = document.createElement("div");
  pop.className = "notif-pop";
  pop.id = "notif-pop";
  pop.innerHTML = `<div class="notif-pop-head">${RIOIcons.get("bell")} Notifications</div>
    <div class="empty"><span class="skel">reading the journals…</span></div>`;
  document.body.append(pop);
  const d = await fetchNotifications();
  if (!notifOpen) return;                    // closed while fetching
  const body = pop.querySelector(".empty, div:last-child");
  if (!d) {
    pop.innerHTML = `<div class="notif-pop-head">${RIOIcons.get("bell")} Notifications</div>
      <div class="empty">Notification centre unavailable right now.</div>`;
    return;
  }
  const items = d.items || [];
  const list = document.createElement("div");
  if (!items.length) {
    list.innerHTML = `<div class="empty">Nothing notable in the journals —
      no warnings, failures or pending approvals.</div>`;
  } else {
    list.className = "cmd-list";
    list.replaceChildren(...items.map(it => {
      const row = document.createElement("div");
      row.className = "notif-item";
      const sev = String(it.severity || "Information").toLowerCase();
      row.innerHTML = `<span class="notif-sev s-${escapeHtml(sev)}"></span>` +
        `<span><span class="notif-title">${escapeHtml(it.title)}</span>` +
        `<div class="notif-meta">${escapeHtml(it.domain)} · ${escapeHtml(fmtAge(it.age_s))}</div>` +
        (it.detail ? `<div class="notif-detail">${escapeHtml(it.detail)}</div>` : "") +
        `</span>`;
      row.title = it.detail || it.title;
      return row;
    }));
  }
  pop.replaceChildren(pop.querySelector(".notif-pop-head"), list);
}

function closeNotifPop() {
  notifOpen = false;
  $("notif-pop")?.remove();
}
document.addEventListener("click", (e) => {
  if (!notifOpen) return;
  const pop = $("notif-pop");
  if (pop && (pop.contains(e.target) || $("notif-btn").contains(e.target))) return;
  closeNotifPop();
});

/* ── toast (top-level feedback; replaces the old alertify box) ───────── */
function toast(msg, kind) {
  const t = document.createElement("div");
  t.className = "toast" + (kind ? ` t-${kind}` : "");
  const icon = kind === "ok" ? "check" : kind === "err" ? "alert-triangle" : "info";
  t.innerHTML = RIOIcons.get(icon) + `<span>${escapeHtml(msg)}</span>`;
  document.body.append(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; }, 2400);
  setTimeout(() => t.remove(), 2800);
}

/* ── 6. Home — the command center ─────────────────────────────────────── */
let lastHome = null;
let lastAgents = null;

function buildHome() {
  const page = document.createDocumentFragment();

  // greeting (real clock)
  const now = new Date();
  const hour = now.getHours();
  const part = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const greet = document.createElement("h1");
  greet.textContent = `${part}, rajneesh`;
  const dateLine = document.createElement("p");
  dateLine.className = "sub";
  dateLine.textContent = now.toLocaleDateString(undefined, {
    weekday: "long", year: "numeric", month: "long", day: "numeric" });
  page.append(greet, dateLine);

  const grid = document.createElement("div");
  grid.className = "cmd-grid";

  // ── LEFT column: activity · agents · conversations · knowledge ──
  const left = document.createElement("div");
  left.className = "cmd-col cmd-left";
  left.innerHTML = `
    <section class="card" id="card-assistant">
      <h2>${RIOIcons.get("sparkles")} Assistant</h2>
      <div class="doing-line" id="doing-line">…</div>
      <div class="doing-detail" id="doing-detail">waiting for first check</div>
      <div class="tasks-sum" id="tasks-summary" style="margin-top:8px">N/A — not loaded</div>
    </section>
    <section class="card">
      <h2>${RIOIcons.get("bot")} Agent distribution</h2>
      <div id="agents-mini"><span class="skel">loading registry…</span></div>
    </section>
    <section class="card">
      <h2>${RIOIcons.get("chat")} Recent conversations</h2>
      <div class="cmd-list" id="convs-mini"><span class="skel">loading journal…</span></div>
    </section>
    <section class="card">
      <h2>${RIOIcons.get("file-text")} Knowledge & files</h2>
      <div class="cmd-list" id="files-mini"><span class="skel">loading index…</span></div>
    </section>`;
  grid.append(left);

  // ── CENTER column: AI core · services · metrics · quick actions ──
  const center = document.createElement("div");
  center.className = "cmd-col cmd-center";
  const hero = document.createElement("section");
  hero.className = "cmd-hero";
  const coreWrap = document.createElement("div");
  coreWrap.className = "cmd-core";
  let face = null, core = null;
  if (window.RIOFace) {
    face = RIOFace.create({});
    window.__rioRegisterHomeFace(face);   // coreSet() routes states into it
    face.el.addEventListener("facestate", (e) => {
      const line = document.getElementById("core-label");
      const det = document.getElementById("core-detail");
      if (line) line.textContent = e.detail.label;
      if (det) det.textContent = e.detail.detail;
    });
    coreWrap.append(face.el);
  } else if (window.RIOCore) {
    core = RIOCore.create({});            // legacy fallback (face missing)
    window.__rioRegisterHomeCore(core);
    core.el.addEventListener("corestate", (e) => {
      const line = document.getElementById("core-label");
      const det = document.getElementById("core-detail");
      if (line) line.textContent = e.detail.label;
      if (det) det.textContent = e.detail.detail;
    });
    coreWrap.append(core.el);
  } else {
    coreWrap.innerHTML = `<div class="orb" id="rio-orb"><span class="orb-core">R</span></div>`;
  }
  const heroRest = document.createElement("div");
  heroRest.innerHTML = `
    <div class="cmd-status-line" id="core-label">Checking core…</div>
    <div class="cmd-status-detail" id="core-detail">first health probe in flight</div>
    <div class="cmd-input-row">
      <input id="cmd-input" type="text" autocomplete="off"
             placeholder="Ask RIO anything…" aria-label="Ask RIO anything">
      <button class="btn btn-primary cmd-send" id="btn-send" title="Send">
        ${RIOIcons.get("send")}</button>
    </div>
    <div class="cmd-actions">
      <button class="btn btn-mute" id="btn-mute" title="Mute or unmute the assistant's microphone">
        ${RIOIcons.get("mic-off")} <span id="btn-mute-label">Mic…</span>
      </button>
      <button class="btn" id="btn-voice">${RIOIcons.get("mic")} Voice</button>
      <button class="btn" id="btn-upload">${RIOIcons.get("paperclip")} Upload</button>
      <button class="btn" id="btn-mission">${RIOIcons.get("target")} Start Mission</button>
      <input type="file" id="file-input" hidden>
    </div>
    <div class="mission-form" id="mission-form" hidden>
      <input id="mission-name" placeholder="mission name (e.g. morning-scan)" autocomplete="off">
      <input id="mission-what" placeholder="what should it do?" autocomplete="off">
      <button class="btn btn-primary" id="mission-create">Create</button>
      <button class="btn" id="mission-cancel">Cancel</button>
    </div>
    <div class="cmd-feedback feedback" id="cmd-feedback" role="status"></div>`;
  hero.append(coreWrap, heroRest);

  // RGB palette button (top-right corner of the hero) — custom face colors
  const palBtn = document.createElement("button");
  palBtn.className = "btn btn-ghost face-palette";
  palBtn.id = "face-rgb-btn";
  palBtn.title = "Face glow colors";
  palBtn.setAttribute("aria-label", "Face glow colors");
  palBtn.innerHTML = RIOIcons.get("palette");
  hero.append(palBtn);
  center.append(hero);

  const svc = document.createElement("section");
  svc.className = "svc-strip";
  svc.id = "svc-strip";
  svc.innerHTML = ["backend", "qwen", "gemini", "rag", "hermes"].map(k =>
    `<span class="chip" data-svc="${k}"><i class="dot dot-unknown"></i>` +
    `<b>${k === "backend" ? "RIO" : k[0].toUpperCase() + k.slice(1)}</b>` +
    `<span class="chip-state">…</span></span>`).join("");
  center.append(svc);

  const metrics = document.createElement("section");
  metrics.className = "card";
  metrics.id = "metrics";
  metrics.innerHTML = `<h2>${RIOIcons.get("gauge")} System status</h2>
    <div class="metrics-grid">${
      ["cpu", "ram", "gpu", "disk", "net"].map(k =>
        `<div class="metric" data-metric="${k}"><div class="m-label">${k.toUpperCase()}</div>
         <div class="m-value">N/A</div><div class="m-meta">not loaded</div></div>`).join("")
    }</div>`;
  center.append(metrics);

  const quick = document.createElement("section");
  quick.className = "card";
  quick.innerHTML = `<h2>${RIOIcons.get("zap")} Quick actions</h2><div class="quick-row">
    <button class="btn" data-quick="health">${RIOIcons.get("refresh")} Re-check health</button>
    <button class="btn" data-quick="chat">${RIOIcons.get("chat")} Open chat</button>
    <button class="btn" data-quick="missions">${RIOIcons.get("target")} Missions</button>
    <button class="btn" data-quick="system">${RIOIcons.get("monitor")} System</button>
    <button class="btn" data-quick="memory">${RIOIcons.get("database")} Memory</button>
    <button class="btn" data-quick="login">${RIOIcons.get("key")} Pair device</button>
    <button class="btn" data-quick="upload">${RIOIcons.get("upload")} Upload file</button>
  </div>`;
  center.append(quick);
  grid.append(center);

  // ── RIGHT column: missions · models · channels · signals · notifications ──
  const right = document.createElement("div");
  right.className = "cmd-col cmd-right";
  right.innerHTML = `
    <section class="card">
      <h2>${RIOIcons.get("calendar")} Missions & tasks</h2>
      <div class="cmd-list" id="tasks-list"><span class="skel">loading engine…</span></div>
    </section>
    <section class="card">
      <h2>${RIOIcons.get("cpu")} Models & providers</h2>
      <div class="cmd-list" id="models-list"><span class="skel">probing…</span></div>
    </section>
    <section class="card">
      <h2>${RIOIcons.get("radio")} Channels</h2>
      <div class="cmd-list" id="channels-mini"><span class="skel">probing gateways…</span></div>
    </section>
    <section class="card">
      <h2>${RIOIcons.get("shield")} Signals</h2>
      <div class="cmd-list" id="signals-mini"><span class="skel">reading state…</span></div>
    </section>
    <section class="card">
      <h2>${RIOIcons.get("bell")} Notifications</h2>
      <div class="cmd-list" id="notif-mini"><span class="skel">reading journals…</span></div>
    </section>`;

  // ── HOME: camera + media cards (live; pollers die with the page) ──
  const homeApi = async (path, opts = {}) => {
    const headers = Object.assign({}, opts.headers || {});
    const tok = token();
    if (tok) headers["Authorization"] = `Bearer ${tok}`;
    const r = await fetch(path, Object.assign({}, opts, { headers, cache: "no-store" }));
    let body = null;
    try { body = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error((body && (body.error || body.detail)) || ("HTTP " + r.status));
    return body;
  };

  const cameraCard = document.createElement("section");
  cameraCard.className = "card cam-card";
  cameraCard.innerHTML = `
    <h2>${RIOIcons.get("mic")} Camera</h2>
    <div class="cam-body" id="cam-body">
      <span class="skel">camera idle</span>
    </div>
    <div class="cam-actions">
      <button class="btn" id="cam-toggle">Open camera preview</button>
      <span class="cam-note" id="cam-note"></span>
    </div>`;

  const mediaCard = document.createElement("section");
  mediaCard.className = "card media-controller";
  mediaCard.innerHTML = `
    <h2>${RIOIcons.get("music")} Media Controller
        <span class="mc-badge">JioSaavn</span></h2>
    <div class="mc-now">
      <img class="mc-art" id="mc-art" alt="" hidden>
      <div class="mc-now-meta">
        <div class="mc-now-title" id="mc-now-title">Nothing playing</div>
        <div class="mc-now-artist" id="mc-now-artist">ask JARVIS: “play Tum Hi Ho”</div>
      </div>
    </div>
    <div class="mc-seek-row">
      <span class="mc-time" id="mc-tcur">0:00</span>
      <input class="mc-seek" id="mc-seek" type="range" min="0" max="1000"
             value="0" step="1" disabled>
      <span class="mc-time" id="mc-ttot">0:00</span>
    </div>
    <div class="mc-controls">
      <button class="mc-btn mc-main" id="mc-toggle" title="Play / Pause"
              disabled>${RIOIcons.get("play")}</button>
      <button class="mc-btn" id="mc-stop" title="Stop"
              disabled>${RIOIcons.get("square")}</button>
      <div class="mc-vol-wrap">${RIOIcons.get("volume", "mc-vol-ico")}
        <input class="mc-vol" id="mc-vol" type="range" min="0" max="100" value="100">
      </div>
    </div>
    <div class="mc-search">
      <input class="mc-q" id="mc-q" type="text" autocomplete="off"
             placeholder="Search JioSaavn — song, artist, album…">
      <button class="btn" id="mc-go">${RIOIcons.get("search")} Search</button>
    </div>
    <div class="mc-results" id="mc-results">
      <div class="mc-hint">Full 320 kbps streams, fetched straight from
        JioSaavn — no third-party service. Results play in the dock.</div>
    </div>`;

  const mc = (id) => mediaCard.querySelector("#" + id);
  const mcAudio = () => document.querySelector("#media-dock audio.dock-audio");
  const mcFmt = (s) => (!isFinite(s) || s < 0) ? "0:00"
    : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  function mcPaintNow(m) {
    const art = mc("mc-art"), seek = mc("mc-seek");
    const title = mc("mc-now-title"), artist = mc("mc-now-artist");
    const audioSrc = m && (m.source === "jiosaavn" || m.source === "audio");
    mc("mc-toggle").disabled = !m || !audioSrc;
    mc("mc-stop").disabled = !m;
    if (!m) {
      title.textContent = "Nothing playing";
      artist.textContent = "ask JARVIS: “play Tum Hi Ho”";
      art.hidden = true; art.removeAttribute("src");
      seek.disabled = true; seek.value = 0;
      mc("mc-tcur").textContent = "0:00"; mc("mc-ttot").textContent = "0:00";
      mcSetToggle(false);
      return;
    }
    title.textContent = m.title || "Now playing";
    artist.textContent = m.artist
      || (m.source === "youtube" ? "YouTube" : "");
    const img = (m.source === "youtube" && m.videoId)
      ? `https://i.ytimg.com/vi/${m.videoId}/mqdefault.jpg` : (m.image || "");
    if (img) { art.src = img; art.hidden = false; }
    else { art.hidden = true; art.removeAttribute("src"); }
    seek.disabled = !audioSrc;
  }

  function mcSetToggle(playing) {
    mc("mc-toggle").innerHTML = RIOIcons.get(playing ? "pause" : "play");
  }

  function mcBindAudio() {
    const a = mcAudio();
    if (!a || a.dataset.mcBound === "1") return;
    a.dataset.mcBound = "1";
    if (window.__mcVol != null) a.volume = window.__mcVol;
    a.addEventListener("timeupdate", () => {
      const seek = mc("mc-seek");
      if (!seek.matching) {
        seek.max = String(Math.max(1, Math.floor(a.duration || 1)));
        seek.value = String(Math.floor(a.currentTime || 0));
      }
      mc("mc-tcur").textContent = mcFmt(a.currentTime);
      mc("mc-ttot").textContent = mcFmt(a.duration);
    });
    a.addEventListener("durationchange", () => {
      mc("mc-ttot").textContent = mcFmt(a.duration);
    });
    a.addEventListener("play", () => mcSetToggle(true));
    a.addEventListener("pause", () => mcSetToggle(false));
  }

  async function mcPoll() {
    for (;;) {
      if (!mediaCard.isConnected) return;
      try {
        const j = await homeApi("/api/rio/media/next?wait=0");
        if (mediaCard.isConnected && j) {
          mcPaintNow(j.current || j.media);
          mcBindAudio();
          const a = mcAudio();
          if (a) mcSetToggle(!a.paused);
        }
      } catch (_) {}
      await new Promise(r => setTimeout(r, 1500));
    }
  }

  const mcSeek = mc("mc-seek");
  mcSeek.addEventListener("input", () => {
    mcSeek.matching = true;
    mc("mc-tcur").textContent = mcFmt(+mcSeek.value);
  });
  mcSeek.addEventListener("change", () => {
    mcSeek.matching = false;
    const a = mcAudio();
    if (a && isFinite(a.duration)) a.currentTime = +mcSeek.value;
  });

  mc("mc-toggle").addEventListener("click", async () => {
    const a = mcAudio();
    if (!a) return;                       // nothing loaded — nothing to toggle
    if (a.paused) {
      apiPost("/api/rio/media/resume", {}).catch(() => {});
      a.play().catch(() => {});
    } else {
      apiPost("/api/rio/media/pause", {}).catch(() => {});
      a.pause();
    }
  });

  mc("mc-stop").addEventListener("click", () => stopDock());

  mc("mc-vol").addEventListener("input", () => {
    window.__mcVol = +mc("mc-vol").value / 100;   // survives new tracks
    const a = mcAudio();
    if (a) a.volume = window.__mcVol;
  });

  async function mcSearch() {
    const q = mc("mc-q").value.trim();
    const res = mc("mc-results");
    if (!q) return;
    res.innerHTML = `<div class="mc-hint">searching JioSaavn for
      “${escapeHtml(q)}”…</div>`;
    let hits = [];
    try {
      const j = await homeApi(`/api/rio/media/search?q=${encodeURIComponent(q)}`);
      hits = j.results || [];
    } catch (e) {
      res.innerHTML = `<div class="mc-hint">search failed:
        ${escapeHtml(String(e.message || e))}</div>`;
      return;
    }
    if (!hits.length) {
      res.innerHTML = `<div class="mc-hint">no results on JioSaavn for
        “${escapeHtml(q)}”</div>`;
      return;
    }
    res.innerHTML = "";
    for (const h of hits) {
      const row = document.createElement("div");
      row.className = "mc-row";
      row.innerHTML = `
        ${h.image ? `<img class="mc-row-art" src="${escapeHtml(h.image)}" alt="">`
                  : `<span class="mc-row-art mc-row-art-e">${RIOIcons.get("music")}</span>`}
        <div class="mc-row-meta">
          <div class="mc-row-title">${escapeHtml(h.title || "")}</div>
          <div class="mc-row-artist">${escapeHtml(h.artist || "")}</div>
        </div>
        <button class="mc-btn mc-row-play" title="Play">${RIOIcons.get("play")}</button>`;
      row.querySelector(".mc-row-play").addEventListener("click", async () => {
        const r = await apiPost("/api/rio/media/play", h);
        if (r.status !== 200) {
          res.innerHTML = `<div class="mc-hint">play failed:
            ${escapeHtml((r.body && r.body.error) || ("HTTP " + r.status))}</div>`;
          return;
        }
        mcPaintNow(h);   // snappy local echo — the dock confirms via its poll
        mc("mc-toggle").disabled = false; mc("mc-stop").disabled = false;
      });
      res.append(row);
    }
  }
  mc("mc-go").addEventListener("click", mcSearch);
  mc("mc-q").addEventListener("keydown", (e) => { if (e.key === "Enter") mcSearch(); });

  async function camPoll() {
    for (;;) {
      if (!cameraCard.isConnected) return;
      try {
        const st = await homeApi("/api/rio/camera/state");
        if (!cameraCard.isConnected) return;
        const body = cameraCard.querySelector("#cam-body");
        const note = cameraCard.querySelector("#cam-note");
        const btn = cameraCard.querySelector("#cam-toggle");
        const streaming = st.ok && (st.active || st.streaming);
        // The webcam can drop off the USB bus mid-preview (autosuspend reset
        // storms on this Inspiron). While the user wants it on, re-arm the
        // stream — bounded: at most every 4th poll (~5s), so a genuinely dead
        // camera doesn't get hammered.
        const desired = cameraCard.dataset.desired === "1";
        if (desired && !streaming) {
          cameraCard.dataset.retry = String(
            (parseInt(cameraCard.dataset.retry || "0", 10) + 1) % 4);
          if (cameraCard.dataset.retry === "0") {
            homeApi("/api/rio/camera/toggle", { method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ on: true }) }).catch(() => {});
          }
        } else if (streaming) {
          cameraCard.dataset.retry = "0";
        }
        if (streaming && !body.querySelector("img.cam-live")) {
          body.innerHTML = `<img class="cam-live" alt="live camera"
             src="/api/rio/camera/stream?token=${encodeURIComponent(token())}">`;
          if (btn.textContent === "Open camera preview")
            btn.textContent = "Stop preview";
        } else if (!streaming) {
          // Keep the message LIVE (a device that vanished says "not found",
          // a busy one says "would not open") — never a frozen old reason.
          const skel = `camera idle${st.ok && st.error
            ? ` — ${escapeHtml(st.error)}` : ""}`;
          const el = body.querySelector(".skel");
          if (el) el.innerHTML = skel;
          else body.innerHTML = `<span class="skel">${skel}</span>`;
          note.textContent = "";
          if (btn.textContent === "Stop preview" && !desired)
            btn.textContent = "Open camera preview";
        }
        if (streaming) note.textContent = st.error ? `⚠ ${st.error}` :
          `${st.source === "stream" ? "streaming" : "JARVIS camera"} · ${st.clients || 0} viewer(s)`;
      } catch (_) {}
      await new Promise(r => setTimeout(r, 1200));
    }
  }

  cameraCard.querySelector("#cam-toggle").addEventListener("click", async () => {
    const btn = cameraCard.querySelector("#cam-toggle");
    const on = btn.textContent !== "Stop preview";
    cameraCard.dataset.desired = on ? "1" : "0";
    cameraCard.dataset.retry = "0";
    try {
      await homeApi("/api/rio/camera/toggle", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ on }) });
      btn.textContent = on ? "Stop preview" : "Open camera preview";
    } catch (e) {
      cameraCard.querySelector("#cam-note").textContent = `⚠ ${e.message}`;
    }
  });

  grid.append(right);
  right.prepend(mediaCard, cameraCard);

  page.append(grid);

  // wire + fetch AFTER the fragment is mounted (IDs must exist). The
  // one-shot loaders (loadAgentsMini, …) look up their target element
  // synchronously via document.getElementById — called here, before
  // viewEl.replaceChildren() has attached this fragment, every lookup
  // returns null and each loader silently no-ops, leaving the skeleton
  // text up forever. Defer them into the same microtask as wireHome,
  // which runs after render() has mounted the page.
  queueMicrotask(() => {
    wireHome(page);
    loadAgentsMini();
    loadConvsMini();
    loadFilesMini();
    loadChannelsMini();
    loadNotifsMini();
    if (lastHealth) paintServices(lastHealth);
    // The live card pollers MUST start here, after the mount: called at
    // definition time their first isConnected guard sees an unmounted card
    // and returns, so neither the camera state nor the controller's
    // now-playing/seek/volume binding EVER ran (found 2026-10-17).
    mcPoll();
    camPoll();
  });
  fetchHome();
  fetchMicMute();          // real mute state for the Home mute button
  return page;
}

/* ── mic mute (REAL: /api/settings/mic-mute — the assistant's ui.muted gate)
 * One shared fetch/toggle/paint used by the Home mute button and the Chat
 * mic-off control. State unknown (assistant not running) renders honestly. */
let micMuted = null;          // null = unknown (no live session)
let micMuteLive = false;      // setter registered (assistant running)?

async function fetchMicMute() {
  const res = await apiSend("/api/settings/mic-mute");
  if (res.status === 200 && res.body) {
    micMuted = (typeof res.body.muted === "boolean") ? res.body.muted : null;
    micMuteLive = !!res.body.live;
  } else {
    micMuted = null; micMuteLive = false;
  }
  paintMicMute();
  return micMuted;
}

async function toggleMicMute() {
  if (!micMuteLive) {
    toast("The assistant session isn't running — start RIO first.", "warn");
    return null;
  }
  const want = !micMuted;
  const res = await apiPost("/api/settings/mic-mute", { muted: !!want });
  if (res.status === 200 && res.body && res.body.ok) {
    micMuted = !!res.body.muted;
    paintMicMute();
    return micMuted;
  }
  toast(failMsg("mic-mute", res), "err");
  return null;
}

function paintMicMute() {
  const unknown = micMuted === null;
  const icon = micMuted ? "mic-off" : "mic";
  const text = unknown ? "Mic N/A" : micMuted ? "Mic muted" : "Mic live";

  RIOBus.emit("micmute", micMuted === true);   // the 3D face follows

  // Home button (left of Voice) — state icon + text label
  const btn = document.getElementById("btn-mute");
  if (btn) {
    btn.classList.toggle("mute-on", micMuted === true);
    btn.disabled = !micMuteLive;
    btn.title = !micMuteLive
      ? "Mic state unknown — the assistant session is not running"
      : micMuted ? "Microphone is muted — click to unmute"
                 : "Microphone is live — click to mute";
    btn.innerHTML = RIOIcons.get(unknown ? "mic-off" : icon) +
      `<span id="btn-mute-label">${text}</span>`;
  }
  // Chat mic-off control — ALWAYS the slashed glyph (never confusable with
  // the voice-input mic); the color carries the state (red = muted).
  const cm = document.getElementById("chat-mute");
  if (cm) {
    cm.classList.toggle("mute-on", micMuted === true);
    cm.disabled = !micMuteLive;
    cm.title = !micMuteLive
      ? "Mic state unknown — the assistant session is not running"
      : micMuted ? "Microphone muted — click to unmute"
                 : "Microphone live — click to mute";
    cm.innerHTML = RIOIcons.get("mic-off");
  }
  // Chat voice-input button: off while muted (never record into a muted mic)
  const mic = document.getElementById("chat-mic");
  if (mic) {
    mic.disabled = !micMuteLive || micMuted === true ||
                   mic.dataset.srOff === "1";
    mic.title = mic.dataset.srOff === "1"
      ? "Voice input not supported in this browser"
      : micMuted === true
        ? "Mic muted — unmute to use voice input"
        : micMuteLive ? "Voice input" : "Voice input (state unknown)";
  }
}

/* ── Home wiring (same real actions as before) ────────────────────────── */
function wireHome(root) {
  const fb = document.getElementById("cmd-feedback");
  const say = (msg, kind) => {
    if (!fb) return;
    fb.textContent = msg;
    fb.className = "cmd-feedback feedback" + (kind ? " fb-" + kind : "");
  };

  const input = document.getElementById("cmd-input");
  const send = async () => {
    const text = (input?.value || "").trim();
    if (!text) { say("Type something first.", "warn"); return; }
    say("Sending…");
    const mid = "m" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    const res = await apiPost("/api/command", { text, mid });
    if (res.status === 200 && res.body && res.body.ok) {
      say("Queued — RIO received your command.", "ok");
      input.value = "";
      refreshHome();
    } else {
      say(failMsg("command", res), "err");
    }
  };
  document.getElementById("btn-send")?.addEventListener("click", send);
  input?.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });

  document.getElementById("btn-mute")?.addEventListener("click", toggleMicMute);

  document.getElementById("face-rgb-btn")?.addEventListener("click", toggleRgbPop);

  document.getElementById("btn-voice")?.addEventListener("click", async () => {
    say("Waking RIO…");
    const res = await apiPost("/api/wake", {});
    say(res.status === 200 ? "Wake signal sent to the assistant."
                           : failMsg("wake", res),
        res.status === 200 ? "ok" : "err");
  });

  const fileInput = document.getElementById("file-input");
  document.getElementById("btn-upload")?.addEventListener("click", () => fileInput?.click());
  fileInput?.addEventListener("change", async () => {
    const f = fileInput.files && fileInput.files[0];
    if (!f) return;
    say(`Uploading ${f.name}…`);
    const fd = new FormData();
    fd.append("file", f);
    const res = await apiSend("/api/upload", { method: "POST", body: fd });
    if (res.status === 200 && res.body && (res.body.saved || res.body.name || res.body.ok)) {
      say(`Uploaded: ${res.body.name || res.body.saved || f.name}`, "ok");
      loadFilesMini();
    } else {
      say(failMsg("upload", res), "err");
    }
    fileInput.value = "";
  });

  const mform = document.getElementById("mission-form");
  document.getElementById("btn-mission")?.addEventListener("click", () => {
    if (mform) mform.hidden = !mform.hidden;
  });
  document.getElementById("mission-cancel")?.addEventListener("click", () => {
    if (mform) mform.hidden = true;
  });
  document.getElementById("mission-create")?.addEventListener("click", async () => {
    const name = (document.getElementById("mission-name")?.value || "").trim();
    const what = (document.getElementById("mission-what")?.value || "").trim();
    if (!name) { say("Mission needs a name.", "warn"); return; }
    say("Creating mission…");
    const res = await apiPost("/api/autonomy/tasks", {
      action: "add", name, kind: "scheduled", enabled: false,
      steps: what ? [{ prompt: what }] : [],
    });
    if (res.status === 200 && res.body && res.body.ok) {
      say(`Mission '${name}' created (paused — enable it in Missions).`, "ok");
      if (mform) mform.hidden = true;
      refreshHome();
    } else {
      say(failMsg("mission", res), "err");
    }
  });

  // Quick actions. NOTE: `root` here is the buildHome DocumentFragment, which
  // render() has already EMPTIED by moving its children into the view — so a
  // fragment-scoped querySelectorAll would find nothing and the buttons would
  // never get click handlers (long-standing bug, fixed 2026-10-14). The
  // buttons are unique to Home, so the document scope is correct and safe.
  document.querySelectorAll("[data-quick]").forEach(btn =>
    btn.addEventListener("click", () => {
      const q = btn.dataset.quick;
      if (q === "health") { refreshHome(); say("Re-checking…"); return; }
      if (q === "upload") { fileInput?.click(); return; }
      if (q === "login") { location.href = "/login"; return; }
      const route = "/" + q;
      if (ROUTE_BY_PATH[route]) navigate(route);
    }));
}

function failMsg(what, res) {
  if (res.status === 401) return `${what}: pairing required — pair this browser first (/login).`;
  if (res.status === 503) return `${what}: backend not ready (${res.body && res.body.error ? res.body.error : "503"}).`;
  if (res.error) return `${what}: ${res.error}`;
  const detail = res.body && (res.body.error || res.body.detail);
  return `${what}: ${detail || "HTTP " + res.status}`;
}

async function apiSend(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const tok = token();
  if (tok) headers["Authorization"] = `Bearer ${tok}`;
  if (opts.body && !(opts.body instanceof FormData))
    headers["Content-Type"] = "application/json";
  try {
    const r = await fetch(path, Object.assign({}, opts, { headers }));
    let body = null;
    try { body = await r.json(); } catch (_) { body = null; }
    return { status: r.status, body };
  } catch (e) {
    return { status: 0, error: "backend unreachable" };
  }
}
const apiPost = (path, payload) =>
  apiSend(path, { method: "POST", body: JSON.stringify(payload) });

/* ── one-shot side panels (no polling — refresh on page mount / action) ── */
async function loadAgentsMini() {
  const el = document.getElementById("agents-mini");
  if (!el) return;
  const res = await apiSend("/api/rio/agents");
  if (res.status !== 200 || !res.body || !res.body.ok) {
    el.innerHTML = `<div class="cmd-row"><span class="txt">Agent registry unavailable</span></div>`;
    lastAgents = null; paintSignals(); return;
  }
  lastAgents = res.body;
  const counts = {};
  for (const a of (res.body.agents || []))
    counts[a.status] = (counts[a.status] || 0) + 1;
  const total = (res.body.agents || []).length || 1;
  const order = ["READY", "RUNNING", "PAUSED", "ERROR", "OFFLINE", "NOT IMPLEMENTED"];
  el.innerHTML = `<div class="cmd-dist">` + order.filter(s => counts[s]).map(s => {
    const n = counts[s];
    const cls = s === "READY" ? "" : s === "RUNNING" ? "" : s === "ERROR" ? "fb-err" : "fb-warn";
    return `<div class="dist-line"><span class="${cls}">${s === "NOT IMPLEMENTED" ? "planned" : s.toLowerCase()}</span>` +
      `<span class="dist-bar"><i style="width:${Math.round(n / total * 100)}%"></i></span>` +
      `<span class="dist-n">${n}</span></div>`;
  }).join("") + `</div>`;
  paintSignals();
}

async function loadConvsMini() {
  const el = document.getElementById("convs-mini");
  if (!el) return;
  const res = await apiSend("/api/rio/conversations");
  if (res.status !== 200 || !res.body || !res.body.ok) {
    el.innerHTML = `<div class="cmd-row"><span class="txt">Conversation journal unavailable</span></div>`;
    return;
  }
  const convs = (res.body.conversations || []).slice(0, 5);
  if (!convs.length) {
    el.innerHTML = `<div class="cmd-row"><span class="txt">No conversations yet — open Chat to start one.</span></div>`;
    return;
  }
  el.replaceChildren(...convs.map(c => {
    const row = document.createElement("a");
    row.className = "cmd-row";
    row.href = "/chat";
    row.innerHTML = `<span class="txt"><b>${escapeHtml(c.title || "Untitled")}</b></span>` +
      `<span class="dim">${c.turns != null ? `${c.turns} turns` : ""}${c.last ? ` · ${fmtAge(ageFromIso(c.last))}` : ""}</span>`;
    row.addEventListener("click", (e) => { e.preventDefault(); navigate("/chat"); });
    return row;
  }));
}

function ageFromIso(iso) {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? Math.max(0, (Date.now() - t) / 1000) : null;
}

async function loadFilesMini() {
  const el = document.getElementById("files-mini");
  if (!el) return;
  const res = await apiSend("/api/rio/files");
  if (res.status !== 200 || !res.body || !res.body.ok) {
    el.innerHTML = `<div class="cmd-row"><span class="txt">Knowledge index unavailable</span></div>`;
    return;
  }
  const ups = (res.body.uploads || []).slice(0, 4);
  const docs = (res.body.documents || []);
  const rows = ups.map(u => {
    const row = document.createElement("a");
    row.className = "cmd-row";
    row.href = "/files";
    row.innerHTML = `<span class="txt"><b>${escapeHtml(u.name)}</b></span>` +
      `<span class="dim">${u.indexed ? "indexed" : u.doc_id ? "processing" : "not indexed"}</span>`;
    row.addEventListener("click", (e) => { e.preventDefault(); navigate("/files"); });
    return row;
  });
  const head = document.createElement("div");
  head.className = "cmd-row";
  head.innerHTML = `<span class="txt dim">${docs.length} indexed document${docs.length === 1 ? "" : "s"} · ${ups.length ? "recent uploads below" : "no uploads yet"}</span>`;
  el.replaceChildren(head, ...rows);
}

async function loadChannelsMini() {
  const el = document.getElementById("channels-mini");
  if (!el) return;
  const res = await apiSend("/api/rio/hermes");
  if (res.status !== 200 || !res.body || !res.body.ok) {
    el.innerHTML = `<div class="cmd-row"><span class="txt">Hermes probe unavailable</span></div>`;
    return;
  }
  const rows = [["Hermes gateway", res.body.hermes],
                ["Telegram", res.body.telegram],
                ["WhatsApp", res.body.whatsapp]];
  el.replaceChildren(...rows.map(([name, ch]) => {
    const st = (ch && ch.status) || "UNKNOWN";
    const cls = st === "CONNECTED" ? "ok-txt" : st === "ERROR" ? "err-txt" : "fb-warn";
    const row = document.createElement("div");
    row.className = "chan-row";
    row.title = (ch && ch.detail) || st;
    row.innerHTML = `<i class="dot ${st === "CONNECTED" ? "dot-ok" : st === "ERROR" ? "dot-err" : "dot-warn"}"></i>` +
      `<span class="chan-name">${name}</span>` +
      `<span class="chan-state ${cls}">${st === "CONNECTED" ? "connected" : st === "ERROR" ? "error" : st === "DISCONNECTED" ? "disconnected" : escapeHtml(st.toLowerCase())}</span>`;
    return row;
  }));
}

async function loadNotifsMini() {
  const el = document.getElementById("notif-mini");
  if (!el) return;
  const res = await apiSend("/api/rio/system/notifications?limit=4");
  if (res.status !== 200 || !res.body || !res.body.ok) {
    el.innerHTML = `<div class="cmd-row"><span class="txt">Notification centre unavailable</span></div>`;
    return;
  }
  const items = res.body.items || [];
  paintNotifCount({ items });
  if (!items.length) {
    el.innerHTML = `<div class="cmd-row"><span class="txt ok-txt">Nothing notable — journals are quiet.</span></div>`;
    return;
  }
  el.replaceChildren(...items.map(it => {
    const sev = String(it.severity || "Information").toLowerCase();
    const row = document.createElement("div");
    row.className = "cmd-row";
    row.title = it.detail || it.title;
    row.innerHTML = `<span class="txt"><b>${escapeHtml(it.title)}</b></span>` +
      `<span class="dim">${escapeHtml(fmtAge(it.age_s))}</span>`;
    row.style.borderLeft = `2px solid var(--${
      sev === "error" ? "err" : sev === "warning" ? "warn" : sev === "success" ? "ok" : "accent-3"})`;
    return row;
  }));
}

/* ── Signals — honest observations, only when a real condition holds ──── */
function paintSignals() {
  const el = document.getElementById("signals-mini");
  if (!el || !lastHome) return;
  const sig = [];
  const checks = (lastHome.health && lastHome.health.checks) || {};
  for (const k of ["qwen", "gemini", "rag", "hermes"]) {
    const c = checks[k];
    if (c && c.state === "down")
      sig.push({ sev: "warn", text: `${k[0].toUpperCase() + k.slice(1)} is offline` });
  }
  const a = lastHome.tasks && lastHome.tasks.autonomy;
  if (a && a.emergency_stop)
    sig.push({ sev: "err", text: "EMERGENCY STOP is active" });
  else if (a && a.pending_approvals > 0)
    sig.push({ sev: "warn", text: `${a.pending_approvals} approval${a.pending_approvals === 1 ? "" : "s"} pending` });
  const s = lastHome.system || {};
  if (s.disk && s.disk.percent >= 85)
    sig.push({ sev: "warn", text: `Disk usage at ${s.disk.percent}%` });
  const errs = (lastHome.activity && lastHome.activity.errors) || [];
  if (errs.length)
    sig.push({ sev: "err", text: `${errs.length} recent error${errs.length === 1 ? "" : "s"} in the journals` });
  if (!sig.length) {
    el.innerHTML = `<div class="cmd-row"><span class="txt ok-txt">All nominal — no warnings, no pending approvals.</span></div>`;
    return;
  }
  el.replaceChildren(...sig.slice(0, 5).map(g => {
    const row = document.createElement("div");
    row.className = "cmd-row";
    row.innerHTML = `<span class="txt ${g.sev === "err" ? "err-txt" : "fb-warn"}">${escapeHtml(g.text)}</span>`;
    return row;
  }));
}

/* ── face RGB customization popover (real: CSS vars + localStorage) ───── */
const FACE_RGB_DEFAULTS = { listening: "#38bdf8", thinking: "#fb923c",
                            speaking: "#fde047" };
const FACE_RGB_LABELS = [
  ["listening", "Listening", "blue"],
  ["thinking", "Thinking / working", "orange"],
  ["speaking", "Speaking", "light yellow"],
];

function toggleRgbPop() {
  if (document.getElementById("rgb-pop")) { closeRgbPop(); return; }
  if (!window.RIOFace) { toast("Face module not loaded.", "warn"); return; }
  const cur = RIOFace.rgbLoad();
  const pop = document.createElement("div");
  pop.className = "rgb-pop";
  pop.id = "rgb-pop";
  pop.innerHTML =
    `<div class="notif-pop-head">${RIOIcons.get("palette")} Face glow colors</div>` +
    FACE_RGB_LABELS.map(([key, label, hint]) => {
      const val = cur[key] || FACE_RGB_DEFAULTS[key];
      const custom = cur[key] ? "" : `<span class="rgb-def">${hint} · default</span>`;
      return `<label class="rgb-row"><span class="rgb-name">${label}${custom}</span>` +
        `<input type="color" data-rgb="${key}" value="${val}"></label>`;
    }).join("") +
    `<div class="rgb-actions">
       <button class="btn btn-mini" id="rgb-reset">${RIOIcons.get("rotate-ccw")} Reset to defaults</button>
     </div>
     <div class="set-note">Saved in this browser — applies to the face glow instantly.</div>`;
  document.body.append(pop);
  pop.querySelectorAll("input[type=color]").forEach(inp => {
    inp.addEventListener("input", () => {
      const m = RIOFace.rgbLoad();
      m[inp.dataset.rgb] = inp.value;
      RIOFace.rgbApply(m);
      RIOFace.rgbSave(m);
    });
  });
  pop.querySelector("#rgb-reset").addEventListener("click", () => {
    RIOFace.rgbApply({ listening: "", thinking: "", speaking: "" });
    RIOFace.rgbSave({ listening: "", thinking: "", speaking: "" });
    closeRgbPop();
    toast("Face colors reset to defaults.", "ok");
  });
}

function closeRgbPop() {
  document.getElementById("rgb-pop")?.remove();
}
document.addEventListener("click", (e) => {
  const pop = document.getElementById("rgb-pop");
  if (!pop) return;
  if (pop.contains(e.target) || $("face-rgb-btn")?.contains(e.target)) return;
  closeRgbPop();
});

/* ── Home data (REAL: /api/rio/home) ──────────────────────────────────── */
function refreshHome() { fetchHome(); }

async function fetchHome() {
  const res = await apiSend("/api/rio/home", { method: "GET" });
  if (res.status === 401) { paintHomeUnauth(); return; }
  if (res.status !== 200 || !res.body || !res.body.ok) { paintHomeDown(); return; }
  lastHome = res.body;
  RIOBus.emit("data:home", lastHome);
  paintHome(lastHome);
  paintSignals();
  if (res.body.health) {
    lastHealth = res.body.health;
    paintHealth(lastHealth);
    paintServices(lastHealth);
  }
}

function paintHomeUnauth() {
  coreSet("idle", "Pairing required", "pair this browser to see live data (/login)");
  setOrb("unknown");
  const d = document.getElementById("doing-detail");
  if (d) d.textContent = "pair this browser to see live data (/login)";
  document.querySelectorAll("#metrics .metric").forEach(m => {
    m.querySelector(".m-value").textContent = "N/A";
    m.querySelector(".m-meta").textContent = "pair required";
  });
}

function paintHomeDown() {
  coreSet("error", "Connection issue", "backend unreachable");
  setOrb("err");
  const d = document.getElementById("doing-detail");
  if (d) d.textContent = "backend unreachable";
}

/* face state setter with orb fallback */
function coreSet(state, label, detail) {
  if (window.RIOFace && homeFace) {
    homeFace.setState(state, { label, detail });
  } else if (window.RIOCore && homeCore) {
    homeCore.setState(state === "muted" ? "idle" : state, { label, detail });
  } else {
    setOrb(state === "error" ? "err" : state === "idle" ? "unknown" : "ok");
  }
  const line = document.getElementById("core-label");
  const det = document.getElementById("core-detail");
  if (line && label) line.textContent = label;
  if (det && detail) det.textContent = detail;
  // doing card mirrors the same honest state
  const dl = document.getElementById("doing-line");
  const dd = document.getElementById("doing-detail");
  if (dl && label) dl.textContent = label;
  if (dd && detail) dd.textContent = detail;
}

let homeFace = null;                    // registered by buildHome
let homeCore = null;                    // legacy fallback
window.__rioRegisterHomeFace = (f) => { homeFace = f; };
window.__rioRegisterHomeCore = (c) => { homeCore = c; };

function setOrb(state) {
  const orb = document.getElementById("rio-orb");
  if (orb) orb.className = "orb orb-" + (state || "unknown");
}

function fmtAge(sec) {
  if (sec == null) return "";
  if (sec < 60) return `${Math.max(0, Math.round(sec))}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}

function paintHome(d) {
  // doing — honest label from the live session state
  const doing = d.doing || {};
  if (doing.state === "busy") {
    coreSet("working", "RIO is working", doing.detail || "live session engaged");
  } else if (doing.state === "idle") {
    coreSet("listening", "Listening…", doing.detail || "microphone live — say something");
  } else {
    coreSet("idle", "RIO state unknown", "health probe returned no session state");
  }
  setOrb(doing.state === "busy" || doing.state === "idle" ? "ok" : "unknown");

  // system metrics (real or N/A — never invented)
  const s = d.system || {};
  const set = (key, val, meta) => {
    const el = document.querySelector(`[data-metric="${key}"]`);
    if (!el) return;
    el.querySelector(".m-value").textContent = val == null ? "N/A" : val;
    el.querySelector(".m-meta").textContent = meta == null ? "" : meta;
  };
  set("cpu", s.cpu_percent == null ? null : s.cpu_percent + "%",
      s.cpu_temp_c == null ? (s.uptime ? `up ${s.uptime}` : "")
                           : `${s.cpu_temp_c}°C · up ${s.uptime || "?"}`);
  set("ram", s.ram_percent == null ? null : s.ram_percent + "%",
      s.ram_used_gb == null ? null : `${s.ram_used_gb} / ${s.ram_total_gb} GB`);
  set("gpu", s.gpu_percent == null ? null : s.gpu_percent + "%",
      s.gpu_percent == null ? "no GPU reported" : "utilization");
  set("disk", s.disk == null ? null : s.disk.percent + "%",
      s.disk == null ? null
        : `${s.disk.used_gb} / ${s.disk.total_gb} GB (${s.disk.free_gb} GB free)`);
  set("net", s.net == null ? null : `↓${s.net.rx_kbps} ↑${s.net.tx_kbps} kbps`,
      s.net == null ? "N/A" : "sampled now");

  // models
  const ml = document.getElementById("models-list");
  if (ml) {
    const models = d.models || [];
    if (!models.length) ml.innerHTML = `<div class="cmd-row"><span class="txt">No providers reported</span></div>`;
    else ml.replaceChildren(...models.map(m => {
      const row = document.createElement("div");
      row.className = "cmd-row";
      const st = m.state === "up" ? "dot-ok" : m.state === "idle" ? "dot-warn" : "dot-err";
      row.innerHTML = `<span class="txt"><i class="dot ${st}"></i> <b>${escapeHtml(m.name)}</b>` +
        (m.models && m.models.length
          ? ` <span class="dim">${escapeHtml(m.models.join(", "))}</span>` : "") +
        `</span><span class="dim">${escapeHtml(m.detail || m.state)}</span>`;
      return row;
    }));
  }

  // tasks summary (in the Assistant card) + recent task rows (right column)
  const tsum = document.getElementById("tasks-summary");
  const tlist = document.getElementById("tasks-list");
  const a = d.tasks && d.tasks.autonomy;
  if (tsum) {
    tsum.textContent = !a ? "N/A — autonomy engine unavailable"
      : !a.available ? `N/A — ${a.detail || "engine not running"}`
      : `${a.task_count} mission(s) · ${a.enabled_tasks} enabled · ` +
        `${a.pending_approvals} approval(s) pending` +
        (a.emergency_stop ? " · EMERGENCY STOP ACTIVE" : "");
    tsum.className = "tasks-sum" + (a && a.emergency_stop ? " danger" : "");
  }
  if (tlist) {
    const recent = (d.tasks && d.tasks.recent) || [];
    if (!recent.length)
      tlist.innerHTML = `<div class="cmd-row"><span class="txt">no routed tasks in the journal yet</span></div>`;
    else tlist.replaceChildren(...recent.slice(-5).reverse().map(e => {
      const row = document.createElement("div");
      row.className = "cmd-row";
      const when = fmtAge(e.age_s) || e.ts || "";
      row.innerHTML = `<span class="txt">${escapeHtml(e.event)}${e.label ? " · " + escapeHtml(e.label) : ""}` +
        `${e.status ? " · " + escapeHtml(e.status) : ""}</span>` +
        `<span class="dim">${escapeHtml(when)}</span>`;
      return row;
    }));
  }

  // status bar
  paintStatusbar(d);
}

function paintServices(h) {
  const checks = (h && h.checks) || {};
  document.querySelectorAll("[data-svc]").forEach(chip => {
    const key = chip.dataset.svc;
    const dot = chip.querySelector(".dot");
    const stateEl = chip.querySelector(".chip-state");
    const c = key === "backend" ? { state: "up", detail: "online" } : checks[key];
    if (!c) { dot.className = "dot dot-unknown"; stateEl.textContent = "N/A"; return; }
    dot.className = "dot dot-" + (STATE_CLASS[c.state] || "unknown");
    stateEl.textContent = STATE_LABEL[c.state] || c.state;
    chip.title = c.detail || "";
  });
}

function escapeHtml(s) {
  // entity map built with chr() codes so nothing can decode it in transit
  const M = {};
  M['&'] = '&amp;';
  M['<'] = '&lt;';
  M['>'] = '&gt;';
  M['"'] = '&quot;';
  M["'"] = '&#39;';
  return String(s).replace(/[&<>"']/g, c => M[c]);
}

/* ── 7. Health (REAL: /api/rio/health) + bottom status bar ────────────── */
let lastHealth = null;
let healthTimer = null;

const STATE_CLASS = { up: "ok", active: "ok", idle: "warn",
                      unconfigured: "warn", down: "err" };
const STATE_LABEL = { up: "ONLINE", active: "ACTIVE", idle: "IDLE",
                      unconfigured: "NOT SET", down: "OFFLINE" };
const STACK_ROWS = [
  ["backend", "RIO ONLINE"],
  ["qwen",    "Qwen Local"],
  ["gemini",  "Gemini"],
  ["rag",     "RAG"],
  ["hermes",  "Hermes"],
];

function renderStackSkeleton() {
  stackEl.replaceChildren(...STACK_ROWS.map(([key, label]) => {
    const row = document.createElement("div");
    row.className = "status-row";
    row.dataset.check = key;
    row.innerHTML = `<i class="dot dot-unknown"></i>` +
      `<span class="label">${escapeHtml(label)}</span>` +
      `<span class="detail" title=""></span>`;
    return row;
  }));
}

function token() {
  try { return sessionStorage.getItem("jarvis_token") || ""; } catch (_) { return ""; }
}

async function fetchHealth() {
  let res;
  try {
    res = await fetch("/api/rio/health", {
      headers: token() ? { "Authorization": `Bearer ${token()}` } : {},
      cache: "no-store",
    });
  } catch (e) {
    lastHealth = null;
    RIOBus.emit("data:health", null);
    paintBackendDown("backend unreachable");
    return;
  }
  if (res.status === 401) {
    lastHealth = { ok: false, unauth: true };
    RIOBus.emit("data:health", lastHealth);
    paintBackend("pair", "pair required");
    paintHealth(null);
    return;
  }
  if (!res.ok) {
    paintBackend("err", `HTTP ${res.status}`);
    return;
  }
  try {
    const data = await res.json();
    lastHealth = data;
    RIOBus.emit("data:health", data);
    paintBackend("ok", "RIO ONLINE");
    paintHealth(data);
  } catch (e) {
    paintBackend("err", "bad response");
  }
}

function paintBackend(state, label) {
  sysDot.className = "dot " +
    (state === "ok" ? "dot-ok" : state === "pair" ? "dot-warn" : "dot-err");
  sysLabel.textContent = label;
  const sbDot = $("sb-dot"), sbText = $("sb-backend");
  if (sbDot) sbDot.className = "dot " + sysDot.className.replace("dot ", "");
  if (sbText) sbText.textContent = label;
  const row = stackEl.querySelector('[data-check="backend"]');
  if (row) {
    row.querySelector(".dot").className = "dot " + sysDot.className.replace("dot dot-", "dot-");
    row.querySelector(".label").textContent =
      state === "ok" ? "RIO ONLINE" : state === "pair" ? "RIO PAIRING" : "RIO OFFLINE";
    row.querySelector(".detail").textContent = label;
    row.querySelector(".detail").title = label;
  }
}

function paintBackendDown(detail) {
  paintBackend("err", "offline");
  const row = stackEl.querySelector('[data-check="backend"]');
  if (row) { row.querySelector(".detail").textContent = detail;
             row.querySelector(".detail").title = detail; }
  for (const rowEl of stackEl.querySelectorAll(".status-row")) {
    if (rowEl.dataset.check === "backend") continue;
    rowEl.querySelector(".dot").className = "dot dot-unknown";
    rowEl.querySelector(".detail").textContent = "";
  }
}

function paintHealth(data) {
  for (const rowEl of stackEl.querySelectorAll(".status-row")) {
    const key = rowEl.dataset.check;
    if (key === "backend") continue;
    const dot = rowEl.querySelector(".dot");
    const det = rowEl.querySelector(".detail");
    if (!data || !data.checks || !data.checks[key]) {
      dot.className = "dot dot-unknown"; det.textContent = ""; det.title = "";
      continue;
    }
    const c = data.checks[key];
    dot.className = "dot dot-" + (STATE_CLASS[c.state] || "unknown");
    det.textContent = c.state === "up" || c.state === "active"
      ? "ok" : STATE_LABEL[c.state] || c.state;
    det.title = `${c.state} — ${c.detail || ""}`;
  }
  const grid = document.getElementById("home-grid");
  if (grid) {
    for (const card of grid.querySelectorAll("[data-check]")) {
      const key = card.dataset.check;
      const val = card.querySelector("[data-value]");
      const meta = card.querySelector("[data-meta]");
      const c = data && data.checks && data.checks[key];
      if (!c) {
        val.className = "value"; val.textContent = "—";
        meta.textContent = data ? "no probe data" : "not authenticated / offline";
        continue;
      }
      const cls = STATE_CLASS[c.state] || "";
      val.className = "value" + (cls ? " " + cls : "");
      val.textContent = STATE_LABEL[c.state] || c.state;
      meta.textContent = c.detail || "";
      meta.title = c.detail || "";
    }
  }
}

/* bottom status bar — only real values */
function paintStatusbar(d) {
  const modelTxt = $("sb-model-txt"), taskTxt = $("sb-task-txt"),
        cpuTxt = $("sb-cpu-txt"), ramTxt = $("sb-ram-txt");
  if (modelTxt)
    modelTxt.textContent = (d.session_model || "").replace(/^models\//, "") || "—";
  if (taskTxt) {
    const doing = d.doing || {};
    taskTxt.textContent = doing.state === "busy"
      ? (doing.detail || "working…") : "idle";
  }
  const s = d.system || {};
  if (cpuTxt) cpuTxt.textContent = s.cpu_percent == null ? "—" : `CPU ${s.cpu_percent}%`;
  if (ramTxt) ramTxt.textContent = s.ram_percent == null ? "—" : `RAM ${s.ram_percent}%`;
}

/* ── Clock (real client time, topbar + status bar) ────────────────────── */
function tickClock() {
  const d = new Date();
  clockEl.textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  clockEl.title = d.toLocaleString();
  const sb = $("sb-clock");
  if (sb) sb.textContent = d.toLocaleDateString(undefined,
    { weekday: "short", day: "numeric", month: "short" }) +
    " · " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/* ── topbar icons + notifications button ──────────────────────────────── */
$("menu-btn").innerHTML = RIOIcons.get("menu");
$("topbar-settings").innerHTML = RIOIcons.get("settings");
document.querySelector(".notif-btn .bell, #notif-btn .bell").innerHTML = RIOIcons.get("bell");
$("notif-btn").addEventListener("click", toggleNotifPop);

/* ── Mobile sidebar toggle ────────────────────────────────────────────── */
$("menu-btn").addEventListener("click", () => sidebar.classList.toggle("open"));
document.addEventListener("click", (e) => {
  if (window.innerWidth > 980) return;
  if (!sidebar.classList.contains("open")) return;
  if (sidebar.contains(e.target) || $("menu-btn").contains(e.target)) return;
  sidebar.classList.remove("open");
});

/* ── 8. Popstate / boot ───────────────────────────────────────────────── */
window.addEventListener("popstate", () => render(location.pathname));

/* ── 8.5 Global media dock (EVERY tab) ──────────────────────────────────
 * Playback must not depend on which page is open when the user asks
 * JARVIS to play — the old Home-only card went silent whenever the user
 * was on Chat. The dock is fixed, app-level, and the ONLY player.
 * Chrome blocks autoplay-with-sound for voice-driven windows (few clicks
 * → low Media Engagement Index), so a rejected play() becomes an explicit
 * Play button — never a silent card. */
function paintDock(m) {
  const dock = $("media-dock");
  if (!dock) return;
  const sig = `${m.seq || ""}|${m.source || ""}|${m.videoId || m.url || m.spotifyId || ""}`;
  if (dock.dataset.sig === sig && !dock.hidden) return;
  dock.dataset.sig = sig;
  dock.hidden = false;
  let inner = "";
  if (m.source === "youtube" && m.videoId) {
    inner = `<iframe class="dock-frame" src="https://www.youtube.com/embed/${escapeHtml(m.videoId)}?autoplay=1"
      allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen
      referrerpolicy="strict-origin-when-cross-origin"></iframe>`;
  } else if (m.source === "spotify" && m.spotifyId) {
    inner = `<iframe class="dock-frame dock-frame-s"
      src="https://open.spotify.com/embed/${escapeHtml(m.spotifyType)}/${escapeHtml(m.spotifyId)}"
      allow="autoplay; clipboard-write; encrypted-media" loading="lazy"></iframe>`;
  } else if ((m.source === "jiosaavn" || m.source === "audio") && m.url) {
    inner = `<audio class="dock-audio" controls
      src="/api/rio/media/proxy?u=${encodeURIComponent(m.url)}&token=${encodeURIComponent(token())}"></audio>`;
  } else {
    inner = `<div class="dock-note">unsupported media payload</div>`;
  }
  dock.innerHTML = `
    <div class="dock-head">
      <span class="dock-title">${escapeHtml(m.title || "Now playing")}</span>
      <button class="dock-stop" id="dock-stop" title="Stop">${RIOIcons.get("x")}</button>
    </div>${inner}
    ${m.note ? `<div class="dock-note">${escapeHtml(m.note)}</div>` : ""}`;
  const stop = dock.querySelector("#dock-stop");
  if (stop) stop.addEventListener("click", () => stopDock());
  const audio = dock.querySelector("audio.dock-audio");
  if (audio) {
    audio.addEventListener("playing", () => reportDockState("playing"));
    audio.addEventListener("pause", () => {
      // Removing the element fires a pause event too — that's a Stop, not a
      // Pause; a stale "paused" report would overwrite the truth.
      if (!dock.contains(audio)) return;
      if (!audio.ended && !audio.error) reportDockState("paused");
    });
    audio.addEventListener("error", () => {
      if (!dock.contains(audio)) return;
      reportDockState("error",
        audio.error ? `media error code ${audio.error.code}` : "stream failed");
      // A dead player must say so — the old dock sat there looking loaded.
      let note = dock.querySelector(".dock-note");
      if (!note) {
        note = document.createElement("div");
        note.className = "dock-note";
        dock.append(note);
      }
      note.textContent = "stream failed to load — click here to retry";
      note.style.cursor = "pointer";
      note.onclick = () => {
        note.textContent = "reconnecting…";
        audio.load();
        audio.play().catch(() => reportDockState("blocked"));
      };
    });
    audio.play().catch(() => {
      // A stop/repaint while play() was still pending rejects with an abort —
      // that is not a block, and the dock's player may already be gone.
      if (!dock.contains(audio)) return;
      reportDockState("blocked");
      if (dock.querySelector(".dock-play")) return;
      const head = dock.querySelector(".dock-head");
      if (!head) return;
      const b = document.createElement("button");
      b.className = "dock-play";
      b.textContent = "▶ Tap to play";
      b.addEventListener("click", () => audio.play().catch(() => {}));
      head.append(b);
    });
  }
}

function reportDockState(status, detail) {
  // The dock is the only witness to what actually happened on screen —
  // POST it so now_playing answers from fact, not hope. Fire-and-forget.
  apiPost("/api/rio/media/state", { status, detail: detail || "" })
    .catch(() => {});
}

function stopDock(post) {
  // post=true: the USER pressed Stop — tell the server (it clears the bus and
  // tells the other pages). post=false: this stop ARRIVED from the server —
  // clear locally and say nothing back. Posting back used to echo: server
  // broadcast → every page POSTed /media/stop → server broadcast again… a
  // stop-storm that drained the queue and killed every play payload
  // (observed 2026-10-17).
  if (post !== false) apiSend("/api/rio/media/stop", { method: "POST" });
  const dock = $("media-dock");
  if (dock) { dock.dataset.sig = ""; dock.innerHTML = ""; dock.hidden = true; }
}

/* Chrome blocks autoplay-with-sound for voice-driven windows (few clicks →
 * low Media Engagement Index). The ▶ button is the fallback, but ANY user
 * gesture unlocks audio — so one capture-phase pointerdown retries the
 * dock's audio on the next click anywhere: the music starts without the
 * user having to find the small button, and the blocked state clears. */
document.addEventListener("pointerdown", () => {
  const a = document.querySelector("#media-dock audio.dock-audio");
  if (a && a.paused && a.readyState > 0 && !a.error)
    a.play().catch(() => {});
}, { capture: true });

function applyDockCmd(cmd) {
  // Transport commands from voice / the controller. Idempotent by design:
  // the same pause can arrive over the live WS broadcast AND the poll
  // fallback (cmd=1) — pausing a paused <audio> is a no-op.
  if (!cmd || !cmd.kind) return;
  if (cmd.kind === "stop") { stopDock(false); return; }
  const a = document.querySelector("#media-dock audio.dock-audio");
  if (!a) return;
  if (cmd.kind === "pause") a.pause();
  else if (cmd.kind === "resume") a.play().catch(() => reportDockState("blocked"));
}

/* Voice/controller transport commands normally arrive INSTANTLY over the
 * shared /ws broadcast; the dockLoop's cmd=1 poll below is the fallback for
 * when this socket is down. */
(function mediaCmdWS() {
  try {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(
      `${proto}://${location.host}/ws?token=${encodeURIComponent(token())}`);
    ws.onmessage = (ev) => {
      let m = null; try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (!m || m.type !== "media_event") return;
      // A fresh play payload lands sub-second over the socket (the dock's
      // own long-poll may be mid-wait) — paint it now; paintDock's sig
      // guard makes the later poll delivery a no-op.
      if (m.kind === "play" && m.media) paintDock(m.media);
      applyDockCmd(m);
    };
    ws.onclose = () => setTimeout(mediaCmdWS, 3000);
  } catch (_) {}
})();

(async function dockLoop() {
  /* Sequential BY DESIGN: the old loop armed the next 400 ms timer before
   * its fetch resolved, so a single page held ~10 concurrent 4 s long-polls;
   * a few open pages then saturated the server's thread pool, the audio
   * proxy starved, nothing ever played and no state ever came back — the
   * exact "dashboard player did not start" symptom (fixed 2026-10-17).
   * One in-flight poll per page. */
  for (;;) {
    const dock = $("media-dock");
    if (dock && token()) {
      const res = await apiSend("/api/rio/media/next?wait=4&cmd=1");
      const j = res && res.body;
      if (j) {
        if (j.cmd) applyDockCmd(j.cmd);
        const m = j.media || j.current;
        if (m) paintDock(m);
        else if (!j.current && !dock.hidden) {
          dock.hidden = true; dock.innerHTML = ""; dock.dataset.sig = "";
        }
      }
    }
    await new Promise(r => setTimeout(r, 400));
  }
})();

(function boot() {
  applyCompact(loadCompact());
  renderStackSkeleton();
  const path = ROUTE_BY_PATH[location.pathname] ? location.pathname : "/home";
  if (path !== location.pathname) history.replaceState(null, "", path);
  render(path);
  tickClock();
  setInterval(tickClock, 15000);
  fetchHealth();
  healthTimer = setInterval(fetchHealth, 30000);
  // one-shot badge fill (real journal data; no fake counts)
  if (token()) fetchNotifications();
})();
