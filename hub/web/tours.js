/* Guided tours in the hub shell: the hub tour, the Sandbox and Storage page tours, the Tours page, the first-visit
   offer, and starting a studio's tour inside its frame (the bridge runs it there, see brand/bridge.js).
   Engine: brand/tour.js. Studio tours: brand/tours-studios.js. Tours only point and explain - they open pages,
   never start a job, upload, delete or change a setting. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const hub = () => window.Hub;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const waitFor = (pred, ms, every = 250) => new Promise((res) => { const t0 = Date.now(); const tick = () => { let ok = false; try { ok = pred(); } catch (e) { ok = false; } if (ok) return res(true); if (Date.now() - t0 > ms) return res(false); setTimeout(tick, every); }; tick(); });

  // ------------------------------------------------------------------ what was offered / taken (this browser)
  const KEY = "hub.tours.v1";
  const load = () => { try { const v = JSON.parse(localStorage.getItem(KEY) || "{}"); return { offered: !!v.offered, done: v.done || {}, seen: v.seen || {}, resetAt: v.resetAt || 0 }; } catch (e) { return { offered: false, done: {}, seen: {}, resetAt: 0 }; } };
  const save = (st) => { try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (e) { /* private window: nothing is remembered */ } };
  const mark = (fn) => { const st = load(); fn(st); save(st); if (hub() && hub().view && hub().view() === "tours") renderPage(); };

  const state = () => (hub() && hub().state()) || null;
  const studios = () => { const st = state(); return st ? st.order.map((id) => st.tools[id]).filter(Boolean) : []; };
  const STUDIO_LINES = { image: "pictures from words, photo edits and cut-outs", tts: "speech, designed and cloned voices, whole drama scenes",
    video: "clips, storyboards and short films with sound", music: "whole songs with vocals from your lyrics", forge: "Stable Diffusion with checkpoints and LoRAs" };

  // ------------------------------------------------------------------ the hub tour
  const HUB_TOUR = {
    id: "hub", title: "Hub tour",
    steps: [
      { before: (c) => c.go("#/home"), title: "Welcome to AI Studio Hub",
        text: ["Your AI studios for pictures, voices, video and music, all on this PC.", "They share one graphics card. The hub loads and unloads models for you, so you never have to juggle them."] },
      { sel: "#navTools", title: "The studios",
        text: "Each studio opens right here in the hub:",
        list: () => studios().map((t) => `<b>${esc(t.name)}</b> - ${esc(STUDIO_LINES[t.id] || t.tagline || "")}`) },
      { sel: '.nav-item[data-view="sandbox"]', title: "Sandbox",
        text: "A place to share what you built with an AI (ChatGPT, Gemini…): upload it, change it here, publish a link." },
      { sel: [".top-status", "#chipOwner"], title: "One GPU, one job at a time",
        text: ["This shows which studio holds the graphics card and how full it is.", "Jobs from every studio wait their turn in the <b>GPU queue</b>. When something waits, a \"waiting\" chip appears here - open it to see the order or <b>Skip</b> a request."] },
      { before: (c) => c.go("#/home"), sel: ["#toolCards .tool-card", "#toolCards"], title: "Studio cards",
        list: ["<b>Open</b> - show the studio here (it starts if it's off)", "<b>Warm up</b> - load its model now, so the first job starts at once", "<b>Stop</b> - shut it down to free memory", "<b>⋯</b> - log, restart, output folder"] },
      { sel: [".top-actions", "#btnActivity"], title: "Free GPU and Activity",
        text: ["<b>Free GPU</b> unloads every model. <b>Activity</b> lists everything the hub did.", "Long jobs tell you when they finish, even when you're on another page."] },
      { before: (c) => c.go("#/assistant"), sel: [".as-chat", "#view-assistant"], wait: 2500, title: "Assistant",
        text: "Say what you want in plain words. It picks the studio, model and prompt, shows you the plan, and renders only when you say so." },
      { sel: ['#asStudios [data-id="vprompt"]', "#asStudios"], wait: 2500, title: "Video prompt writer",
        text: "Describe a short film - who, where, what happens, who says what - and it writes a ready-made prompt for each part, to send to Video Studio." },
      { before: (c) => c.go("#/library"), sel: [".view-library .lib-bar", "#libGrid"], title: "Library",
        text: ["Everything every studio has made, in one place. Search it or filter by studio.", "Open an item to download it or <b>Send to</b> another studio - no download and upload needed."] },
      { before: (c) => c.go("#/sandbox"), sel: [".sb-gallery .lib-bar", "#view-sandbox"], wait: 2500, title: "Sandbox",
        text: ["Upload a page, a tool or a script, edit it in the browser, then publish a link the team can open.", "Nothing is public until you publish. The Sandbox has its own Tour button."] },
      { before: (c) => c.go("#/storage"), sel: [".storage-disk", "#storageBody"], wait: 4000, title: "Storage",
        text: ["How much space each studio uses.", "A clean-up only runs after you review the exact list of files and confirm it."] },
      { before: (c) => c.go("#/models"), sel: ["#modelsGrid .studio-models", "#modelsGrid"], wait: 4000, title: "Models",
        text: "Every studio's models: what's installed, what you can download, and which one is in use." },
      { before: (c) => c.go("#/settings"), sel: '.nav-item[data-view="settings"]', wait: 1500, title: "Settings",
        list: ["<b>Appearance</b> - light or dark theme (and Look below)", "<b>Network</b> - share the hub with your network", "<b>Notifications</b> - browser or phone alerts when a job ends", "<b>Health watchdog</b> - restarts a studio that hung"] },
      { sel: '.nav-item[data-view="tours"]', title: "Tours",
        text: "Replay this tour or any studio's tour here. <b>Reset tours</b> brings back the first-visit offers." },
      { before: (c) => c.go("#/home"), title: "Next: pick a studio tour",
        text: "Each studio has a short tour of its own. Pick one to open the studio and start it - or press Done.",
        actions: () => studios().filter((t) => t.installed && t.enabled).map((t, i) => ({ label: t.name, primary: i === 0, run: () => startStudio(t.id) })) },
    ],
  };

  // ------------------------------------------------------------------ Sandbox page tour
  let sbFirst = null;
  const SANDBOX_TOUR = {
    id: "sandbox", title: "Sandbox tour", restore: true,
    steps: [
      { before: async (c) => { await c.go("#/sandbox"); try { const r = await fetch("/api/sandbox/projects"); const d = await r.json(); sbFirst = (d.projects || [])[0] || null; } catch (e) { sbFirst = null; } },
        sel: ["#sbGrid .sb-card", ".sb-empty", "#sbGrid"], wait: 2500, title: "Your projects",
        text: () => sbFirst ? "Every project uploaded to the Sandbox, with its links and views. Click one to open it." : "Nothing here yet. Projects you upload show up as cards, with their links and views." },
      { sel: "#sbKinds", title: "Filter", text: "Web apps, scripts and documents - and the archived ones, whose links keep working." },
      { sel: ".sb-gallery .sb-author", title: "Your name", text: "The hub has no accounts: this name is recorded with every upload, edit and publish." },
      { sel: ".sb-activity", title: "Activity", text: "Who changed or published what, across every project." },
      { sel: '.sb-gallery a[href="#/sandbox/new"]', title: "New project", text: "Start here to upload something new." },
      { before: (c) => c.go("#/sandbox/new"), sel: "#sbDrop", wait: 2500, title: "Upload",
        text: "Drop files, a folder or a .zip. A zip is unpacked and checked on the hub." },
      { sel: [".sb-new .sb-form", "#sbName"], title: "Name it",
        text: ["Give it a name and a line about what it does.", "<b>Create project</b> uploads it - nothing is public until you publish."] },
      { when: () => !sbFirst, title: "Inside a project",
        list: ["<b>Files</b> - edit any file in the browser with a live preview", "<b>Publish &amp; links</b> - publish a version and share its link", "<b>History</b> - every change, and who made it"] },
      { when: () => !!sbFirst, before: (c) => c.go(`#/sandbox/p/${sbFirst.id}/files`), sel: '.sb-tabs a[href$="/files"]', wait: 3000, title: "Files",
        text: "Edit any file right here; the preview next to it updates as you save." },
      { when: () => !!sbFirst, before: (c) => c.go(`#/sandbox/p/${sbFirst.id}/publish`), sel: '.sb-tabs a[href$="/publish"]', wait: 3000, title: "Publish & links",
        text: "Publish a version and share its link. A link can follow the latest version or stay on one." },
      { when: () => !!sbFirst, before: (c) => c.go(`#/sandbox/p/${sbFirst.id}/history`), sel: '.sb-tabs a[href$="/history"]', wait: 3000, title: "History",
        text: "Every upload, edit and publish, with who did it - so you can see what changed." },
    ],
  };

  // ------------------------------------------------------------------ Storage page tour
  const STORAGE_TOUR = {
    id: "storage", title: "Storage tour", restore: true,
    steps: [
      { before: (c) => c.go("#/storage"), sel: "#view-storage .lib-bar", title: "Storage",
        text: "When the space was last measured and how much the disk has free. Rescan measures again." },
      { sel: [".storage-disk", "#storageBody"], wait: 5000, title: "This disk", text: "Each colour is one studio's share of the disk." },
      { sel: [".storage-studio", "#storageBody"], wait: 3000, title: "Per studio", text: "Outputs, models, caches and inputs - what each studio keeps, and how big it is." },
      { sel: ".cleanup", wait: 3000, title: "Clean up",
        text: ["Tick what may go. Leftover files and old Trash are ticked for you.", "Anything that still belongs to a job in History goes only if you tick it yourself."] },
      { sel: ['[data-storage-act="review"]', ".cleanup-actions"], title: "Review, then confirm",
        text: "Review shows the exact list of files first. Nothing is deleted until you press Delete in that list." },
    ],
  };

  const PAGE_TOURS = { hub: HUB_TOUR, sandbox: SANDBOX_TOUR, storage: STORAGE_TOUR };

  function startPage(id) {
    const tour = PAGE_TOURS[id]; if (!tour || !window.HubTour) return false;
    try { hub().closeModal(); } catch (e) { /* ignore */ }
    const from = location.hash || "#/home";
    return window.HubTour.start(tour, {
      onEnd: (r) => {
        mark((st) => { st.offered = true; if (r.completed) st.done[id] = Date.now() / 1000; else st.seen[id] = Date.now() / 1000; });
        if (tour.restore && location.hash !== from) location.hash = from;
      },
    });
  }

  // ------------------------------------------------------------------ studio tours (run by the bridge in the frame)
  const pending = {};          // studio id -> waiting for its frame to ask
  const frameOf = (src) => { const st = state(); if (!st) return null; for (const id of st.order) { const f = hub().frame(id); if (f && f.contentWindow === src) return id; } return null; };
  const post = (id, msg) => { const f = hub().frame(id); if (f) { try { f.contentWindow.postMessage(msg, "*"); } catch (e) { /* ignore */ } } };

  async function startStudio(id) {
    const t = (state() || { tools: {} }).tools[id];
    if (!t) return;
    if (!t.installed || !t.enabled) { hub().toast(`${t.name} is not available`, "warn", !t.enabled ? "It is disabled in Settings." : (t.install_note || "")); return; }
    pending[id] = Date.now();
    if (window.HubTour && window.HubTour.offerOpen()) window.HubTour.closeOffer();
    location.hash = `#/tool/${id}`;
    if (hub().frameReady(id)) post(id, { type: "hub:tour", action: "start" });
    else hub().toast(`Opening ${t.name}…`, "", "Its tour starts as soon as it is ready.", 6000);
    // The frame's bridge asks for its tour state once the studio has rendered: the start goes out with the answer.
    // A studio that never comes up (not installed, crashed) just drops the request after 5 minutes.
    setTimeout(() => { if (pending[id] && Date.now() - pending[id] >= 299000) delete pending[id]; }, 300000);
  }

  window.addEventListener("message", (e) => {
    const d = e.data || {};
    if (!d.type || String(d.type).indexOf("hub:tour") !== 0) return;
    const id = frameOf(e.source);
    if (!id || id !== d.tool) return;           // only the studio frames, each about itself
    const st = load();
    if (d.type === "hub:tour-ask") {
      const startNow = !!pending[id];
      post(id, { type: "hub:tour-state", tool: id, seen: !!st.seen[id] || !!st.done[id], resetAt: st.resetAt || 0, offer: !startNow && !(window.HubTour && window.HubTour.active()) });
      if (startNow) post(id, { type: "hub:tour", action: "start" });
    } else if (d.type === "hub:tour-status") {
      if (d.ok) delete pending[id];
      else if (!/not ready/.test(d.message || "")) { delete pending[id]; hub().toast(d.message || "The tour could not start", "warn"); }
    } else if (d.type === "hub:tour-seen") {
      mark((s) => { s.seen[id] = Date.now() / 1000; });
    } else if (d.type === "hub:tour-ended") {
      mark((s) => { s.seen[id] = Date.now() / 1000; if (d.completed) s.done[id] = Date.now() / 1000; });
      if (Array.isArray(d.report)) window.__lastStudioTourReport = { tool: id, completed: d.completed, steps: d.report };
    }
  });

  function resetTours() {
    const resetAt = Date.now() / 1000;
    save({ offered: false, done: {}, seen: {}, resetAt });
    const st = state();
    if (st) st.order.forEach((id) => { if (hub().frame(id)) post(id, { type: "hub:tour", action: "reset", resetAt }); });
    // no toast: the offer card appears in the same corner, and every row reads "New" again
    renderPage();
    setTimeout(offerHub, 600);
  }

  // ------------------------------------------------------------------ the first-visit offer
  function offerHub() {
    if (!window.HubTour || window.HubTour.active() || window.HubTour.offerOpen()) return;
    const st = load();
    if (st.offered) return;
    if (hub().view() === "tool") return;              // not over a studio; it waits for the next hub page
    window.HubTour.offer({
      title: "New here? Take a 2-minute tour",
      text: "A quick look at the studios, the shared GPU and every page of the hub. It only points and explains.",
      startLabel: "Take the tour", laterLabel: "Not now",
      onStart: () => { mark((s) => { s.offered = true; }); startPage("hub"); },
      onLater: () => { mark((s) => { s.offered = true; }); hub().toast("You can take it any time", "", "Tours in the sidebar lists every tour.", 5000); },
    });
  }
  (async function boot() {
    if (hub() && hub().view() === "tours") renderPage();     // the page was opened before this script loaded
    await waitFor(() => window.HubTour && state(), 30000);
    if (hub().view() === "tours") renderPage();              // now with the studios
    await sleep(1200);
    offerHub();
    window.addEventListener("hashchange", () => setTimeout(() => {
      // never over a studio: the offer closes (unanswered) and comes back on the next hub page
      if (hub().view() === "tool") { if (window.HubTour.offerOpen()) window.HubTour.closeOffer(); } else offerHub();
    }, 300));
  })();

  // ------------------------------------------------------------------ the Tours page
  function renderPage() {
    const root = $("#view-tours"); if (!root) return;
    const st = load(), defs = window.HubTourStudios || {};
    const status = (id) => st.done[id] ? `<span class="pill ready">Taken</span>` : st.seen[id] ? `<span class="pill">Seen</span>` : `<span class="pill off">New</span>`;
    const hubRows = [
      ["hub", "The whole hub", "Studios, the GPU queue, Home, Assistant, Library, Sandbox, Storage, Models and Settings.", `${HUB_TOUR.steps.length} steps · 2 min`],
      ["sandbox", "Sandbox", "Projects, uploading, Files, Publish & links and History.", "1 min"],
      ["storage", "Storage", "Space per studio and the clean-up that only runs after you confirm.", "1 min"],
    ];
    const row = (kind, id, name, desc, meta, color, icon, note, disabled) => `<div class="tour-row" style="--tool:${color || "var(--accent)"}">
        <div class="tool-num">${icon}</div>
        <div class="tour-main"><b>${esc(name)}</b><small>${esc(desc)}</small>${note ? `<small class="tour-note">${esc(note)}</small>` : ""}</div>
        <div class="tour-meta"><span>${esc(meta)}</span>${status(id)}</div>
        <button class="btn ${kind === "hub" && id === "hub" ? "primary" : ""}" data-tour-start="${esc(kind)}:${esc(id)}" ${disabled ? "disabled" : ""}>Start</button></div>`;
    const ICO_HUB = `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5z"/></svg>`;
    const studioRows = studios().map((t) => {
      const d = defs[t.id] || {};
      const steps = d.steps ? `${d.steps.length} steps · ${d.minutes || 1} min` : (d.external ? "Forge's own tour" : "");
      const note = !t.installed ? "Not set up on this PC" : !t.enabled ? "Disabled in Settings" : (t.state !== "running" ? "Opens the studio (it starts if it's off)" : "");
      return row("studio", t.id, t.name, d.blurb || STUDIO_LINES[t.id] || t.tagline || "", steps, t.color, hub().studioIcon(t.id), note, !t.installed || !t.enabled || (!d.steps && !d.external));
    }).join("");
    root.innerHTML = `<div class="tours-page">
      <section class="card"><div class="card-head"><div><div class="eyebrow">Guided tours</div><h3>The hub</h3></div>
        <button class="btn ghost small" id="toursReset" title="Show the first-visit offers again, in the hub and in every studio">Reset tours</button></div>
        <p class="lead">A tour points at each part of the page and explains it in a sentence or two. Use <kbd>←</kbd> <kbd>→</kbd> to move and <kbd>Esc</kbd> to leave. Tours never start a job, upload, delete or change a setting.</p>
        <div class="tour-list">${hubRows.map(([id, n, desc, meta]) => row("hub", id, n, desc, meta, "", ICO_HUB, "", false)).join("")}</div></section>
      <section class="card"><div class="card-head"><div><div class="eyebrow">Inside each studio</div><h3>Studio tours</h3></div></div>
        <p class="lead">Each one runs inside the studio. It is offered the first time you open the studio here; the small <b>Tour</b> button in the studio starts it again.</p>
        <div class="tour-list">${studioRows || `<div class="empty">Loading…</div>`}</div></section></div>`;
    $("#toursReset").onclick = resetTours;
  }

  document.addEventListener("click", (e) => {
    const s = e.target.closest("[data-tour-start]");
    if (s) { e.preventDefault(); const [kind, id] = s.dataset.tourStart.split(":"); if (kind === "hub") startPage(id); else startStudio(id); return; }
    const b = e.target.closest("[data-tour]");
    if (b) { e.preventDefault(); startPage(b.dataset.tour); }
  });

  window.HubTours = { show: renderPage, start: startPage, startStudio, reset: resetTours, offer: offerHub, tours: PAGE_TOURS, store: load };
})();
