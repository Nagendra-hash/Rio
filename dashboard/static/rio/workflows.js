/* RIO Workflows — one page per agent, like the trading desk pipeline.
 *
 * Data:  GET /api/rio/agent-workflows
 *          → static, source-verified stage lists (_WORKFLOWS in rio_api.py)
 *          + LIVE status merged server-side: orchestrator registry state for
 *          the Agent Center agents, the desk progress file (desk_state.json)
 *          for the six paper-trading desk agents.
 *
 * Routes (client):
 *   /workflows      → index: every agent card, grouped
 *   /agents/<key>   → one agent's pipeline page (deep-linkable)
 *
 * Honesty rules (same as the Agent Center): the stage list describes what the
 * agent's code actually does; status chips are measured (READY/ERROR/…/IDLE),
 * never invented. A desk that is not running shows IDLE, not a fake progress
 * bar. Polling happens ONLY while a desk run is live, and self-stops when the
 * page is navigated away from (rootEl.isConnected check).
 */
"use strict";

const RIOWorkflows = (() => {

  // ── state ────────────────────────────────────────────────────────────
  let data = null;          // last GET /api/rio/agent-workflows payload
  let loadPromise = null;   // in-flight fetch (shared between pages)
  let rootEl = null;
  let pollTimer = null;

  // ── helpers (same conventions as agents.js) ──────────────────────────
  function token() {
    try { return sessionStorage.getItem("jarvis_token") || ""; } catch (_) { return ""; }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function api(path) {
    const r = await fetch(path, {
      headers: { "Authorization": `Bearer ${token()}`,
                 "Content-Type": "application/json" },
      cache: "no-store",
    });
    let j = null;
    try { j = await r.json(); } catch (_) { /* empty body */ }
    if (r.status === 401) throw Object.assign(new Error("pairing required"), { code: 401 });
    if (!r.ok) throw Object.assign(
      new Error((j && (j.detail || j.error)) || ("HTTP " + r.status)),
      { code: r.status, body: j });
    return j;
  }

  function load(force = false) {
    if (loadPromise && !force) return loadPromise;
    loadPromise = api("/api/rio/agent-workflows")
      .then(j => { data = j; return j; })
      .catch(e => {
        data = null;
        loadPromise = null;               // allow a retry on the next visit
        throw e;
      });
    return loadPromise;
  }

  const byKey = k => ((data && data.agents) || []).find(a => a.key === k) || null;

  const CHIP = {
    "READY": "wf-ready", "RUNNING": "wf-running", "PAUSED": "wf-paused",
    "ERROR": "wf-error", "IDLE": "wf-idle", "PENDING": "wf-idle",
    "DONE": "wf-ready", "OFFLINE": "wf-idle", "NOT IMPLEMENTED": "wf-ni",
  };
  const chipCls = s => CHIP[s] || "wf-idle";

  const GROUPS = {
    orchestrator: "Orchestrator agents",
    desk: "Paper-trading desk",
  };

  // ── index page ───────────────────────────────────────────────────────
  function paintIndex(root) {
    const groups = ["orchestrator", "desk"];
    const wrap = document.createElement("div");
    wrap.className = "wf-wrap";
    wrap.innerHTML = `
      <h1>Agent Workflows</h1>
      <p class="sub">One page per agent: the pipeline it actually runs, the
         tools it uses, and its live status. Statuses are measured — never
         simulated.</p>
      <div class="wf-groups"></div>`;
    const box = wrap.querySelector(".wf-groups");

    for (const g of groups) {
      const list = (data.agents || []).filter(a => a.group === g);
      if (!list.length) continue;
      const sec = document.createElement("section");
      sec.className = "wf-group";
      sec.innerHTML = `<h2>${esc(GROUPS[g] || g)}</h2>
        <div class="wf-grid"></div>`;
      const grid = sec.querySelector(".wf-grid");
      for (const a of list) {
        const card = document.createElement("a");
        card.className = "wf-card";
        card.href = a.page;
        card.addEventListener("click", (e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault();
          navigate(a.page);
        });
        const stages = (a.stages || []).length;
        card.innerHTML = `
          <div class="wf-card-top">
            <span class="wf-ico">${RIOIcons.get(a.icon || "bot")}</span>
            <span class="ac-chip ${chipCls(a.status)}">${esc(a.status)}</span>
          </div>
          <div class="wf-card-name">${esc(a.name)}</div>
          <div class="wf-card-role">${esc(a.role)}</div>
          <div class="wf-card-out">${esc(a.output || "")}</div>
          <div class="wf-card-meta">${stages} stage${stages === 1 ? "" : "s"}</div>`;
        grid.append(card);
      }
      box.append(sec);
    }
    root.replaceChildren(wrap);
  }

  // ── one agent's page ─────────────────────────────────────────────────
  function paintAgent(root, a) {
    const idx = (data.agents || []).findIndex(x => x.key === a.key);
    const prev = idx > 0 ? data.agents[idx - 1] : null;
    const next = idx >= 0 && idx < data.agents.length - 1 ? data.agents[idx + 1] : null;

    const wrap = document.createElement("div");
    wrap.className = "wf-wrap";

    const back = document.createElement("a");
    back.className = "wf-back";
    back.href = "/workflows";
    back.innerHTML = `${RIOIcons.get("chevron-right")} all agent workflows`;
    back.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      navigate("/workflows");
    });

    const deskStrip = a.desk_states ? `
      <div class="wf-deskstrip">
        <span class="wf-desklabel">desk run</span>
        ${Object.entries(a.desk_states).map(([k, st]) =>
          `<span class="wf-deskstage">${esc(k.replace(/_/g, " "))}
             <span class="ac-chip ${chipCls(st)}">${esc(st)}</span></span>`).join("")}
        <span class="wf-desknote">${esc(a.status_detail || "")}</span>
      </div>` : "";

    const head = `
      <div class="wf-head">
        <span class="wf-ico wf-ico-lg">${RIOIcons.get(a.icon || "bot")}</span>
        <div class="wf-head-txt">
          <div class="wf-title-row">
            <h1>${esc(a.name)}</h1>
            <span class="ac-chip ${chipCls(a.status)}">${esc(a.status)}</span>
          </div>
          <div class="wf-role">${esc(a.role || "")}${a.impl === "stub" ?
            " · registered stub" : ""}</div>
          ${a.status_detail ? `<div class="wf-note">${esc(a.status_detail)}</div>` : ""}
        </div>
      </div>
      <p class="sub">${esc(a.summary || "")}</p>
      ${deskStrip}`;

    const pipe = `
      <div class="wf-pipe">
        ${(a.stages || []).map((s, i) => `
          <div class="wf-stage">
            <div class="wf-n">${i + 1}</div>
            <div class="wf-body">
              <div class="wf-stagetitle">${esc(s.title || "")}</div>
              <div class="wf-stagedetail">${esc(s.detail || "")}</div>
              ${s.tool ? `<div class="wf-tool">${RIOIcons.get("zap")} ${esc(s.tool)}</div>` : ""}
            </div>
          </div>`).join("")}
        <div class="wf-final">
          <div class="wf-n wf-n-out">${RIOIcons.get("check")}</div>
          <div class="wf-body">
            <div class="wf-stagetitle">Output</div>
            <div class="wf-stagedetail">${esc(a.output || "")}</div>
          </div>
        </div>
      </div>`;

    const tools = (a.tools && a.tools.length) ? `
      <div class="wf-tools">
        <div class="wf-tools-h">Tools</div>
        ${a.tools.map(t => `<span class="wf-toolchip">${esc(t)}</span>`).join("")}
      </div>` : "";

    const links = [];
    if (a.center) links.push(`<a href="/agents" class="wf-link" data-nav>open in Agent Center</a>`);

    const foot = `
      <div class="wf-footnav">
        ${prev ? `<a class="wf-prev" href="${esc(prev.page)}" data-wf-prev>
            ${RIOIcons.get("chevron-right")}<span class="wf-flip"></span>${esc(prev.name)}</a>`
               : "<span></span>"}
        ${next ? `<a class="wf-next" href="${esc(next.page)}" data-wf-next>${esc(next.name)}
            ${RIOIcons.get("chevron-right")}</a>` : "<span></span>"}
      </div>`;

    wrap.innerHTML = `<div class="wf-agentpage">${back}${head}${pipe}${tools}
      ${links.length ? `<div class="wf-links">${links.join("")}</div>` : ""}${foot}</div>`;

    // internal navigation through the SPA router
    wrap.querySelectorAll("a[data-nav], a.wf-back").forEach(el => {
      el.addEventListener("click", (e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        navigate(el.getAttribute("href"));
      });
    });
    const pv = wrap.querySelector("[data-wf-prev]");
    if (pv) pv.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      navigate(pv.getAttribute("href"));
    });
    const nx = wrap.querySelector("[data-wf-next]");
    if (nx) nx.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      navigate(nx.getAttribute("href"));
    });

    root.replaceChildren(wrap);
  }

  // ── live polling while a desk run is in progress ─────────────────────
  function maybePoll(slug) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (!slug || !data || data.desk_status !== "running") return;
    pollTimer = setInterval(async () => {
      if (!rootEl || !rootEl.isConnected) {          // navigated away
        clearInterval(pollTimer); pollTimer = null;
        return;
      }
      try {
        const j = await load(true);
        data = j;
        if (data.desk_status !== "running") {
          clearInterval(pollTimer); pollTimer = null;
        }
        const a = byKey(slug);
        if (a) paintAgent(rootEl, a);                // repaint with live state
      } catch (_) { /* keep the stale paint; retry next tick */ }
    }, 5000);
  }

  function paintError(root, message, slug) {
    const wrap = document.createElement("div");
    wrap.className = "wf-wrap";
    wrap.innerHTML = `
      <h1>${slug ? "Agent" : "Agent Workflows"} ${slug ? esc(slug) : ""}</h1>
      <p class="sub">${esc(message)}</p>
      <p><a href="/workflows" class="wf-link" data-retry>try again</a></p>`;
    const link = wrap.querySelector("[data-retry]");
    if (link) link.addEventListener("click", (e) => {
      e.preventDefault();
      loadPromise = null;
      navigate(location.pathname);
    });
    root.replaceChildren(wrap);
  }

  // ── module API ───────────────────────────────────────────────────────
  function build(slug) {
    const root = document.createElement("div");
    rootEl = root;

    if (!data) {
      root.innerHTML = `<div class="wf-wrap"><h1>${slug ? "Agent" :
        "Agent Workflows"}</h1><p class="sub">loading…</p></div>`;
    }
    load().then(j => {
      if (!root.isConnected) return;                 // navigated away mid-load
      if (slug) {
        const a = byKey(slug);
        if (!a) {
          paintError(root, `No workflow page for “${slug}”. Known agents: ` +
            (j.agents || []).map(x => x.key).join(", "), slug);
        } else {
          paintAgent(root, a);
          maybePoll(slug);
        }
      } else {
        paintIndex(root);
      }
    }).catch(e => {
      if (!root.isConnected) return;
      paintError(root, e.code === 401
        ? "Pairing required — open /login in this browser first."
        : `backend unavailable: ${e.message}`, slug);
    });

    return root;   // single element; content paints async
  }

  function teardown() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    rootEl = null;
  }

  return { build, teardown };
})();

window.RIOWorkflows = RIOWorkflows;
