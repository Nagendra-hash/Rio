/* RIO Memory (Phase 6) — six sections over the EXISTING stores:
 *   long_term.json (personal/projects/knowledge), the RAG registry
 *   (documents/knowledge/research) and the conversation journal.
 * Capabilities come from the backend per section — the journal is
 * append-only, so Add/Edit/Remove are never offered there. */
"use strict";

const RIOMemory = (() => {

  let overview = null;      // GET /api/rio/memory
  let section = null;       // current section id
  let rows = [];            // current rows
  let query = "";
  let flash = null;         // {text, kind}
  let busy = false;
  let editingKey = null;    // key being edited (memory rows)
  let editingDoc = null;    // doc_id being retitled (rag rows)
  let adding = false;       // add-form visible?

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
      new Error((j && (j.detail || j.error)) || ("HTTP " + r.status)),
      { code: r.status, body: j });
    return j;
  }

  function say(text, kind) { flash = text ? { text, kind: kind || "ok" } : null; }

  // ── data ──────────────────────────────────────────────────────────────
  async function loadOverview() {
    overview = await api("/api/rio/memory");
    if (!section && overview.sections.length) section = overview.sections[0].id;
  }

  async function loadSection() {
    if (!section) return;
    const q = query.trim();
    const url = `/api/rio/memory/${encodeURIComponent(section)}` +
      (q ? `?q=${encodeURIComponent(q)}` : "");
    const res = await api(url);
    rows = res.rows || [];
  }

  async function refresh() {
    busy = true; paint();
    try {
      await loadOverview();
      await loadSection();
      say(null);
    } catch (e) {
      say(e.code === 401
        ? "Pairing required — open /login in this browser first."
        : `backend unavailable: ${e.message}`, "err");
    }
    busy = false; paint();
  }

  function meta() {
    if (!overview) return null;
    return overview.sections.find(s => s.id === section) || null;
  }

  // ── actions ───────────────────────────────────────────────────────────
  async function choose(id) {
    if (id === section) return;
    section = id; query = ""; editingKey = editingDoc = null; adding = false;
    await refresh();
  }

  async function search() { await refresh(); }

  async function addEntry(payload) {
    busy = true; paint();
    try {
      await api(`/api/rio/memory/${encodeURIComponent(section)}`,
                { method: "POST", body: JSON.stringify(payload) });
      adding = false;
      say("Saved.", "ok");
      await loadOverview(); await loadSection();
    } catch (e) {
      say(e.message, "err");
    }
    busy = false; paint();
  }

  async function editEntry(payload) {
    busy = true; paint();
    try {
      await api(`/api/rio/memory/${encodeURIComponent(section)}`,
                { method: "PUT", body: JSON.stringify(payload) });
      editingKey = editingDoc = null;
      say("Updated.", "ok");
      await loadSection();
    } catch (e) {
      say(e.message, "err");
    }
    busy = false; paint();
  }

  async function removeEntry(payload, label) {
    if (!confirm(`Remove "${label}"? This cannot be undone.`)) return;
    busy = true; paint();
    try {
      await api(`/api/rio/memory/${encodeURIComponent(section)}`,
                { method: "DELETE", body: JSON.stringify(payload) });
      say("Removed.", "ok");
      await loadOverview(); await loadSection();
    } catch (e) {
      say(e.message, "err");
    }
    busy = false; paint();
  }

  // ── shell ─────────────────────────────────────────────────────────────
  function build() {
    const frag = document.createDocumentFragment();
    const root = document.createElement("div");
    root.className = "mem-wrap";
    root.innerHTML = `
      <h1>Memory</h1>
      <p class="sub">What RIO remembers — curated facts, projects, the
        knowledge base and the conversation journal. Every section reads the
        SAME stores the assistant uses; nothing here is a copy.</p>
      <div class="mem-tabs" id="mem-tabs" role="tablist"></div>
      <div class="mem-bar">
        <input id="mem-q" type="search" placeholder="Search this section…"
               aria-label="Search memory section">
        <button class="btn btn-primary" id="mem-search">Search</button>
        <button class="btn" id="mem-add" hidden>+ Add memory</button>
        <button class="btn" id="mem-refresh">↻ Refresh</button>
      </div>
      <div class="mem-hint" id="mem-hint"></div>
      <div class="mem-flash" id="mem-flash" hidden></div>
      <div class="mem-addform" id="mem-addform" hidden></div>
      <div class="mem-list" id="mem-list"></div>`;
    frag.append(root);

    queueMicrotask(() => {
      const $id = k => document.getElementById(k);
      $id("mem-search")?.addEventListener("click", search);
      $id("mem-q")?.addEventListener("keydown",
        e => { if (e.key === "Enter") search(); });
      $id("mem-refresh")?.addEventListener("click", refresh);
      $id("mem-add")?.addEventListener("click", () => {
        adding = !adding; editingKey = editingDoc = null; paint();
      });
      refresh();
    });
    return frag;
  }

  // ── paint ─────────────────────────────────────────────────────────────
  function paint() {
    const root = document.querySelector(".mem-wrap");
    if (!root) return;

    // tabs
    const tabs = document.getElementById("mem-tabs");
    if (tabs && overview) {
      tabs.replaceChildren(...overview.sections.map(s => {
        const b = document.createElement("button");
        b.className = "mem-tab" + (s.id === section ? " active" : "");
        b.setAttribute("role", "tab");
        b.setAttribute("aria-selected", s.id === section ? "true" : "false");
        b.innerHTML = `${esc(s.label)} <b>${s.count}</b>`;
        b.addEventListener("click", () => choose(s.id));
        return b;
      }));
    }

    const m = meta();
    const caps = (m && m.capabilities) || {};

    // buttons / hint
    const addBtn = document.getElementById("mem-add");
    if (addBtn) addBtn.hidden = !caps.add || busy;
    const hint = document.getElementById("mem-hint");
    if (hint && m) {
      const unsupported = Object.entries(caps)
        .filter(([, v]) => !v).map(([k]) => k);
      hint.innerHTML = esc(m.hint) +
        (unsupported.length
          ? ` <span class="mem-caps-note">(not supported here: ${esc(unsupported.join(", "))})</span>`
          : "");
    }

    // flash
    const fb = document.getElementById("mem-flash");
    if (fb) {
      if (!flash) { fb.hidden = true; }
      else {
        fb.hidden = false;
        fb.textContent = flash.text;
        fb.className = "mem-flash fb-" + flash.kind;
      }
    }

    paintAddForm(caps);
    paintList(caps);
  }

  function paintAddForm(caps) {
    const box = document.getElementById("mem-addform");
    if (!box) return;
    if (!adding || !caps.add) { box.hidden = true; box.replaceChildren(); return; }
    box.hidden = false;
    const m = meta();
    const isRag = m && m.source === "rag";
    box.innerHTML = isRag ? `
      <input id="af-title" placeholder="title" aria-label="Title">
      <textarea id="af-text" rows="3" placeholder="text to remember…"
                aria-label="Text"></textarea>
      <div class="mem-af-row">
        <button class="btn btn-primary" id="af-save">Save to knowledge base</button>
        <button class="btn" id="af-cancel">Cancel</button>
      </div>` : `
      <input id="af-key" placeholder="key (e.g. prefers_dark_mode)"
             aria-label="Key">
      <textarea id="af-value" rows="2" placeholder="value (what to remember)"
                aria-label="Value"></textarea>
      <div class="mem-af-row">
        <button class="btn btn-primary" id="af-save">Remember</button>
        <button class="btn" id="af-cancel">Cancel</button>
      </div>`;
    box.querySelector("#af-cancel")?.addEventListener("click", () => {
      adding = false; paint();
    });
    box.querySelector("#af-save")?.addEventListener("click", () => {
      if (isRag) {
        const t = box.querySelector("#af-title").value.trim();
        const v = box.querySelector("#af-text").value.trim();
        if (!t || !v) { say("Title and text are both required.", "warn"); paint(); return; }
        addEntry({ title: t, text: v });
      } else {
        const k = box.querySelector("#af-key").value.trim();
        const v = box.querySelector("#af-value").value.trim();
        if (!k || !v) { say("Key and value are both required.", "warn"); paint(); return; }
        addEntry({ key: k, value: v });
      }
    });
  }

  function paintList(caps) {
    const list = document.getElementById("mem-list");
    if (!list) return;
    if (busy) {
      list.innerHTML = `<div class="empty">Loading…</div>`;
      return;
    }
    if (!rows.length) {
      list.innerHTML = `<div class="empty">${
        query.trim()
          ? `Nothing in this section matches “${esc(query.trim())}”.`
          : "Nothing stored here yet."}</div>`;
      return;
    }
    list.replaceChildren(...rows.map(r => rowEl(r, caps)));
  }

  function rowEl(r, caps) {
    const el = document.createElement("div");
    el.className = "mem-row";
    const isRagRow = r.source === "rag" || r.source === "rag_hit";
    const isJournal = r.source === "journal";

    if (isRagRow && r.source === "rag") {
      // document entry: edit title / remove doc
      if (editingDoc === r.doc_id) {
        el.innerHTML = `
          <div class="mem-edit">
            <input class="mem-e-title" value="${esc(r.key)}" aria-label="New title">
            <div class="mem-af-row">
              <button class="btn btn-primary mem-e-save">Save</button>
              <button class="btn mem-e-cancel">Cancel</button>
            </div>
          </div>`;
        el.querySelector(".mem-e-save").addEventListener("click", () => {
          const t = el.querySelector(".mem-e-title").value.trim();
          if (!t) return;
          editEntry({ doc_id: r.doc_id, title: t });
        });
        el.querySelector(".mem-e-cancel").addEventListener("click", () => {
          editingDoc = null; paint();
        });
        return el;
      }
      el.innerHTML = `
        <div class="mem-main">
          <span class="mem-key">${esc(r.key)}</span>
          <span class="mem-tag">${esc(r.category || "")}</span>
        </div>
        <div class="mem-val">${esc(r.value)}</div>
        <div class="mem-meta">
          <span>${esc(r.updated || "")}</span>
          <span class="mem-actions">
            ${caps.view ? `<button class="btn btn-mini mem-open">Open</button>` : ""}
            ${caps.edit ? `<button class="btn btn-mini mem-edit-b">Edit</button>` : ""}
            ${caps.remove ? `<button class="btn btn-mini mem-del">Remove</button>` : ""}
          </span>
        </div>`;
      el.querySelector(".mem-open")?.addEventListener("click", () => {
        window.open(`/api/rio/files/raw?doc_id=${encodeURIComponent(r.doc_id)}` +
                    `&token=${encodeURIComponent(token())}`, "_blank");
      });
      el.querySelector(".mem-edit-b")?.addEventListener("click", () => {
        editingDoc = r.doc_id; editingKey = null; paint();
      });
      el.querySelector(".mem-del")?.addEventListener("click", () =>
        removeEntry({ doc_id: r.doc_id }, r.key));
      return el;
    }

    if (isRagRow) {
      // semantic search hit
      el.className += " mem-hit";
      el.innerHTML = `
        <div class="mem-main">
          <span class="mem-key">${esc(r.key)}</span>
          <span class="mem-tag">${esc(r.category || "")}${
            r.page ? ` · p.${r.page}` : ""}${
            r.score != null ? ` · score ${r.score}` : ""}</span>
        </div>
        <div class="mem-val">${esc(r.value)}</div>`;
      return el;
    }

    // memory entry / journal turn
    if (isJournal) {
      el.className += " mem-journal";
      el.innerHTML = `
        <div class="mem-main">
          <span class="mem-key">${esc(r.key)}</span>
          <span class="mem-tag">${esc(r.updated || "")}</span>
        </div>
        <div class="mem-val">${esc(r.value)}</div>`;
      return el;
    }

    if (editingKey === r.key) {
      el.innerHTML = `
        <div class="mem-edit">
          <input class="mem-e-val" rows="2" value="${esc(r.value)}"
                 aria-label="New value">
          <div class="mem-af-row">
            <button class="btn btn-primary mem-e-save">Save</button>
            <button class="btn mem-e-cancel">Cancel</button>
          </div>
        </div>`;
      el.querySelector(".mem-e-save").addEventListener("click", () => {
        const v = el.querySelector(".mem-e-val").value.trim();
        if (!v) return;
        editEntry({ key: r.key, value: v });
      });
      el.querySelector(".mem-e-cancel").addEventListener("click", () => {
        editingKey = null; paint();
      });
      return el;
    }

    el.innerHTML = `
      <div class="mem-main">
        <span class="mem-key">${esc(r.key)}</span>
        <span class="mem-tag">${esc(r.category || "")}</span>
      </div>
      <div class="mem-val">${esc(r.value)}</div>
      <div class="mem-meta">
        <span>${esc(r.updated || "")}</span>
        <span class="mem-actions">
          ${caps.edit ? `<button class="btn btn-mini mem-edit-b">Edit</button>` : ""}
          ${caps.remove ? `<button class="btn btn-mini mem-del">Remove</button>` : ""}
        </span>
      </div>`;
    el.querySelector(".mem-edit-b")?.addEventListener("click", () => {
      editingKey = r.key; editingDoc = null; paint();
    });
    el.querySelector(".mem-del")?.addEventListener("click", () =>
      removeEntry({ key: r.key }, r.key));
    return el;
  }

  return { build };
})();

window.RIOMemory = RIOMemory;
