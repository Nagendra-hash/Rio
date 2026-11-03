/* RIO Tools — Universal Repository Agent (managed GitHub repos + skills).
 *
 * Data:   GET  /api/repo/overview                registry + skills + jobs
 *         POST /api/repo/install {url}           queue an install (inspected,
 *                                                scanned, isolated — detached)
 *         POST /api/repo/jobs/{id}/cancel
 *         POST /api/repo/repos/{name}/remove {confirm:true}
 *         POST /api/repo/skills/{id}/state {enabled}
 * Honest: statuses come from the job store; a skill is READY only when its
 * providers are actually configured. Nothing here executes repo code.
 */
"use strict";

const RIOTools = (() => {

  let overview = null;
  let busy = false;
  let pollTimer = null;

  const $ = (sel, el) => (el || document).querySelector(sel);

  function token() {
    try { return sessionStorage.getItem("jarvis_token") || ""; }
    catch (_) { return ""; }
  }

  async function api(path, opts = {}) {
    const r = await fetch(path, Object.assign({
      headers: { "Authorization": `Bearer ${token()}`,
                 "Content-Type": "application/json" },
    }, opts));
    let body = {};
    try { body = await r.json(); } catch (_) {}
    return { status: r.status, body };
  }

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "\u0026amp;").replace(/</g, "\u0026lt;")
      .replace(/>/g, "\u0026gt;").replace(/"/g, "\u0026quot;");
  }

  function statusClass(status) {
    const s = String(status || "").toUpperCase();
    if (s === "READY" || s === "COMPLETED") return "ok";
    if (s === "FAILED") return "err";
    if (s === "RUNNING" || s === "INSTALLING" || s === "QUEUED") return "run";
    if (s === "NEEDS_CONFIGURATION" || s === "WAITING_FOR_APPROVAL") return "warn";
    return "idle";
  }

  /* ── build ─────────────────────────────────────────────────────────── */
  function build() {
    const page = document.createElement("div");
    page.className = "page tl-page";

    page.innerHTML = `
      <div class="tl-head">
        <div>
          <h1>Tools & Skills</h1>
          <p class="tl-sub">Managed GitHub repositories under
             <code>~/JarvisTools</code> — inspected, risk-scanned and built in
             isolated environments. Jobs run detached and are verified before
             completion.</p>
        </div>
        <button class="btn" id="tl-refresh">Refresh</button>
      </div>

      <div class="card tl-install">
        <div class="tl-install-row">
          <input id="tl-url" type="text" spellcheck="false"
                 placeholder="https://github.com/owner/repo" />
          <button class="btn primary" id="tl-install">Install</button>
        </div>
        <div class="tl-hint" id="tl-install-msg">
          Inspect-first: licence, dependencies and a static risk scan run
          before anything is executed. No sudo, no README scripts.
        </div>
      </div>

      <h2 class="tl-h2">Installed repositories & skills</h2>
      <div class="tl-skills" id="tl-skills"></div>

      <h2 class="tl-h2">Jobs</h2>
      <div class="tl-jobs" id="tl-jobs"></div>
    `;

    $("#tl-refresh", page).addEventListener("click", () => load());
    $("#tl-install", page).addEventListener("click", () => install(page));
    $("#tl-url", page).addEventListener("keydown", (e) => {
      if (e.key === "Enter") install(page);
    });

    load();
    pollTimer = setInterval(() => {
      if (!document.hidden) load();
    }, 5000);
    const observer = new MutationObserver(() => {
      if (!document.body.contains(page)) {
        clearInterval(pollTimer);
        observer.disconnect();
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    return page;
  }

  async function load() {
    if (busy) return;
    busy = true;
    try {
      const res = await api("/api/repo/overview");
      if (res.status === 200 && res.body.ok) {
        overview = res.body;
        paint();
      }
    } finally {
      busy = false;
    }
  }

  async function install(page) {
    const input = $("#tl-url", page);
    const msg = $("#tl-install-msg", page);
    const url = input.value.trim();
    if (!url) { msg.textContent = "Give a github.com/owner/repo URL."; return; }
    msg.textContent = "Queueing install…";
    const res = await api("/api/repo/install", {
      method: "POST", body: JSON.stringify({ url }),
    });
    if (res.status === 200 && res.body.ok) {
      if (res.body.deduplicated) {
        msg.textContent = "An identical job is already active — nothing " +
                          "duplicated (" + res.body.job_id + ").";
      } else {
        msg.textContent = "Install queued: " + res.body.job_id +
                          " — detached worker started.";
        input.value = "";
      }
      load();
    } else {
      msg.textContent = "Refused: " + (res.body.error || res.status);
    }
  }

  /* ── paint ─────────────────────────────────────────────────────────── */
  function paint() {
    paintSkills();
    paintJobs();
  }

  function paintSkills() {
    const box = $("#tl-skills");
    if (!box || !overview) return;
    const repos = overview.registry?.repos || {};
    const skills = overview.registry?.skills || {};
    const frag = document.createDocumentFragment();
    const names = Object.keys(repos).sort();
    if (!names.length) {
      box.innerHTML = `<div class="tl-empty">Nothing installed yet — paste a
        GitHub URL above. Example: https://github.com/HKUDS/ViMax</div>`;
      return;
    }
    for (const name of names) {
      const repo = repos[name] || {};
      const mySkills = Object.entries(skills)
        .filter(([, s]) => s.repo === name);
      const card = document.createElement("div");
      card.className = "card tl-skill";
      const skillBlocks = mySkills.map(([id, s]) => `
        <div class="tl-skillline">
          <span class="pill ${statusClass(s.status)}">${esc(s.status)}</span>
          <span class="tl-skillname">${esc(id)}</span>
          <span class="tl-detail">${esc(s.detail || "")}</span>
          <button class="btn small" data-act="toggle" data-id="${esc(id)}"
                  data-on="${s.enabled ? "1" : "0"}">
            ${s.enabled ? "Disable" : "Enable"}</button>
          <button class="btn small" data-act="health"
                  data-id="${esc(id)}">Health</button>
        </div>`).join("");
      card.innerHTML = `
        <div class="tl-skillhead">
          <strong>${esc(name)}</strong>
          <span class="pill ${statusClass(repo.status)}">${esc(repo.status || "?")}</span>
          <span class="tl-meta">commit ${esc(String(repo.commit || "").slice(0, 10))}
            · licence ${esc(repo.license || "?")}
            · risk ${esc(repo.risk_level || "low")}</span>
          <span class="tl-spacer"></span>
          <button class="btn small danger" data-act="remove"
                  data-name="${esc(name)}">Remove</button>
        </div>
        <div class="tl-url">${esc(repo.url || "")}</div>
        ${skillBlocks || '<div class="tl-detail">no skill adapter</div>'}
        <div class="tl-result" data-result></div>`;
      card.querySelectorAll("button").forEach((btn) => {
        btn.addEventListener("click", () => act(btn, card));
      });
      frag.append(card);
    }
    box.replaceChildren(frag);
  }

  async function act(btn, card) {
    const resultBox = $("[data-result]", card);
    const kind = btn.dataset.act;
    btn.disabled = true;
    try {
      let res;
      if (kind === "toggle") {
        res = await api(`/api/repo/skills/${btn.dataset.id}/state`, {
          method: "POST",
          body: JSON.stringify({ enabled: btn.dataset.on !== "1" }),
        });
      } else if (kind === "health") {
        resultBox.textContent = "Running health check…";
        res = await api(`/api/repo/skills/${btn.dataset.id}/health`);
        resultBox.textContent = res.body.ok
          ? "✅ health check passed"
          : "❌ health check failed: " + (res.body.error ||
             JSON.stringify(res.body.health || ""));
        load();
        return;
      } else if (kind === "remove") {
        if (!confirm(`Remove ${btn.dataset.name} and its environment from ` +
                     "~/JarvisTools?")) { return; }
        res = await api(`/api/repo/repos/${btn.dataset.name}/remove`, {
          method: "POST", body: JSON.stringify({ confirm: true }),
        });
      }
      if (resultBox) {
        resultBox.textContent = res?.body?.ok
          ? "Done — job queued." : "Error: " + (res?.body?.error || "failed");
      }
      load();
    } finally {
      btn.disabled = false;
    }
  }

  function paintJobs() {
    const box = $("#tl-jobs");
    if (!box || !overview) return;
    const jobs = overview.jobs || [];
    if (!jobs.length) {
      box.innerHTML = `<div class="tl-empty">No jobs yet.</div>`;
      return;
    }
    const rows = jobs.slice(0, 12).map((j) => {
      const pct = Math.round((j.progress || 0) * 100);
      return `
        <tr>
          <td class="tl-mono">${esc(j.job_id)}</td>
          <td>${esc(j.kind)}<span class="tl-detail"> · ${esc(j.ref)}</span></td>
          <td><span class="pill ${statusClass(j.status)}">${esc(j.status)}</span></td>
          <td class="tl-progress">
            <div class="tl-bar"><i style="width:${pct}%"></i></div>
            <span>${pct}%</span>
          </td>
          <td class="tl-msg">${esc(j.message || j.error || "")}</td>
          <td>${(j.status === "QUEUED" || j.status === "RUNNING" ||
                 j.status === "INSTALLING")
            ? `<button class="btn small" data-cancel="${esc(j.job_id)}">Cancel</button>`
            : ""}</td>
        </tr>`;
    }).join("");
    box.innerHTML = `
      <table class="tl-table">
        <thead><tr><th>job</th><th>work</th><th>status</th>
          <th>progress</th><th>message</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    box.querySelectorAll("[data-cancel]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api(`/api/repo/jobs/${btn.dataset.cancel}/cancel`,
                  { method: "POST", body: "{}" });
        load();
      });
    });
  }

  return { build };
})();

window.RIOTools = RIOTools;
