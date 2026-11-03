/* RIO Files (Phase 6) — uploads + the EXISTING knowledge base.
 * RAG dashboard (Documents · Chunks · Embeddings · Vector DB · Indexing ·
 * Last indexed) is measured live and shows the literal "N/A" whenever the
 * backend cannot measure something. Upload runs the real workflow
 * Upload → Validate → Process → Extract → Index → Available to RIO and
 * reports every stage. Actions are offered ONLY when the backend reports
 * them for that file (GET /api/rio/files → actions[]). */
"use strict";

const RIOFiles = (() => {

  let dash = null;        // GET /api/rio/rag
  let files = null;       // GET /api/rio/files
  let busy = false;
  let flash = null;       // {text, kind}
  let stages = null;      // last upload workflow report
  let answer = null;      // last analyze/summarize/ask answer
  let asking = null;      // doc_id currently showing the Ask box
  let adding = null;      // doc_id currently showing the Add-to-Memory box

  function token() {
    try { return sessionStorage.getItem("jarvis_token") || ""; } catch (_) { return ""; }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function api(path, opts = {}) {
    const r = await fetch(path, Object.assign({
      headers: { "Authorization": `Bearer ${token()}`,
                 "Content-Type": "application/json" },
      cache: "no-store",
    }, opts));
    let j = null;
    try { j = await r.json(); } catch (_) { /* empty */ }
    if (r.status === 401) throw Object.assign(new Error("pairing required"), { code: 401 });
    if (!r.ok) throw Object.assign(
      new Error((j && (j.error || j.detail)) || (`HTTP ${r.status}`)),
      { code: r.status, body: j });
    return j;
  }

  function say(text, kind) { flash = text ? { text, kind: kind || "ok" } : null; }

  // ── data ──────────────────────────────────────────────────────────────
  async function refresh() {
    busy = true; paint();
    try {
      dash = await api("/api/rio/rag");
    } catch (e) {
      dash = null;
      say(e.code === 401
        ? "Pairing required — open /login in this browser first."
        : `RAG dashboard unavailable: ${e.message}`, "err");
    }
    try {
      files = await api("/api/rio/files");
    } catch (e) {
      files = null;
      if (!flash) say(`files unavailable: ${e.message}`, "err");
    }
    busy = false; paint();
  }

  // ── upload workflow ───────────────────────────────────────────────────
  async function upload() {
    const box = document.getElementById("fl-form");
    if (!box) return;
    const input = box.querySelector("#fl-file");
    const file = input && input.files && input.files[0];
    if (!file) { say("Choose a file first.", "warn"); paint(); return; }
    const coll = box.querySelector("#fl-coll")?.value || "personal_knowledge";
    const title = box.querySelector("#fl-title")?.value || "";
    const fd = new FormData();
    fd.append("file", file, file.name);
    fd.append("collection", coll);
    if (title) fd.append("title", title);

    busy = true; stages = null; answer = null; paint();
    try {
      // no Content-Type header — the browser sets the multipart boundary
      const r = await fetch("/api/rio/files/upload", {
        method: "POST",
        headers: { "Authorization": `Bearer ${token()}` },
        body: fd, cache: "no-store",
      });
      let j = null;
      try { j = await r.json(); } catch (_) { /* empty */ }
      if (r.status === 401) throw Object.assign(new Error("pairing required"), { code: 401 });
      if (!j) throw Object.assign(new Error(`HTTP ${r.status}`), { code: r.status });
      stages = { ok: !!j.ok, stages: j.stages || [],
                 error: j.error || "", name: j.name || file.name,
                 doc_id: j.doc_id || null, collection: j.collection || coll };
      say(j.ok
        ? `Indexed — ${j.chunks ?? "?"} chunk(s) in ${j.collection}.`
        : (j.error || "upload failed."), j.ok ? "ok" : "err");
      if (j.ok && input) input.value = "";
    } catch (e) {
      say(e.message, "err");
    }
    busy = false;
    await refresh();
  }

  // ── per-file actions ──────────────────────────────────────────────────
  async function doAction(action, row, extra = {}) {
    const payload = Object.assign({ action }, extra);
    if (row.doc_id) payload.doc_id = row.doc_id;
    if (!payload.doc_id && row.name) payload.name = row.name;
    busy = true; answer = null; paint();
    try {
      const res = await api("/api/rio/files/action", {
        method: "POST", body: JSON.stringify(payload),
      });
      if (action === "open") {
        window.open(`${res.url}&token=${encodeURIComponent(token())}`, "_blank");
        say("Opened.", "ok");
      } else if (action === "delete") {
        say(`Removed: ${(res.removed || []).join(", ")}.`, "ok");
        asking = adding = null;
      } else if (action === "add_to_memory") {
        say(res.message || `Remembered as “${res.key}”.`, "ok");
        adding = null;
      } else {
        answer = { action, title: row.title || row.name || row.doc_id,
                   text: res.answer || "", citations: res.citations || [],
                   risk: res.hallucination_risk };
        asking = null;
        say(`${action} completed.`, "ok");
      }
    } catch (e) {
      const det = e.body && (e.body.detail || e.body.error);
      say(`${action}: ${det || e.message}`, "err");
      if (e.code !== 409) asking = adding = null;
    }
    busy = false;
    await refresh();
  }

  function askQuestion(row) {
    const box = document.getElementById(`ask-${cssId(row)}`);
    const q = box && box.value.trim();
    if (!q) { say("Type a question first.", "warn"); paint(); return; }
    doAction("ask", row, { question: q });
  }

  function rememberDoc(row) {
    const box = document.getElementById(`mem-${cssId(row)}`);
    const key = box && box.querySelector(".fl-m-key")?.value.trim();
    const value = box && box.querySelector(".fl-m-val")?.value.trim();
    const extra = {};
    if (key) extra.key = key;
    if (value) extra.value = value;
    doAction("add_to_memory", row, extra);
  }

  function cssId(row) {
    return String(row.doc_id || row.name || "").replace(/[^\w.-]/g, "_");
  }

  // ── shell ─────────────────────────────────────────────────────────────
  function build() {
    const frag = document.createDocumentFragment();
    const root = document.createElement("div");
    root.className = "fl-wrap";
    root.innerHTML = `
      <h1>Files</h1>
      <p class="sub">Upload → Validate → Process → Extract → Index → Available
        to RIO, backed by the SAME knowledge base the assistant and Telegram
        already use. Actions appear only when the backend supports them.</p>

      <h2>RAG dashboard</h2>
      <div class="fl-dash" id="fl-dash"></div>

      <h2>Upload</h2>
      <form class="fl-form" id="fl-form" novalidate>
        <input id="fl-file" type="file" aria-label="File to upload">
        <select id="fl-coll" aria-label="Collection"></select>
        <input id="fl-title" type="text" placeholder="title (optional)"
               aria-label="Title">
        <button class="btn btn-primary" id="fl-go" type="submit">Upload</button>
      </form>
      <div class="fl-hint" id="fl-supported"></div>
      <div class="mem-flash" id="fl-flash" hidden></div>
      <div class="fl-stages" id="fl-stages" hidden></div>
      <div class="fl-answer" id="fl-answer" hidden></div>

      <h2>Library</h2>
      <div class="fl-bar">
        <button class="btn" id="fl-refresh">↻ Refresh</button>
      </div>
      <div class="fl-list" id="fl-list"></div>`;
    frag.append(root);

    queueMicrotask(() => {
      document.getElementById("fl-form")?.addEventListener("submit", e => {
        e.preventDefault(); upload();
      });
      document.getElementById("fl-refresh")?.addEventListener("click", refresh);
      refresh();
    });
    return frag;
  }

  // ── paint ─────────────────────────────────────────────────────────────
  function paint() {
    const root = document.querySelector(".fl-wrap");
    if (!root) return;

    paintDash();
    paintForm();

    const fb = document.getElementById("fl-flash");
    if (fb) {
      if (!flash) { fb.hidden = true; }
      else {
        fb.hidden = false;
        fb.textContent = flash.text;
        fb.className = "mem-flash fb-" + flash.kind;
      }
    }
    paintStages();
    paintAnswer();
    paintList();
  }

  function paintDash() {
    const box = document.getElementById("fl-dash");
    if (!box) return;
    const na = "N/A";
    const d = dash || {};
    const cards = [
      ["Documents",        d.documents],
      ["Chunks",           d.chunks],
      ["Embeddings",       d.embeddings],
      ["Vector database",  d.vector_database],
      ["Indexing status",  d.indexing_status],
      ["Last indexed",     d.last_indexed],
    ];
    if (busy && !dash) { box.innerHTML = `<div class="empty">Loading…</div>`; return; }
    box.replaceChildren(...cards.map(([label, val]) => {
      const c = document.createElement("div");
      c.className = "fl-card";
      const v = val == null || val === "" ? na : String(val);
      const isNa = v.startsWith("N/A");
      c.innerHTML = `<span class="fl-card-label">${esc(label)}</span>
                     <span class="fl-card-val${isNa ? " is-na" : ""}">${esc(v)}</span>`;
      return c;
    }));
  }

  function paintForm() {
    const sup = document.getElementById("fl-supported");
    if (sup) {
      sup.textContent = files && files.supported
        ? `Accepted types: ${files.supported.join(", ")}`
        : "Accepted types: loading…";
    }
    const sel = document.getElementById("fl-coll");
    if (sel && files && Array.isArray(files.collections) && files.collections.length) {
      const cur = sel.value;
      const want = files.collections.slice();
      if (cur && !want.includes(cur)) want.unshift(cur);
      sel.replaceChildren(...want.map(c => {
        const o = document.createElement("option");
        o.value = c; o.textContent = c;
        return o;
      }));
      if (cur) sel.value = cur;
    }
    const go = document.getElementById("fl-go");
    if (go) go.disabled = !!busy;
  }

  function paintStages() {
    const box = document.getElementById("fl-stages");
    if (!box) return;
    if (!stages) { box.hidden = true; box.replaceChildren(); return; }
    box.hidden = false;
    box.innerHTML = `
      <div class="fl-stages-title">${stages.ok ? "✓ Workflow complete" : "✗ Workflow failed"} —
        <code>${esc(stages.name)}</code>${stages.doc_id
          ? ` · doc <code>${esc(stages.doc_id)}</code>` : ""}</div>
      <ol class="fl-stage-list">${stages.stages.map(s => `
        <li class="fl-stage ${s.ok ? "ok" : "bad"}">
          <span class="fl-stage-mark">${s.ok ? "✓" : "✗"}</span>
          <span class="fl-stage-step">${esc(s.step)}</span>
          <span class="fl-stage-detail">${esc(s.detail || "")}</span>
        </li>`).join("")}</ol>
      ${stages.error && !stages.ok
        ? `<div class="fl-stage-error">${esc(stages.error)}</div>` : ""}`;
  }

  function paintAnswer() {
    const box = document.getElementById("fl-answer");
    if (!box) return;
    if (!answer) { box.hidden = true; box.replaceChildren(); return; }
    box.hidden = false;
    box.innerHTML = `
      <div class="fl-ans-head">
        <strong>${esc(answer.action)} · ${esc(answer.title || "")}</strong>
        ${answer.risk ? `<span class="mem-tag">risk: ${esc(answer.risk)}</span>` : ""}
        <button class="btn btn-mini" id="fl-ans-close">close</button>
      </div>
      <div class="fl-ans-text">${esc(answer.text)}</div>
      ${answer.citations.length
        ? `<div class="fl-ans-cites">${answer.citations.map(c =>
            `· ${esc(c.source || c.doc_id || "")}${c.page ? ` p.${c.page}` : ""}`
          ).join("  ")}</div>` : ""}`;
    box.querySelector("#fl-ans-close")?.addEventListener("click", () => {
      answer = null; paint();
    });
  }

  function paintList() {
    const list = document.getElementById("fl-list");
    if (!list) return;
    if (busy && !files) { list.innerHTML = `<div class="empty">Loading…</div>`; return; }
    if (!files) { list.innerHTML = `<div class="empty">File list unavailable.</div>`; return; }

    const uploads = files.uploads || [];
    const docs = files.documents || [];
    const seenDocs = new Set(uploads.filter(u => u.doc_id).map(u => u.doc_id));
    const orphans = docs.filter(d => !d.in_uploads || !seenDocs.has(d.doc_id));

    const groups = [];
    if (uploads.length) groups.push(["Uploads", uploads.map(u => ({
      title: u.name,
      meta: `${fmtSize(u.size)}${u.indexed ? " · indexed" : " · not indexed"}${
        u.collection ? " · " + u.collection : ""}`,
      row: u,
    }))]);
    if (orphans.length) groups.push(["Knowledge base only", orphans.map(d => ({
      title: d.title || d.doc_id,
      meta: `${d.filetype || "?"} · ${d.pages ?? "?"} pages · ${d.chunks ?? "?"} chunks` +
            ` · ${d.status || "?"} · ${d.collection || "?"} · ${d.ingested_at || ""}`,
      row: d,
    }))]);

    if (!groups.length) {
      list.innerHTML = `<div class="empty">No uploads and no indexed documents yet — upload one above.</div>`;
      return;
    }

    list.replaceChildren(...groups.map(([label, items]) => {
      const g = document.createElement("section");
      g.className = "fl-group";
      g.innerHTML = `<h3>${esc(label)} <b>${items.length}</b></h3>`;
      g.append(...items.map(it => fileEl(it, files)));
      return g;
    }));
  }

  function fileEl(it, meta) {
    const row = it.row;
    const acts = Array.isArray(row.actions) ? row.actions : [];
    const id = cssId(row);
    const el = document.createElement("div");
    el.className = "mem-row fl-file";
    el.innerHTML = `
      <div class="mem-main">
        <span class="mem-key">${esc(it.title)}</span>
        <span class="mem-tag">${acts.length ? "" : "no actions supported"}</span>
      </div>
      <div class="mem-val">${esc(it.meta)}</div>
      <div class="mem-meta"><span class="mem-actions">${
        acts.map(a => `<button class="btn btn-mini fl-a" data-a="${esc(a)}">${
          LABELS[a] || a}</button>`).join("")
      }</span></div>
      <div class="fl-inline" id="ask-${id}" hidden>
        <input type="text" placeholder="Ask RIO about this file…"
               aria-label="Question">
        <button class="btn btn-primary btn-mini fl-a-ask">Ask</button>
      </div>
      <div class="fl-inline" id="mem-${id}" hidden>
        <input class="fl-m-key" type="text" placeholder="key (optional)" aria-label="Key">
        <input class="fl-m-val" type="text" placeholder="value (optional)" aria-label="Value">
        <button class="btn btn-primary btn-mini fl-a-mem">Remember</button>
      </div>`;

    el.querySelectorAll(".fl-a").forEach(btn => {
      btn.addEventListener("click", () => {
        const a = btn.dataset.a;
        if (a === "ask") {
          asking = asking === id ? null : id; adding = null;
          el.querySelector(`#ask-${CSS.escape(id)}`).hidden = asking !== id;
          el.querySelector(`#mem-${CSS.escape(id)}`).hidden = true;
          if (asking) el.querySelector(`#ask-${CSS.escape(id)} input`)?.focus();
          return;
        }
        if (a === "add_to_memory") {
          adding = adding === id ? null : id; asking = null;
          el.querySelector(`#mem-${CSS.escape(id)}`).hidden = adding !== id;
          el.querySelector(`#ask-${CSS.escape(id)}`).hidden = true;
          if (adding) el.querySelector(`#mem-${CSS.escape(id)} .fl-m-key`)?.focus();
          return;
        }
        if (a === "delete") {
          if (!confirm(`Delete "${it.title}"? This removes it from the file store and the knowledge base.`)) return;
          doAction("delete", row);
          return;
        }
        doAction(a, row);
      });
    });
    el.querySelector(".fl-a-ask")?.addEventListener("click", () => askQuestion(row));
    el.querySelector(`#ask-${CSS.escape(id)} input`)?.addEventListener(
      "keydown", e => { if (e.key === "Enter") askQuestion(row); });
    el.querySelector(".fl-a-mem")?.addEventListener("click", () => rememberDoc(row));
    return el;
  }

  const LABELS = {
    open: "Open", analyze: "Analyze", summarize: "Summarize",
    ask: "Ask RIO", add_to_memory: "Add to Memory", delete: "Delete",
  };

  function fmtSize(n) {
    if (typeof n !== "number" || !isFinite(n)) return "?";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1048576).toFixed(1)} MB`;
  }

  return { build };
})();

window.RIOFiles = RIOFiles;
