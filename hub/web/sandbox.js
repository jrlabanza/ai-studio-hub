/* Sandbox: people upload what they built with an AI (a web page, a script, a document), change it here and publish
   it under a link of its own. The pages themselves are only ever shown from the Sandbox port (a different origin),
   inside a sandboxed frame - never on the hub's own origin. Routes: #/sandbox, #/sandbox/new, #/sandbox/p/<id>/<tab>. */
(function () {
  const $ = (q, el = document) => el.querySelector(q);
  const $$ = (q, el = document) => Array.from(el.querySelectorAll(q));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = { get: (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private window */ } } };
  const hub = () => window.Hub || {};
  const toast = (...a) => (hub().toast ? hub().toast(...a) : console.log(...a));
  const ago = (ts) => (hub().fmtAgo ? hub().fmtAgo(ts) : new Date(ts * 1000).toLocaleString());
  const when = (ts) => ts ? new Date(ts * 1000).toLocaleString([], { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
  const size = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
  const KIND = { web: "Web app", script: "Script", document: "Document" };
  const KIND_HELP = { web: "Runs in the browser from its index.html", script: "Shared to read and download - not run on the server", document: "Files to read, view and download" };
  const COLOR = "#63C5DA";

  const SB = { state: null, list: [], filter: { q: "", kind: "", status: "active" }, d: null, tab: "files",
               file: null, dirty: false, pending: [], route: "", timer: null, slugTimer: null };

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: {} };
    if (opts.body !== undefined) { init.headers["content-type"] = "application/json"; init.body = JSON.stringify(opts.body); }
    const r = await fetch(path, init);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `${r.status} ${r.statusText}`);
    return d;
  }
  const author = () => store.get("sb.author", "");
  const needAuthor = () => {
    const a = author().trim();
    if (!a) { toast("Enter your name first", "warn", "It is recorded with every change (the hub has no accounts)."); askAuthor(); }
    return a;
  };

  // ------------------------------------------------------------------ uploads: one "bundle" request
  // [4-byte header length][JSON header {files: [{path, size}], ...fields}][file bytes ...] - see hub/sandbox/intake.py
  function bundle(fields, items) {
    const head = new TextEncoder().encode(JSON.stringify({ ...fields, files: items.map((i) => ({ path: i.path, size: i.file.size })) }));
    const len = new Uint8Array(4); new DataView(len.buffer).setUint32(0, head.length);
    return new Blob([len, head, ...items.map((i) => i.file)]);
  }
  function send(url, blob, onProgress) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open("POST", url);
      x.setRequestHeader("content-type", "application/octet-stream");
      x.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      x.onload = () => { let d = {}; try { d = JSON.parse(x.responseText); } catch { /* empty */ }
        if (x.status >= 200 && x.status < 300) resolve(d); else reject(new Error(d.detail || `${x.status}`)); };
      x.onerror = () => reject(new Error("The upload did not reach the hub"));
      x.send(blob);
    });
  }
  // Files from a drop (folders included, walked recursively) or a file input, with their relative paths.
  async function droppedItems(dt) {
    const out = [];
    const walk = (entry, prefix) => new Promise((resolve) => {
      if (entry.isFile) entry.file((f) => { out.push({ path: prefix + f.name, file: f }); resolve(); }, () => resolve());
      else if (entry.isDirectory) {
        const reader = entry.createReader(), all = [];
        const more = () => reader.readEntries(async (batch) => {
          if (!batch.length) { for (const e of all) await walk(e, prefix + entry.name + "/"); resolve(); }
          else { all.push(...batch); more(); }
        }, () => resolve());
        more();
      } else resolve();
    });
    const entries = Array.from(dt.items || []).map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    if (entries.length) { for (const e of entries) await walk(e, ""); }
    else Array.from(dt.files || []).forEach((f) => out.push({ path: f.name, file: f }));
    return out;
  }
  const inputItems = (input) => Array.from(input.files || []).map((f) => ({ path: f.webkitRelativePath || f.name, file: f }));

  function copy(text) {
    const ok = () => toast("Copied", "ok", text, 2500);
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).then(ok, () => fallback());
    fallback();
    function fallback() { const a = document.createElement("textarea"); a.value = text; document.body.appendChild(a); a.select(); try { document.execCommand("copy"); ok(); } catch { toast("Select the address and copy it", "warn"); } a.remove(); }
  }
  const shareUrl = (slug, v) => `${(SB.d ? SB.d.bases : SB.state.bases).share}/${slug}${v ? `@v${v}` : ""}/`;

  // ------------------------------------------------------------------ routing
  function root() { return $("#view-sandbox"); }
  async function show(hash) {
    SB.route = hash;
    const parts = hash.replace(/^#\/sandbox\/?/, "").split("/").filter(Boolean);
    try { if (!SB.state) SB.state = await api("/api/sandbox/state"); } catch (e) { root().innerHTML = `<div class="empty">The Sandbox is not answering: ${esc(e.message)}</div>`; return; }
    if (parts[0] === "new") return renderNew();
    if (parts[0] === "p" && parts[1]) return openProject(parts[1], parts[2] || "files");
    return renderGallery();
  }
  function confirmLeave(hash) {
    if (!SB.dirty || !SB.d) return true;
    if (hash.startsWith(`#/sandbox/p/${SB.d.project.id}`)) return true;      // switching tabs keeps the editor's text
    if (!confirm(`${SB.file ? SB.file.path : "A file"} has unsaved changes. Leave and lose them?`)) return false;
    SB.dirty = false;
    return true;
  }
  window.addEventListener("beforeunload", (e) => { if (SB.dirty) { e.preventDefault(); e.returnValue = ""; } });

  function askAuthor(then) {
    hub().openModal(`<div class="eyebrow">Sandbox</div><h3 class="serif" style="font-size:26px;margin:4px 0 6px">Your name</h3>
      <p class="lead">Recorded with every upload, edit, publish and link - the hub has no accounts. This browser remembers it.</p>
      <div class="sb-form"><input id="sbWho" type="text" maxlength="60" value="${esc(author())}" placeholder="e.g. Jorelle Labanza" autocomplete="name">
      <div class="sb-row"><button class="btn primary" id="sbWhoOk">Save</button></div></div>`, true);
    const i = $("#sbWho"); i.focus();
    const ok = () => { const v = i.value.trim(); if (!v) return; store.set("sb.author", v); hub().closeModal(); paintAuthor(); if (then) then(); };
    $("#sbWhoOk").onclick = ok; i.onkeydown = (e) => { if (e.key === "Enter") ok(); };
  }
  function paintAuthor() { $$(".sb-author-name").forEach((el) => { el.textContent = author() || "set your name"; }); }
  const authorChip = () => `<button class="chip sb-author" data-sb="author" title="Your name, recorded with every change"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg><span class="sb-author-name">${esc(author() || "set your name")}</span></button>`;
  function networkNote(b) {
    if (b.public_url) return "";
    if (b.local_only) return `<div class="note warn sb-note">Links work on this PC only: the hub listens on 127.0.0.1. To share them, set <b>Settings → Network → Everyone on my network</b>, restart the hub and open port ${b.port} in the firewall.</div>`;
    return "";
  }

  // ------------------------------------------------------------------ gallery
  async function renderGallery() {
    SB.d = null; SB.file = null; SB.dirty = false;
    const f = SB.filter;
    root().innerHTML = `<div class="sb-gallery">
      <div class="lib-bar">
        <div class="chips" id="sbKinds">${[["", "All"], ["web", "Web apps"], ["script", "Scripts"], ["document", "Documents"]].map(([k, l]) => `<button class="chip filter ${f.kind === k ? "active" : ""}" data-kind="${k}">${l}</button>`).join("")}
          <button class="chip filter ${f.status === "archived" ? "active" : ""}" data-arch="1" title="Archived projects: read-only, their links keep working">Archived</button></div>
        <div class="lib-search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><input id="sbSearch" type="search" placeholder="Search names, authors, links…" value="${esc(f.q)}"></div>
        <span class="lib-count" id="sbCount"></span>
        ${authorChip()}
        <a class="btn primary" href="#/sandbox/new"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>New project</a>
      </div>
      ${networkNote(SB.state.bases)}
      <div class="sb-layout"><div class="sb-grid" id="sbGrid"><div class="empty">Loading…</div></div>
        <aside class="card sb-activity"><div class="card-head"><div><div class="eyebrow">Every project</div><h3>Sandbox activity</h3></div></div><ul class="sb-events" id="sbEvents"><li class="empty small">Loading…</li></ul></aside></div>
    </div>`;
    $("#sbKinds").onclick = (e) => { const c = e.target.closest(".chip"); if (!c) return; if (c.dataset.arch) f.status = f.status === "archived" ? "active" : "archived"; else f.kind = c.dataset.kind; renderGallery(); };
    $("#sbSearch").oninput = (e) => { clearTimeout(SB.timer); SB.timer = setTimeout(() => { f.q = e.target.value.trim(); loadGallery(); }, 250); };
    root().querySelector("[data-sb=author]").onclick = () => askAuthor();
    loadGallery();
    api("/api/sandbox/events?limit=60").then((d) => { $("#sbEvents").innerHTML = eventRows(d.events, true) || `<li class="empty small">Nothing yet.</li>`; }).catch(() => {});
  }
  async function loadGallery() {
    const f = SB.filter;
    let d;
    try { d = await api(`/api/sandbox/projects?${new URLSearchParams({ q: f.q, kind: f.kind, status: f.status })}`); } catch (e) { toast(e.message, "error"); return; }
    SB.list = d.projects;
    const grid = $("#sbGrid"); if (!grid) return;
    $("#sbCount").textContent = `${d.projects.length} project${d.projects.length === 1 ? "" : "s"}`;
    grid.innerHTML = d.projects.map((p) => card(p, d.bases)).join("") || `<div class="sb-empty card"><div class="sb-empty-ico">${ICON_BOX}</div><h3>${f.q || f.kind || f.status === "archived" ? "Nothing matches" : "Nothing in the Sandbox yet"}</h3>
      <p class="lead">Built a page, a tool or a script with ChatGPT, Gemini or another AI and not sure where to put it? Upload it here, change it in the browser, then publish a link the whole team can open.</p>
      <a class="btn primary" href="#/sandbox/new">Upload your first project</a></div>`;
  }
  const ICON_BOX = `<svg viewBox="0 0 24 24"><path d="M4 9.5 12 5l8 4.5v5L12 19l-8-4.5z"/><path d="m4 9.5 8 4.5 8-4.5"/><path d="M12 14v5"/></svg>`;
  const KIND_ICON = { web: `<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18"/><circle cx="6.5" cy="6.5" r=".6"/><circle cx="9" cy="6.5" r=".6"/></svg>`,
    script: `<svg viewBox="0 0 24 24"><path d="m8 8-4 4 4 4"/><path d="m16 8 4 4-4 4"/><path d="m13.5 5-3 14"/></svg>`,
    document: `<svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 16h6"/></svg>` };
  function card(p, b) {
    const links = p.links.map((l) => `<span class="sb-slug">/${esc(l.slug)}${l.pin ? `<em>@v${l.pin}</em>` : ""}</span>`).join("");
    return `<a class="card sb-card" href="#/sandbox/p/${p.id}/files" style="--tool:${COLOR}">
      <div class="sb-card-head"><span class="sb-kind-ico" data-kind="${p.kind}">${KIND_ICON[p.kind] || ICON_BOX}</span>
        <div class="sb-card-title"><h3>${esc(p.name)}</h3><small>${esc(KIND[p.kind] || p.kind)} · by ${esc(p.author)}</small></div>
        ${p.latest ? `<span class="pill ready">v${p.latest}</span>` : `<span class="pill">draft</span>`}</div>
      ${p.description ? `<p class="sb-desc">${esc(p.description)}</p>` : ""}
      <div class="sb-links">${links || `<span class="sb-muted">No link yet</span>`}</div>
      <div class="sb-card-foot"><span title="Views of all its links">${p.views} view${p.views === 1 ? "" : "s"}</span><span>updated ${esc(ago(p.updated))}</span></div></a>`;
  }

  // ------------------------------------------------------------------ new project
  function renderNew() {
    SB.d = null; SB.pending = [];
    root().innerHTML = `<div class="sb-new">
      <a class="btn ghost small sb-back" href="#/sandbox">← Sandbox</a>
      <div class="card sb-new-card">
        <div class="eyebrow">New project</div><h2 class="serif sb-h2">Upload what you built</h2>
        <p class="lead">A web page or app (index.html plus its files), a script (Python, JavaScript, a notebook…) or documents. One file, several, a folder or a .zip - up to ${size(SB.state.max_bytes)} and ${SB.state.max_files} files.</p>
        <label class="sb-drop" id="sbDrop">
          <input type="file" id="sbFiles" multiple hidden>
          <div class="sb-drop-ico">${ICON_BOX}</div>
          <b>Drop files, a folder or a .zip here</b>
          <span>or <u>choose files</u> · <button type="button" class="linkish" id="sbPickFolder">choose a folder</button></span>
          <input type="file" id="sbFolder" webkitdirectory hidden>
        </label>
        <div class="sb-picked" id="sbPicked" hidden></div>
        <div class="sb-form">
          <label>Name<input id="sbName" type="text" maxlength="80" placeholder="e.g. Quote calculator"></label>
          <label>What is it?<textarea id="sbDesc" rows="3" maxlength="2000" placeholder="One or two lines for the people you share it with - what it does, which AI helped build it."></textarea></label>
          <label>Your name<input id="sbAuthor" type="text" maxlength="60" value="${esc(author())}" placeholder="Recorded with the project"></label>
          <div class="sb-row"><button class="btn primary" id="sbCreate">Create project</button><span class="sb-muted" id="sbProgress"></span></div>
          <p class="sb-muted small">Nothing is public until you publish. Web pages run in a locked-down sandbox on their own address; scripts are shared to read and download and never run on the server.</p>
        </div>
      </div></div>`;
    const drop = $("#sbDrop");
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
    drop.ondragleave = () => drop.classList.remove("over");
    drop.ondrop = async (e) => { e.preventDefault(); drop.classList.remove("over"); picked(await droppedItems(e.dataTransfer)); };
    $("#sbFiles").onchange = (e) => picked(inputItems(e.target));
    $("#sbPickFolder").onclick = (e) => { e.preventDefault(); e.stopPropagation(); $("#sbFolder").click(); };
    $("#sbFolder").onchange = (e) => picked(inputItems(e.target));
    $("#sbCreate").onclick = create;
  }
  function picked(items) {
    SB.pending = items;
    const total = items.reduce((a, i) => a + i.file.size, 0);
    const box = $("#sbPicked");
    box.hidden = !items.length;
    box.innerHTML = `<b>${items.length} file${items.length === 1 ? "" : "s"} · ${size(total)}</b>${items.length === 1 && /\.zip$/i.test(items[0].path) ? " · the zip is unpacked and checked on the hub" : ""}
      <ul>${items.slice(0, 12).map((i) => `<li class="mono">${esc(i.path)}<em>${size(i.file.size)}</em></li>`).join("")}${items.length > 12 ? `<li class="sb-muted">… and ${items.length - 12} more</li>` : ""}</ul>`;
    if (total > SB.state.max_bytes) toast(`That is ${size(total)} - the limit is ${size(SB.state.max_bytes)}`, "warn");
    const name = $("#sbName");
    if (!name.value && items.length) {
      const first = items[0].path;
      const base = first.includes("/") ? first.split("/")[0] : first.replace(/\.[^.]+$/, "");
      name.value = base.replace(/[-_]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()).slice(0, 80);
    }
  }
  async function create() {
    const name = $("#sbName").value.trim(), who = $("#sbAuthor").value.trim();
    if (!name) { toast("Give the project a name", "warn"); $("#sbName").focus(); return; }
    if (!who) { toast("Enter your name", "warn", "It is recorded with the project."); $("#sbAuthor").focus(); return; }
    store.set("sb.author", who);
    const btn = $("#sbCreate"); btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Uploading…`;
    try {
      const d = await send("/api/sandbox/projects", bundle({ name, description: $("#sbDesc").value, author: who }, SB.pending),
        (f) => { $("#sbProgress").textContent = `${Math.round(f * 100)}%`; });
      toast(`${d.project.name} created`, "ok", `${d.files.length} file${d.files.length === 1 ? "" : "s"} · ${KIND[d.project.kind]}`);
      location.hash = `#/sandbox/p/${d.project.id}/files`;
    } catch (e) { toast(e.message, "error", "", 9000); btn.disabled = false; btn.textContent = "Create project"; $("#sbProgress").textContent = ""; }
  }

  // ------------------------------------------------------------------ project
  async function openProject(id, tab) {
    const same = SB.d && SB.d.project.id === id;
    SB.tab = ["files", "publish", "history"].includes(tab) ? tab : "files";
    if (!same) {
      try { SB.d = await api(`/api/sandbox/projects/${id}`); } catch (e) { root().innerHTML = `<div class="sb-new"><a class="btn ghost small sb-back" href="#/sandbox">← Sandbox</a><div class="empty">${esc(e.message)}</div></div>`; return; }
      SB.file = null; SB.dirty = false;
    }
    renderProject();
  }
  function setData(d) { SB.d = { ...SB.d, ...d }; }
  function renderProject() {
    const { project: p, versions, links } = SB.d;
    const archived = p.status === "archived";
    root().innerHTML = `<div class="sb-project" style="--tool:${COLOR}">
      <div class="sb-phead">
        <a class="btn ghost icon" href="#/sandbox" title="Back to the Sandbox"><svg viewBox="0 0 24 24"><path d="m15 6-6 6 6 6"/></svg></a>
        <span class="sb-kind-ico" data-kind="${p.kind}">${KIND_ICON[p.kind] || ICON_BOX}</span>
        <div class="sb-ptitle"><h2>${esc(p.name)}</h2><small>${esc(KIND[p.kind])}${p.kind_auto ? " (detected)" : ""} · by ${esc(p.author)} · created ${esc(when(p.created))}${versions.length ? ` · v${versions[0].number} published ${esc(ago(versions[0].created))}` : " · not published yet"}</small></div>
        ${archived ? `<span class="pill warn">Archived - read-only</span>` : ""}
        <nav class="sb-tabs">${[["files", "Files"], ["publish", `Publish & links${links.filter((l) => l.status === "live").length ? ` · ${links.filter((l) => l.status === "live").length}` : ""}`], ["history", "History"]].map(([t, l]) => `<a class="sb-tab ${SB.tab === t ? "active" : ""}" href="#/sandbox/p/${p.id}/${t}">${l}</a>`).join("")}</nav>
        ${authorChip()}
        <button class="btn ghost icon" data-sb="menu" title="Details, archive, delete"><svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg></button>
      </div>
      <div class="sb-body" id="sbBody"></div></div>`;
    root().querySelector("[data-sb=author]").onclick = () => askAuthor();
    root().querySelector("[data-sb=menu]").onclick = (e) => projectMenu(e.currentTarget);
    if (SB.tab === "files") renderFiles();
    else if (SB.tab === "publish") renderPublish();
    else renderHistory();
  }

  function projectMenu(anchor) {
    const p = SB.d.project;
    const old = $("#ctxMenu"); if (old) old.remove();
    const m = document.createElement("div"); m.className = "menu"; m.id = "ctxMenu";
    const items = [["Edit name and description", editDetails],
      ["Download the draft (.zip)", () => { location.href = `/api/sandbox/projects/${p.id}/zip`; }],
      ["New preview address", rotatePreview],
      [p.status === "archived" ? "Restore from the archive" : "Archive", toggleArchive],
      ["Delete…", deleteProject]];
    for (const [label, fn] of items) { const b = document.createElement("button"); b.textContent = label; if (label.startsWith("Delete")) b.className = "danger-text"; b.onclick = () => { m.remove(); fn(); }; m.appendChild(b); }
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect();
    m.style.left = `${Math.min(r.left - 160, window.innerWidth - 240)}px`; m.style.top = `${r.bottom + 6}px`;
    setTimeout(() => document.addEventListener("click", () => m.remove(), { once: true }), 0);
  }
  function editDetails() {
    const p = SB.d.project;
    hub().openModal(`<div class="eyebrow">${esc(p.name)}</div><h3 class="serif" style="font-size:26px;margin:4px 0 12px">Details</h3>
      <div class="sb-form"><label>Name<input id="sbEdName" type="text" maxlength="80" value="${esc(p.name)}"></label>
      <label>Description<textarea id="sbEdDesc" rows="4" maxlength="2000">${esc(p.description)}</textarea></label>
      <div class="sb-row"><button class="btn primary" id="sbEdSave">Save</button></div></div>`, true);
    $("#sbEdSave").onclick = async () => {
      const who = needAuthor(); if (!who) return;
      try { setData(await api(`/api/sandbox/projects/${p.id}`, { method: "PATCH", body: { name: $("#sbEdName").value, description: $("#sbEdDesc").value, author: who } })); hub().closeModal(); renderProject(); toast("Saved", "ok", "", 2000); }
      catch (e) { toast(e.message, "error"); }
    };
  }
  async function rotatePreview() {
    const who = needAuthor(); if (!who) return;
    try { setData(await api(`/api/sandbox/projects/${SB.d.project.id}/preview-token`, { method: "POST", body: { author: who } })); renderProject(); toast("New preview address", "ok", "The old draft preview address no longer works."); }
    catch (e) { toast(e.message, "error"); }
  }
  async function toggleArchive() {
    const who = needAuthor(); if (!who) return;
    const p = SB.d.project, archive = p.status !== "archived";
    try { setData(await api(`/api/sandbox/projects/${p.id}/archive`, { method: "POST", body: { archived: archive, author: who } })); renderProject(); toast(archive ? "Archived - its links keep working" : "Restored", "ok"); }
    catch (e) { toast(e.message, "error"); }
  }
  function deleteProject() {
    const p = SB.d.project, live = SB.d.links.filter((l) => l.status === "live");
    hub().openModal(`<div class="eyebrow">Delete project</div><h3 class="serif" style="font-size:26px;margin:4px 0 6px">Delete “${esc(p.name)}”?</h3>
      <p class="lead">Its files and every published version are removed from disk. ${live.length ? `Its ${live.length} link${live.length === 1 ? "" : "s"} (${live.map((l) => "/" + esc(l.slug)).join(", ")}) will show “this link was taken down” and the names stay reserved - they are never given to another project.` : ""} The history (who did what, version hashes) is kept.</p>
      <div class="sb-form"><label>Type the project's name to confirm<input id="sbDelName" type="text" placeholder="${esc(p.name)}"></label>
      <div class="sb-row"><button class="btn danger" id="sbDelGo" disabled>Delete for good</button><button class="btn ghost" id="sbDelNo">Cancel</button></div></div>`, true);
    $("#sbDelName").oninput = (e) => { $("#sbDelGo").disabled = e.target.value.trim() !== p.name; };
    $("#sbDelNo").onclick = () => hub().closeModal();
    $("#sbDelGo").onclick = async () => {
      const who = needAuthor(); if (!who) return;
      try { await api(`/api/sandbox/projects/${p.id}`, { method: "DELETE", body: { confirm: p.id, author: who } }); hub().closeModal(); SB.dirty = false; toast(`${p.name} deleted`, "ok"); location.hash = "#/sandbox"; }
      catch (e) { toast(e.message, "error"); }
    };
  }

  // ------------------------------------------------------------------ files: tree + editor + preview
  function renderFiles() {
    const { files, project: p } = SB.d;
    const ro = p.status === "archived";
    $("#sbBody").innerHTML = `<div class="sb-files">
      <aside class="sb-tree card">
        <div class="sb-tree-head"><b>Files</b><span class="sb-muted">${files.length} · ${size(SB.d.size)}</span></div>
        <div class="sb-tree-actions">${ro ? "" : `<button class="btn small" data-act="new" title="New empty file">+ File</button><label class="btn small" title="Add files, a folder or a .zip (a lone .zip is unpacked)">Upload<input type="file" id="sbAdd" multiple hidden></label>`}</div>
        <ul class="sb-tree-list" id="sbTree">${files.map((f) => `<li><button class="sb-file ${SB.file && SB.file.path === f.path ? "on" : ""}" data-path="${esc(f.path)}" title="${esc(f.path)} · ${size(f.size)}"><span>${esc(f.path)}</span><em>${size(f.size)}</em></button></li>`).join("") || `<li class="sb-muted small">No files yet - add one.</li>`}</ul>
        ${ro ? "" : `<div class="sb-tree-drop" id="sbTreeDrop">Drop files here to add them</div>`}
      </aside>
      <section class="sb-editor-pane card">
        <div class="sb-ed-head" id="sbEdHead"></div>
        <div class="sb-ed-body" id="sbEdBody"></div>
      </section>
      <section class="sb-preview card">
        <div class="sb-ed-head"><b>Preview</b><span class="sb-muted" title="${esc(KIND_HELP[p.kind])}">${esc(KIND[p.kind])} · draft</span>
          <span class="sb-spacer"></span><button class="btn ghost small" data-act="reload" title="Reload the preview">Reload</button>
          <a class="btn ghost small" href="${esc(previewSrc())}" target="_blank" rel="noopener noreferrer" id="sbPrevOpen" title="Open the draft in a new tab">Open ↗</a></div>
        <iframe class="sb-frame" id="sbFrame" title="Draft preview" src="${esc(previewSrc())}"
          sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads${p.allow_storage ? " allow-same-origin" : ""}" referrerpolicy="no-referrer"></iframe>
        <div class="sb-frame-note">${p.allow_storage ? "Storage is allowed for this project (same-origin)." : "Runs in a sandbox on its own address - it cannot reach the hub."}</div>
      </section></div>`;
    const body = $("#sbBody");
    body.onclick = (e) => {
      const f = e.target.closest(".sb-file"); if (f) return pickFile(f.dataset.path);
      const a = e.target.closest("[data-act]"); if (!a) return;
      ({ new: newFile, reload: reloadPreview, save: saveFile, rename: renameFile, del: deleteFile, assist: askAssistant, revert: revertFile }[a.dataset.act] || (() => {}))();
    };
    const add = $("#sbAdd"); if (add) add.onchange = (e) => upload(inputItems(e.target));
    const drop = $("#sbTreeDrop");
    if (drop) {
      const tree = $(".sb-tree");
      tree.ondragover = (e) => { e.preventDefault(); tree.classList.add("over"); };
      tree.ondragleave = () => tree.classList.remove("over");
      tree.ondrop = async (e) => { e.preventDefault(); tree.classList.remove("over"); upload(await droppedItems(e.dataTransfer)); };
    }
    if (SB.file) paintEditor();
    else {
      const first = SB.want || SB.d.entry || (files.find((f) => /\.(py|js|ts|md|txt|ipynb|html|css|json)$/i.test(f.path)) || files[0] || {}).path;
      SB.want = null;
      if (first) pickFile(first); else paintEditor();
    }
  }
  function previewSrc() {
    const p = SB.d.project, f = SB.file;
    if (p.kind === "web" && f && /\.html?$/i.test(f.path) && f.path !== SB.d.entry) return SB.d.draft_url + f.path.split("/").map(encodeURIComponent).join("/");
    if (p.kind !== "web" && f) return SB.d.draft_url + `?file=${encodeURIComponent(f.path)}`;
    return SB.d.draft_url;
  }
  function reloadPreview() {
    const fr = $("#sbFrame"); if (!fr) return;
    const src = previewSrc();
    fr.src = "about:blank"; setTimeout(() => { fr.src = src; }, 30);
    const o = $("#sbPrevOpen"); if (o) o.href = src;
  }
  async function pickFile(path) {
    if (SB.file && SB.file.path === path) return;
    if (SB.dirty && !confirm(`${SB.file.path} has unsaved changes. Open another file and lose them?`)) return;
    try {
      const d = await api(`/api/sandbox/projects/${SB.d.project.id}/file?path=${encodeURIComponent(path)}`);
      SB.file = { ...d, saved: d.content }; SB.dirty = false;
    } catch (e) { toast(e.message, "error"); return; }
    $$(".sb-file").forEach((b) => b.classList.toggle("on", b.dataset.path === path));
    paintEditor();
    if (SB.d.project.kind !== "web" || /\.html?$/i.test(path)) reloadPreview();
  }
  function paintEditor() {
    const f = SB.file, ro = SB.d.project.status === "archived";
    const head = $("#sbEdHead"), body = $("#sbEdBody");
    if (!head) return;
    if (!f) { head.innerHTML = `<b>No file open</b>`; body.innerHTML = `<div class="empty" style="padding:30px">Pick a file on the left${ro ? "" : ", or add one"}.</div>`; return; }
    head.innerHTML = `<b class="mono" title="${esc(f.path)}">${esc(f.path)}</b><span class="sb-dirty" id="sbDirty" ${SB.dirty ? "" : "hidden"}>unsaved</span><span class="sb-spacer"></span>
      ${ro ? "" : `${f.text ? `<button class="btn ghost small" data-act="assist" title="Ask the hub Assistant's local model to change this file">✦ Assistant</button>` : ""}
      <button class="btn ghost small" data-act="rename">Rename</button><button class="btn ghost small danger-text" data-act="del">Delete</button>
      ${f.text ? `<button class="btn ghost small" data-act="revert" id="sbRevert" ${SB.dirty ? "" : "disabled"}>Revert</button><button class="btn primary small" data-act="save" id="sbSave" ${SB.dirty ? "" : "disabled"} title="Save (Ctrl+S)">Save</button>` : ""}`}`;
    if (!f.text) {
      const url = SB.d.draft_url + f.path.split("/").map(encodeURIComponent).join("/");
      body.innerHTML = /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/i.test(f.path)
        ? `<div class="sb-binary"><img src="${esc(url)}" alt="" referrerpolicy="no-referrer"><small>${size(f.size)}</small></div>`
        : `<div class="sb-binary"><b>${size(f.size)} · not a text file</b><small>It is kept and published as it is. Upload a new copy to replace it.</small></div>`;
      return;
    }
    body.innerHTML = `<div class="sb-code"><pre class="sb-gutter" id="sbGutter" aria-hidden="true"></pre><textarea id="sbText" spellcheck="false" autocapitalize="off" autocomplete="off" wrap="off" ${ro ? "readonly" : ""}></textarea></div>`;
    const ta = $("#sbText");
    ta.value = SB.file.content;
    gutter();
    ta.addEventListener("input", () => { SB.file.content = ta.value; markDirty(ta.value !== SB.file.saved); gutter(); });
    ta.addEventListener("scroll", () => { $("#sbGutter").scrollTop = ta.scrollTop; });
    ta.addEventListener("keydown", onKey);
  }
  function gutter() {
    const ta = $("#sbText"), g = $("#sbGutter"); if (!ta || !g) return;
    const n = ta.value.split("\n").length;
    if (+g.dataset.n !== n) { g.dataset.n = n; g.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n") + "\n "; }
    g.scrollTop = ta.scrollTop;
  }
  function markDirty(on) {
    SB.dirty = on;
    const d = $("#sbDirty"); if (d) d.hidden = !on;
    const s = $("#sbSave"); if (s) s.disabled = !on;
    const r = $("#sbRevert"); if (r) r.disabled = !on;
  }
  // Tab / Shift+Tab indent (several lines at once), Enter keeps the indentation, Ctrl+S saves.
  function onKey(e) {
    const ta = e.target;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveFile(); return; }
    const unit = /\n\t/.test(ta.value) && !/\n {2}\S/.test(ta.value) ? "\t" : (/\.py$/i.test(SB.file.path) ? "    " : "  ");
    if (e.key === "Tab") {
      e.preventDefault();
      const v = ta.value, s = ta.selectionStart, en = ta.selectionEnd;
      if (s === en && !e.shiftKey) { document.execCommand("insertText", false, unit); return; }
      const ls = v.lastIndexOf("\n", s - 1) + 1, le = en > s && v[en - 1] === "\n" ? en - 1 : (v.indexOf("\n", en) < 0 ? v.length : v.indexOf("\n", en));
      const block = v.slice(ls, le).split("\n");
      const out = block.map((l) => e.shiftKey ? l.replace(new RegExp(`^(\\t| {1,${unit === "\t" ? 4 : unit.length}})`), "") : unit + l).join("\n");
      ta.setSelectionRange(ls, le); document.execCommand("insertText", false, out);
      ta.setSelectionRange(ls, ls + out.length);
    } else if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.altKey) {
      const v = ta.value, s = ta.selectionStart;
      const line = v.slice(v.lastIndexOf("\n", s - 1) + 1, s);
      let ind = (line.match(/^[\t ]*/) || [""])[0];
      if (/[{[(:]\s*$/.test(line)) ind += unit;
      e.preventDefault(); document.execCommand("insertText", false, "\n" + ind);
    }
  }
  async function saveFile() {
    if (!SB.file || !SB.file.text || !SB.dirty) return;
    const who = needAuthor(); if (!who) return;
    const btn = $("#sbSave"); if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
    try {
      const d = await api(`/api/sandbox/projects/${SB.d.project.id}/file`, { method: "PUT", body: { path: SB.file.path, content: SB.file.content, author: who } });
      SB.file.saved = SB.file.content;
      const kindBefore = SB.d.project.kind;
      setData(d); markDirty(false);
      if (btn) btn.textContent = "Save";
      toast("Saved", "ok", SB.file.path, 1800);
      if (kindBefore !== d.project.kind) renderProject(); else { refreshTree(); reloadPreview(); }
    } catch (e) { toast(e.message, "error"); if (btn) { btn.disabled = false; btn.textContent = "Save"; } }
  }
  function revertFile() { if (!SB.file) return; SB.file.content = SB.file.saved; markDirty(false); paintEditor(); }
  function refreshTree() {
    const t = $("#sbTree"); if (!t) return;
    t.innerHTML = SB.d.files.map((f) => `<li><button class="sb-file ${SB.file && SB.file.path === f.path ? "on" : ""}" data-path="${esc(f.path)}" title="${esc(f.path)} · ${size(f.size)}"><span>${esc(f.path)}</span><em>${size(f.size)}</em></button></li>`).join("");
    const h = $(".sb-tree-head .sb-muted"); if (h) h.textContent = `${SB.d.files.length} · ${size(SB.d.size)}`;
  }
  function newFile() {
    const who = needAuthor(); if (!who) return;
    const path = prompt("New file name (folders with /, e.g. js/app.js):", SB.d.files.length ? "" : "index.html");
    if (!path) return;
    if (SB.dirty && !confirm(`${SB.file.path} has unsaved changes. Continue and lose them?`)) return;
    const starter = /\.html?$/i.test(path) && !SB.d.files.length ? `<!doctype html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n  <title>${path}</title>\n</head>\n<body>\n  <h1>Hello</h1>\n</body>\n</html>\n` : "";
    api(`/api/sandbox/projects/${SB.d.project.id}/file`, { method: "PUT", body: { path, content: starter, create: true, author: who } })
      .then((d) => { setData(d); SB.file = null; SB.dirty = false; SB.want = d.path; renderProject(); })
      .catch((e) => toast(e.message, "error"));
  }
  function renameFile() {
    const who = needAuthor(); if (!who || !SB.file) return;
    if (SB.dirty) { toast("Save or revert the changes first", "warn"); return; }
    const to = prompt(`Rename ${SB.file.path} to:`, SB.file.path);
    if (!to || to === SB.file.path) return;
    api(`/api/sandbox/projects/${SB.d.project.id}/rename`, { method: "POST", body: { from: SB.file.path, to, author: who } })
      .then((d) => { setData(d); SB.file = null; SB.want = d.path; renderProject(); toast("Renamed", "ok", d.path, 2000); })
      .catch((e) => toast(e.message, "error"));
  }
  function deleteFile() {
    const who = needAuthor(); if (!who || !SB.file) return;
    if (!confirm(`Delete ${SB.file.path} from the draft? Published versions keep their copy.`)) return;
    api(`/api/sandbox/projects/${SB.d.project.id}/file?${new URLSearchParams({ path: SB.file.path, author: who })}`, { method: "DELETE" })
      .then((d) => { setData(d); SB.file = null; SB.dirty = false; renderProject(); })
      .catch((e) => toast(e.message, "error"));
  }
  async function upload(items) {
    if (!items.length) return;
    const who = needAuthor(); if (!who) return;
    toast(`Uploading ${items.length} file${items.length === 1 ? "" : "s"}…`, "", "", 3000);
    try {
      const d = await send(`/api/sandbox/projects/${SB.d.project.id}/upload`, bundle({ author: who }, items));
      setData(d);
      toast(`Added ${d.added.length}${d.replaced.length ? `, replaced ${d.replaced.length}` : ""}`, "ok", [...d.added, ...d.replaced].slice(0, 4).join(", "));
      if (SB.file && d.replaced.includes(SB.file.path) && !SB.dirty) SB.file = null;
      renderProject();
    } catch (e) { toast(e.message, "error", "", 9000); }
  }

  // "Ask the Assistant to change this file": the hub Assistant's local model proposes the whole new file; the
  // change is shown line by line and only applied to the editor (unsaved) when you say so.
  function askAssistant() {
    if (!SB.file || !SB.file.text) return;
    hub().openModal(`<div class="eyebrow">Assistant · ${esc(SB.file.path)}</div><h3 class="serif" style="font-size:26px;margin:4px 0 6px">What should change?</h3>
      <p class="lead">The hub Assistant's local model rewrites this file as you ask - on the GPU when it is free, otherwise on the CPU (slower, up to a few minutes). You see the change before anything is applied; nothing is saved until you press Save.</p>
      <div class="sb-form"><textarea id="sbAsk" rows="3" placeholder="e.g. add a dark mode toggle in the top right corner"></textarea>
      <div class="sb-row"><button class="btn primary" id="sbAskGo">Suggest a change</button><span class="sb-muted" id="sbAskState"></span></div></div>
      <div id="sbAskOut"></div>`, false);
    $("#sbAsk").focus();
    $("#sbAskGo").onclick = async () => {
      const ask = $("#sbAsk").value.trim(); if (!ask) return;
      const go = $("#sbAskGo"); go.disabled = true; $("#sbAskState").innerHTML = `<span class="spinner"></span> Thinking…`;
      const t0 = Date.now(), tick = setInterval(() => { const s = $("#sbAskState"); if (s) s.innerHTML = `<span class="spinner"></span> Thinking… ${Math.round((Date.now() - t0) / 1000)} s`; }, 1000);
      try {
        const d = await api(`/api/sandbox/projects/${SB.d.project.id}/assist`, { method: "POST", body: { path: SB.file.path, content: SB.file.content, instruction: ask, author: author() } });
        const diff = lineDiff(SB.file.content, d.content);
        $("#sbAskState").textContent = `${d.info.seconds} s on the ${d.info.device.toUpperCase()} · +${diff.add} −${diff.del} lines`;
        $("#sbAskOut").innerHTML = `<pre class="sb-diff">${diff.html || "<i>No change.</i>"}</pre><div class="sb-row"><button class="btn primary" id="sbAskApply" ${diff.add + diff.del ? "" : "disabled"}>Apply to the editor</button><button class="btn ghost" id="sbAskNo">Discard</button></div>`;
        $("#sbAskNo").onclick = () => hub().closeModal();
        $("#sbAskApply").onclick = () => { SB.file.content = d.content; hub().closeModal(); paintEditor(); markDirty(SB.file.content !== SB.file.saved); toast("Applied - review it and press Save", "ok"); };
      } catch (e) { $("#sbAskState").textContent = ""; toast(e.message, "error", "", 9000); }
      clearInterval(tick); go.disabled = false;
    };
  }
  // A plain LCS line diff (files the assistant takes are small).
  function lineDiff(a, b) {
    const A = a.split("\n"), B = b.split("\n"), n = A.length, m = B.length;
    if (n * m > 4e6) return { add: m, del: n, html: B.map((l) => `<ins>+ ${esc(l)}</ins>`).join("\n") };
    const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    const out = []; let i = 0, j = 0, add = 0, del = 0;
    while (i < n || j < m) {
      if (i < n && j < m && A[i] === B[j]) { out.push(`  ${esc(A[i])}`); i++; j++; }
      else if (j < m && (i >= n || L[i][j + 1] >= L[i + 1][j])) { out.push(`<ins>+ ${esc(B[j])}</ins>`); j++; add++; }
      else { out.push(`<del>- ${esc(A[i])}</del>`); i++; del++; }
    }
    // keep 3 lines of context around the changes
    const keep = out.map((l) => l.startsWith("<"));
    const html = out.filter((_, k) => keep.slice(Math.max(0, k - 3), k + 4).some(Boolean)).join("\n");
    return { add, del, html };
  }

  // ------------------------------------------------------------------ publish & links
  function renderPublish() {
    const { project: p, versions, links, bases } = SB.d;
    const ro = p.status === "archived";
    const live = links.filter((l) => l.status === "live"), retired = links.filter((l) => l.status !== "live");
    const opts = (pin) => `<option value="latest" ${pin ? "" : "selected"}>latest${versions.length ? ` (now v${versions[0].number})` : ""}</option>` + versions.map((v) => `<option value="${v.number}" ${pin === v.number ? "selected" : ""}>v${v.number} only</option>`).join("");
    $("#sbBody").innerHTML = `<div class="sb-pub">
      <div class="sb-pub-main">
        ${networkNote(bases)}
        <section class="card">
          <div class="card-head"><div><div class="eyebrow">Publish</div><h3>${versions.length ? `Publish version ${versions[0].number + 1}` : "Publish the first version"}</h3></div></div>
          <p class="lead">Publishing freezes the draft as it is now: an immutable version with a fingerprint (sha256) of every file. Links set to <b>latest</b> move to it; pinned links stay where they are. Every version also keeps a permanent address, <span class="mono">link@vN</span>.</p>
          ${ro ? `<div class="note warn">Archived projects are read-only. Restore it from the ⋯ menu to publish.</div>` : `
          <div class="sb-form">
            <label><span>What changed? <span class="sb-muted">(version note)</span></span><input id="sbNote" type="text" maxlength="300" placeholder="${versions.length ? "e.g. fixed the totals, added a dark mode" : "e.g. first version"}"></label>
            ${live.length ? `<div class="sb-muted small">This version goes live at ${live.filter((l) => !l.pin).map((l) => `<b>/${esc(l.slug)}</b>`).join(", ") || "none of the current links (they are all pinned)"}.</div>` : ""}
            <label><span>${live.length ? "Also create a new link" : "Link name"} <span class="sb-muted">${live.length ? "(optional)" : ""}</span></span>
              <div class="sb-slug-input"><span class="sb-slug-base">${esc(bases.share)}/</span><input id="sbSlug" type="text" maxlength="48" placeholder="${esc(suggestSlug(p.name))}" autocomplete="off" spellcheck="false"></div>
              <small class="sb-slug-state" id="sbSlugState">a-z, 0-9 and hyphens · 2-48 characters · never reused once taken</small></label>
            <label class="sb-check"><input type="checkbox" id="sbPin"> Pin the new link to this version (otherwise it follows the latest)</label>
            <div class="sb-row"><button class="btn primary" id="sbPublish" ${SB.d.files.length ? "" : "disabled"}>Publish</button><span class="sb-muted">as ${esc(author() || "—")} · ${SB.d.files.length} files · ${size(SB.d.size)}</span></div>
          </div>`}
        </section>
        <section class="card">
          <div class="card-head"><div><div class="eyebrow">Shared at</div><h3>Links</h3></div></div>
          ${live.length ? `<table class="sb-table"><thead><tr><th>Address</th><th>Shows</th><th>Views</th><th>Created</th><th></th></tr></thead><tbody>
            ${live.map((l) => `<tr><td><a href="${esc(shareUrl(l.slug))}" target="_blank" rel="noopener noreferrer" class="mono">${esc(shareUrl(l.slug))}</a></td>
              <td><select data-repoint="${esc(l.slug)}" ${ro ? "disabled" : ""}>${opts(l.pin)}</select></td><td>${l.views}</td><td><small>${esc(when(l.created))}<br>${esc(l.created_by)}</small></td>
              <td class="sb-actions"><button class="btn small" data-copy="${esc(shareUrl(l.slug))}">Copy</button><button class="btn small ghost danger-text" data-retire="${esc(l.slug)}">Take down</button></td></tr>`).join("")}</tbody></table>`
            : `<div class="empty small">No links yet - ${versions.length ? "add one below" : "publish a version and name its link"}.</div>`}
          ${versions.length && !ro ? `<div class="sb-addlink"><div class="sb-slug-input"><span class="sb-slug-base">${esc(bases.share)}/</span><input id="sbNewSlug" type="text" maxlength="48" placeholder="another-name" autocomplete="off" spellcheck="false"></div>
            <select id="sbNewTarget">${opts(null)}</select><button class="btn" id="sbAddLink" disabled>Add link</button></div><small class="sb-slug-state" id="sbNewSlugState"></small>` : ""}
          ${retired.length ? `<div class="sb-retired"><b>Taken down</b> (reserved forever): ${retired.map((l) => `<span class="sb-slug off" title="taken down ${esc(when(l.retired))} by ${esc(l.retired_by || "")}">/${esc(l.slug)}</span>`).join(" ")}</div>` : ""}
        </section>
      </div>
      <aside class="sb-pub-side">
        <section class="card">
          <div class="card-head"><div><div class="eyebrow">How it runs</div><h3>Runtime</h3></div></div>
          <div class="sb-form">
            <label>Kind<select id="sbKind" ${ro ? "disabled" : ""}>${["web", "script", "document"].map((k) => `<option value="${k}" ${p.kind === k ? "selected" : ""}>${KIND[k]}${p.kind_auto && p.kind === k ? " (detected)" : ""}</option>`).join("")}<option value="auto">Detect from the files</option></select>
              <small class="sb-muted">${esc(KIND_HELP[p.kind])}.${p.kind !== "web" ? " Its link shows a read-only page with the files, line numbers, copy and download." : ""}</small></label>
            <div class="sb-toggle"><div><b>Allow storage (same-origin)</b><small>For apps that need localStorage, IndexedDB or cookies. ${p.allow_storage ? "On" : "Off"}.</small></div>
              <button type="button" class="switch ${p.allow_storage ? "on" : ""}" id="sbStorage" ${ro ? "disabled" : ""} aria-label="Allow storage"></button></div>
            <div class="note warn small">With storage on, the app runs on the Sandbox's shared origin: every storage-enabled Sandbox app can read the others' saved data (localStorage, IndexedDB, cookies on this address). Leave it off unless the app breaks without it. Turning it off applies to every link at once; turning it on applies to the preview now and to links from the next published version.</div>
            <div class="sb-muted small">Always: the app cannot call the hub or the studios, and nothing is run on the server.</div>
          </div>
        </section>
        <section class="card">
          <div class="card-head"><div><div class="eyebrow">Permanent addresses</div><h3>Versions</h3></div></div>
          <ul class="sb-versions">${versions.map((v) => `<li><div><b>v${v.number}</b> <span class="sb-muted">${esc(when(v.created))} · ${esc(v.author)}</span>${v.note ? `<div class="sb-vnote">${esc(v.note)}</div>` : ""}</div>
            ${live[0] ? `<button class="btn small ghost" data-copy="${esc(shareUrl(live[0].slug, v.number))}" title="${esc(shareUrl(live[0].slug, v.number))}">Copy @v${v.number}</button>` : ""}</li>`).join("") || `<li class="sb-muted small">None yet.</li>`}</ul>
        </section>
      </aside></div>`;
    const body = $("#sbBody");
    body.onclick = (e) => {
      const c = e.target.closest("[data-copy]"); if (c) return copy(c.dataset.copy);
      const r = e.target.closest("[data-retire]"); if (r) return retire(r.dataset.retire);
    };
    body.onchange = async (e) => {
      const s = e.target.closest("[data-repoint]"); if (!s) return;
      const who = needAuthor(); if (!who) { renderPublish(); return; }
      try { setData(await api(`/api/sandbox/links/${s.dataset.repoint}`, { method: "PATCH", body: { target: s.value, author: who } })); toast(`/${s.dataset.repoint} now shows ${s.value === "latest" ? "the latest version" : "v" + s.value}`, "ok"); renderProject(); }
      catch (err) { toast(err.message, "error"); renderPublish(); }
    };
    const slugInput = $("#sbSlug"); if (slugInput) slugWatch(slugInput, $("#sbSlugState"), null);
    const newSlug = $("#sbNewSlug"); if (newSlug) slugWatch(newSlug, $("#sbNewSlugState"), $("#sbAddLink"));
    const pub = $("#sbPublish"); if (pub) pub.onclick = publish;
    const add = $("#sbAddLink"); if (add) add.onclick = addLink;
    const kind = $("#sbKind"); if (kind) kind.onchange = () => patchProject({ kind: kind.value });
    const st = $("#sbStorage"); if (st) st.onclick = () => {
      const on = !p.allow_storage;
      if (on && !confirm("Allow storage for this app?\n\nIt will run on the Sandbox's shared origin: it can read the saved data (localStorage, IndexedDB) of every other Sandbox app that has storage on, and they can read its data. It still cannot reach the hub.\n\nOnly for apps that need it.")) return;
      patchProject({ allow_storage: on });
    };
  }
  const suggestSlug = (name) => (String(name).toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "my-tool").replace(/^(.)$/, "$1-1");
  function slugWatch(input, out, btn) {
    const check = async () => {
      const v = input.value.trim().toLowerCase();
      if (input.value !== v) input.value = v;
      if (!v) { out.className = "sb-slug-state"; out.textContent = btn ? "" : "a-z, 0-9 and hyphens · 2-48 characters · never reused once taken"; if (btn) btn.disabled = true; input.dataset.ok = ""; return; }
      try {
        const d = await api(`/api/sandbox/slug?slug=${encodeURIComponent(v)}`);
        if (input.value.trim().toLowerCase() !== v) return;
        out.className = `sb-slug-state ${d.ok ? "ok" : "bad"}`;
        out.textContent = d.ok ? `✓ ${SB.d.bases.share}/${d.slug}/ is free` : `✗ ${d.reason}`;
        input.dataset.ok = d.ok ? "1" : "";
        if (btn) btn.disabled = !d.ok;
      } catch (e) { out.textContent = e.message; }
    };
    input.addEventListener("input", () => { input.dataset.ok = ""; if (btn) btn.disabled = true; clearTimeout(SB.slugTimer); SB.slugTimer = setTimeout(check, 250); });
    if (!input.value && input.id === "sbSlug" && !SB.d.links.some((l) => l.status === "live")) { input.value = suggestSlug(SB.d.project.name); check(); }
  }
  async function publish() {
    if (SB.dirty && !confirm(`${SB.file.path} has unsaved changes - they are NOT part of this version. Publish anyway?`)) return;
    const who = needAuthor(); if (!who) return;
    const slug = ($("#sbSlug").value || "").trim();
    if (slug && $("#sbSlug").dataset.ok !== "1") { toast("Pick a link name that is free (or leave it empty)", "warn"); return; }
    if (!slug && !SB.d.links.some((l) => l.status === "live")) { toast("Name the link people will open", "warn"); $("#sbSlug").focus(); return; }
    const btn = $("#sbPublish"); btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Publishing…`;
    try {
      const d = await api(`/api/sandbox/projects/${SB.d.project.id}/publish`, { method: "POST", body: { note: $("#sbNote").value, slug, pin: $("#sbPin").checked, author: who } });
      setData(d);
      const live = d.links.filter((l) => l.status === "live" && (!l.pin || l.pin === d.published.number));
      toast(`Published v${d.published.number}`, "ok", live.length ? live.map((l) => shareUrl(l.slug)).join("  ") : "", 9000);
      renderProject();
    } catch (e) { toast(e.message, "error", "", 9000); btn.disabled = false; btn.textContent = "Publish"; }
  }
  async function addLink() {
    const who = needAuthor(); if (!who) return;
    try { setData(await api(`/api/sandbox/projects/${SB.d.project.id}/links`, { method: "POST", body: { slug: $("#sbNewSlug").value.trim(), target: $("#sbNewTarget").value, author: who } })); toast("Link created", "ok"); renderProject(); }
    catch (e) { toast(e.message, "error"); }
  }
  function retire(slug) {
    const who = needAuthor(); if (!who) return;
    if (!confirm(`Take down /${slug}?\n\nThe address will show "this link was taken down". The name stays reserved forever - it can never be used again, by anyone.`)) return;
    api(`/api/sandbox/links/${slug}/retire`, { method: "POST", body: { author: who } })
      .then((d) => { setData(d); toast(`/${slug} taken down`, "ok"); renderProject(); }).catch((e) => toast(e.message, "error"));
  }
  async function patchProject(patch) {
    const who = needAuthor(); if (!who) { renderPublish(); return; }
    try { setData(await api(`/api/sandbox/projects/${SB.d.project.id}`, { method: "PATCH", body: { ...patch, author: who } })); toast("Saved", "ok", "", 2000); renderProject(); }
    catch (e) { toast(e.message, "error"); renderPublish(); }
  }

  // ------------------------------------------------------------------ history
  const ACTION_TONE = { "created": "ok", "version published": "ok", "link created": "ok", "deleted": "bad", "link retired": "bad", "archived": "warn", "storage allowed": "warn" };
  function eventText(e) {
    const d = e.detail || {};
    const bits = [];
    if (d.path) bits.push(`<span class="mono">${esc(d.path)}</span>${d.to ? ` → <span class="mono">${esc(d.to)}</span>` : ""}`);
    if (d.version) bits.push(`v${d.version}`);
    if (d.files != null && e.action !== "deleted") bits.push(`${d.files} file${d.files === 1 ? "" : "s"}`);
    if (d.bytes != null && !d.path) bits.push(size(d.bytes));
    if (d.replaced && d.replaced.length) bits.push(`replaced ${d.replaced.length}`);
    if (d.target) bits.push(`→ ${esc(d.target)}`);
    if (d.before !== undefined && d.after !== undefined && d.before !== null) bits.push(`${esc(d.before)} → ${esc(d.after)}`);
    if (d.note) bits.push(`“${esc(d.note)}”`);
    if (d.instruction) bits.push(`“${esc(d.instruction)}”`);
    if (d.why) bits.push(esc(d.why));
    if (d.hash) bits.push(`<span class="mono" title="sha256 ${esc(d.hash)}">#${esc(d.hash.slice(0, 10))}</span>`);
    return bits.join(" · ");
  }
  function eventRows(evs, global) {
    return evs.map((e) => `<li class="${ACTION_TONE[e.action] || ""}"><time title="${esc(new Date(e.ts * 1000).toLocaleString())}">${esc(ago(e.ts))}</time>
      <div><b>${esc(e.action)}</b>${e.slug ? ` <span class="sb-slug">/${esc(e.slug)}</span>` : ""}${global && e.project_name ? ` · <a href="#/sandbox/p/${esc(e.project_id)}/history">${esc(e.project_name)}</a>` : ""}
      <small>${eventText(e)}</small><small class="sb-who">${esc(e.author)}${e.ip ? ` · ${esc(e.ip)}` : ""}</small></div></li>`).join("");
  }
  async function renderHistory() {
    const { versions, project: p } = SB.d;
    $("#sbBody").innerHTML = `<div class="sb-hist">
      <section class="card"><div class="card-head"><div><div class="eyebrow">Immutable</div><h3>Versions</h3></div></div>
        ${versions.length ? `<table class="sb-table"><thead><tr><th>Version</th><th>Published</th><th>By</th><th>Files</th><th>Fingerprint (sha256)</th><th></th></tr></thead><tbody>
        ${versions.map((v) => `<tr><td><b>v${v.number}</b>${v.note ? `<div class="sb-vnote">${esc(v.note)}</div>` : ""}</td><td><small>${esc(when(v.created))}</small></td><td>${esc(v.author)}</td><td>${v.files} · ${size(v.size)}</td>
          <td><span class="mono" title="${esc(v.hash)}">${esc(v.hash.slice(0, 16))}…</span><div class="sb-verify" id="sbVer${v.number}"></div></td>
          <td class="sb-actions"><button class="btn small" data-verify="${v.number}" title="Recompute every file's hash and compare it with the record">Verify</button><a class="btn small ghost" href="/api/sandbox/projects/${p.id}/zip?version=${v.number}">.zip</a></td></tr>`).join("")}</tbody></table>`
        : `<div class="empty small">Nothing published yet.</div>`}</section>
      <section class="card"><div class="card-head"><div><div class="eyebrow">Append-only</div><h3>Activity</h3></div><span class="sb-muted small">Every change: what, who (the name they gave), from which address, when.</span></div>
        <ul class="sb-events wide" id="sbProjEvents"><li class="empty small">Loading…</li></ul></section></div>`;
    $("#sbBody").onclick = async (e) => {
      const b = e.target.closest("[data-verify]"); if (!b) return;
      const out = $(`#sbVer${b.dataset.verify}`); out.innerHTML = `<span class="spinner"></span>`;
      try { const r = await api(`/api/sandbox/projects/${p.id}/versions/${b.dataset.verify}/verify`, { method: "POST", body: {} });
        out.innerHTML = r.ok ? `<span class="sb-ok">✓ all ${r.files} files match the record</span>` : `<span class="sb-bad">✗ ${[...r.changed.map((x) => "changed " + x), ...r.missing.map((x) => "missing " + x), ...r.extra.map((x) => "extra " + x)].map(esc).join(", ")}</span>`; }
      catch (err) { out.textContent = err.message; }
    };
    try { const d = await api(`/api/sandbox/projects/${p.id}/events`); $("#sbProjEvents").innerHTML = eventRows(d.events, false) || `<li class="empty small">Nothing yet.</li>`; }
    catch (e) { toast(e.message, "error"); }
  }

  window.Sandbox = { show, confirmLeave };
  if (location.hash.startsWith("#/sandbox")) setTimeout(() => show(location.hash), 0);
})();
