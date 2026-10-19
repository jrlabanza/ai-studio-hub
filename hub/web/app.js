/* AI Studio Hub - shell logic (no build step). */
(() => {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  // Each studio's icon (24 px, stroked in its signature colour) - shown where the 01-05 numbers used to be.
  const STUDIO_ICONS = {
    image: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>',
    tts: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/><path d="M8.5 21h7"/>',
    video: '<rect x="3" y="6.5" width="13" height="11" rx="2.5"/><path d="m16 10.5 5-3v9l-5-3"/>',
    music: '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
    forge: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 15l.7 1.9L21.6 17.6l-1.9.7L19 20.2l-.7-1.9-1.9-.7 1.9-.7z"/>',
  };
  const studioIcon = (id) => `<svg viewBox="0 0 24 24" aria-hidden="true">${STUDIO_ICONS[id] || '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>'}</svg>`;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const S = {
    state: null, view: "home", tool: null, theme: document.documentElement.dataset.theme || "light",
    frames: {}, frameGen: {}, frameWasRunning: {}, frameReady: {}, events: [], unread: 0, es: null, connected: false, handoff: null,
    lib: { tool: "", q: "", items: [], offset: 0, total: 0, loading: false, counts: {} }, libTimer: null,
    settingsDirty: false, recentLoadedAt: 0, lastToast: { text: "", at: 0 }, focusSent: null,
    models: { data: null, tool: "", loading: false, downloads: {} },
  };
  // Names, colours and numbers come from the hub (hub/tools.py); these maps are filled from the first state.
  const TOOL_NAMES = {}, TOOL_COLORS = {}, TOOL_NUMS = {};
  const nameOf = (id) => TOOL_NAMES[id] || id;

  // ------------------------------------------------------------------ helpers
  const api = async (url, opts = {}) => {
    const r = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body });
    let data = null;
    try { data = await r.json(); } catch (e) { /* no body */ }
    if (!r.ok) throw new Error((data && data.detail) || `${r.status} ${r.statusText}`);
    return data;
  };
  const gb = (mb) => (mb / 1024).toFixed(mb >= 10240 ? 0 : 1) + " GB";
  const fmtTime = (ts) => { const d = new Date(ts * 1000); return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
  const fmtAgo = (ts) => {
    if (!ts) return "";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    const d = new Date(ts * 1000);
    return d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + fmtTime(ts);
  };
  const fmtDur = (sec) => { if (sec == null || isNaN(sec)) return ""; sec = Math.round(sec); return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`; };
  const fmtEta = (sec) => { if (sec == null) return ""; sec = Math.round(sec); return sec >= 60 ? `${Math.floor(sec / 60)} min ${sec % 60}s left` : `${sec}s left`; };
  const toolOf = (id) => S.state && S.state.tools[id];

  function learnTools(st) {
    for (const [id, t] of Object.entries(st.tools || {})) { TOOL_NAMES[id] = t.name; TOOL_COLORS[id] = t.color; TOOL_NUMS[id] = t.number; }
    const nav = $("#navTools");
    if (nav && nav.dataset.built !== st.order.join(",")) {
      nav.dataset.built = st.order.join(",");
      nav.innerHTML = st.order.map((id, i) => { const t = st.tools[id]; return `<a class="nav-item nav-tool" data-view="tool" data-tool="${id}" href="#/tool/${id}" style="--tool:${t.color}" title="${esc(t.name)} (Alt+${i + 1})">
        <span class="nav-ico tool-ico">${studioIcon(id)}</span><span class="nav-label">${esc(t.name)}</span><span class="nav-dot" id="navdot-${id}"></span></a>`; }).join("");
      const chips = $("#libChips");
      chips.querySelectorAll(".chip[data-tool]:not([data-tool=''])").forEach((c) => c.remove());
      chips.insertAdjacentHTML("beforeend", st.order.map((id) => `<button class="chip filter" data-tool="${id}" style="--tool:${st.tools[id].color}">${esc(st.tools[id].short)}</button>`).join(""));
    }
  }
  const proxyUrl = (t) => `${location.protocol}//${location.hostname}:${t.proxy_port}/`;
  const greeting = () => { const h = new Date().getHours(); return h < 12 ? "Good morning." : h < 18 ? "Good afternoon." : "Good evening."; };

  function toast(text, tone = "", sub = "", ttl = 6000) {
    const now = Date.now();
    if (S.lastToast.text === text && now - S.lastToast.at < 4000) return;
    S.lastToast = { text, at: now };
    const el = document.createElement("div");
    el.className = `toast ${tone}`;
    el.innerHTML = `<div><div>${esc(text)}</div>${sub ? `<small>${esc(sub)}</small>` : ""}</div>`;
    const host = $("#toasts");
    while (host.children.length >= 4) host.firstElementChild.remove();
    host.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; setTimeout(() => el.remove(), 320); }, ttl);
  }

  // ------------------------------------------------------------------ look
  // "cinema" (default): the AI-film-studio palette, always dark - the studios get "dark" too, so the whole hub
  // matches; "classic": palette v7 with the light / dark theme.
  function currentLook() { try { return localStorage.getItem("pxx-look") || "cinema"; } catch (e) { return "cinema"; } }
  function applyLook(look, save = true) {
    document.documentElement.dataset.look = look;
    try { localStorage.setItem("pxx-look", look); } catch (e) { /* ignore */ }
    if (save) api("/api/settings", { method: "PUT", body: { look } }).catch(() => {});   // the studios' pages read it
    const t = $("#btnTheme");
    if (t) t.hidden = look === "cinema";
    if (look === "cinema") applyTheme("dark", false);
    else applyTheme(S.classicTheme || "light", false);
  }

  // ------------------------------------------------------------------ theme
  function applyTheme(theme, save = true) {
    if (currentLook() !== "cinema") S.classicTheme = theme;
    S.theme = theme;
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("pxx-theme", theme); } catch (e) { /* ignore */ }
    Object.values(S.frames).forEach((f) => { try { f.contentWindow.postMessage({ type: "hub:theme", theme, look: currentLook() }, "*"); } catch (e) { /* ignore */ } });
    if (save) api("/api/settings", { method: "PUT", body: { theme } }).catch(() => {});
  }

  // ------------------------------------------------------------------ router
  function route() {
    const hash = location.hash || "#/home";
    const m = hash.match(/^#\/(home|tool|library|models|settings|assistant|storage)(?:\/(\w+))?/);
    const view = m ? m[1] : "home";
    const tool = m && m[2];
    showView(view, tool);
  }

  function showView(view, tool) {
    S.view = view;
    if (tool) S.tool = tool;
    if (S.queue) setTimeout(() => renderQueue(S.queue), 0);
    $$(".view").forEach((v) => { v.hidden = v.id !== `view-${view}`; });
    $$(".nav-item").forEach((a) => {
      const active = a.dataset.view === view && (view !== "tool" || a.dataset.tool === tool);
      a.classList.toggle("active", active);
    });
    const num = $("#topNum");
    if (view === "tool" && tool) {
      S.tool = tool;
      const t = toolOf(tool);
      $("#topTitle").textContent = t ? t.name : nameOf(tool);
      num.hidden = false; num.innerHTML = t ? studioIcon(tool) : ""; num.style.setProperty("--tool", t ? t.color : "var(--muted)");
      $("#topSub").textContent = t ? t.tagline : "";
      mountTool(tool);
      sendFocus(tool);
    } else {
      S.tool = null;
      num.hidden = true;
      $("#topTitle").textContent = { home: "Home", library: "Library", models: "Models", settings: "Settings", assistant: "Assistant", storage: "Storage" }[view] || "Home";
      $("#topSub").textContent = { home: "Orchestrating your GPU", library: "Everything your studios have made", models: "Every studio's models: installed, downloadable, selectable", settings: "How the hub shares the GPU", assistant: "Describe it - the assistant picks the model, LoRAs and prompt, then the studio renders", storage: "Space each studio uses - and a clean-up that only runs when you confirm it" }[view] || "";
      if (view === "assistant" && window.Assistant) window.Assistant.show();
      sendFocus(null);
      if (view === "library") loadLibrary(true);
      if (view === "models") { if (tool) S.models.tool = tool; loadModels(); }
      if (view === "settings") renderSettings(true);
      if (view === "storage") loadStorage(false);
    }
    if (view === "home") { renderHome(); loadRecent(); }
  }

  function sendFocus(tool) {
    if (S.focusSent === tool) return;
    S.focusSent = tool;
    api("/api/focus", { method: "POST", body: { tool } }).catch(() => {});
  }

  // ------------------------------------------------------------------ tool frames
  function mountTool(id) {
    const t = toolOf(id);
    const frames = $("#toolFrames");
    if (!t) return;
    let f = S.frames[id];
    if (!f) {
      f = document.createElement("iframe");
      f.className = "tool-frame";
      f.dataset.tool = id;
      f.title = t.name;
      f.allow = "clipboard-read; clipboard-write; microphone; autoplay; fullscreen; display-capture";
      f.src = proxyUrl(t);
      frames.appendChild(f);
      S.frames[id] = f;
      S.frameReady[id] = false;
      S.frameWasRunning[id] = t.state === "running";
      S.frameGen[id] = t.generation;
      if (t.state === "stopped" || t.state === "error") api(`/api/tools/${id}/start`, { method: "POST" }).catch(() => {});
    }
    Object.values(S.frames).forEach((fr) => { fr.hidden = fr.dataset.tool !== id; });
    updateOverlay();
  }

  function syncFrames() {
    if (!S.state) return;
    for (const [id, f] of Object.entries(S.frames)) {
      const t = toolOf(id);
      if (!t) continue;
      if (t.state === "running") {
        if (S.frameWasRunning[id] && S.frameGen[id] !== t.generation) {
          // The tool was restarted since this frame loaded: load the fresh copy.
          S.frameReady[id] = false;
          f.src = proxyUrl(t);
        }
        S.frameGen[id] = t.generation;
        S.frameWasRunning[id] = true;
      }
    }
    updateOverlay();
  }

  function updateOverlay() {
    const ov = $("#toolOverlay");
    const t = S.tool && toolOf(S.tool);
    if (!t) { ov.hidden = true; return; }
    const show = (t.state === "error") || (!t.enabled) || (!t.installed);
    ov.hidden = !show;
    if (!show) return;
    $("#overlayNum").innerHTML = studioIcon(t.id);
    $("#overlayNum").style.setProperty("--tool", t.color);
    $("#overlayTitle").textContent = t.name;
    $("#overlayText").textContent = !t.enabled ? "This studio is disabled in Settings." : (!t.installed ? t.install_note : (t.error || "The studio stopped."));
    $("#overlayStart").hidden = !t.installed || !t.enabled;
  }

  // ------------------------------------------------------------------ state rendering
  function onState(st) {
    const first = !S.state;
    S.state = st;
    learnTools(st);
    document.body.classList.remove("booting");
    renderTop();
    renderNav();
    renderQueue((st.orchestrator || {}).queue || []);
    if (first && S.view === "tool" && S.tool && !S.frames[S.tool]) mountTool(S.tool);
    if (S.view === "home") renderHome();
    if (S.view === "tool") { const t = toolOf(S.tool); if (t) { $("#topTitle").textContent = t.name; $("#topSub").textContent = t.tagline; const num = $("#topNum"); num.textContent = t.number; num.style.setProperty("--tool", t.color); } }
    syncFrames();
    if (S.view === "settings" && !S.settingsDirty) renderSettings(false);
    if (st.look && st.look !== currentLook() && !S.lookTouched) applyLook(st.look, false);
    if (st.theme && st.theme !== S.theme && !S.themeTouched && currentLook() !== "cinema") applyTheme(st.theme, false);
  }

  function renderTop() {
    const st = S.state, sys = st.system, g = sys.gpu;
    const owner = st.orchestrator.owner;
    const chip = $("#chipOwner");
    chip.classList.toggle("has-owner", !!owner);
    $("#chipOwnerText").textContent = owner ? `GPU → ${nameOf(owner)}` : "GPU free";
    if (g.available) {
      const pct = g.total_mb ? (g.used_mb / g.total_mb) * 100 : 0;
      const fill = $("#meterVramFill");
      fill.style.width = `${pct}%`;
      fill.className = pct > 92 ? "full" : pct > 75 ? "hot" : "";
      $("#meterVramText").textContent = `${gb(g.used_mb)} / ${gb(g.total_mb)}`;
      $("#railGpuFill").style.width = `${pct}%`;
      $("#railGpuText").textContent = `${g.name.replace("NVIDIA ", "")} · ${gb(g.free_mb)} free`;
    } else {
      $("#meterVramText").textContent = "no GPU";
      $("#railGpuText").textContent = "No GPU found";
    }
    const rpct = sys.ram_total_mb ? (sys.ram_used_mb / sys.ram_total_mb) * 100 : 0;
    $("#meterRamFill").style.width = `${rpct}%`;
    $("#meterRamFill").className = rpct > 90 ? "full" : rpct > 78 ? "hot" : "";
    $("#meterRamText").textContent = `${gb(sys.ram_used_mb)} / ${gb(sys.ram_total_mb)}`;
    const busy = Object.values(st.tools).filter((t) => t.summary.busy);
    const jobs = $("#chipJobs");
    jobs.hidden = busy.length === 0;
    if (busy.length) {
      const t = busy[0], j = t.summary.job;
      const pct = j && j.percent != null ? ` ${Math.round(j.percent)}%` : "";
      $("#chipJobsText").textContent = `${t.short}: ${j ? j.title : "working"}${pct}` + (busy.length > 1 ? ` (+${busy.length - 1})` : "");
    }
    $("#conn").classList.toggle("off", !S.connected);
  }

  function stateTone(t) {
    if (!t.enabled) return ["off", "Disabled"];
    if (t.state === "starting") return ["starting", "Starting"];
    if (t.state === "stopping") return ["starting", "Stopping"];
    if (t.state === "error") return ["error", "Error"];
    if (t.state !== "running") return ["off", t.held ? "Paused" : (t.installed ? "Off" : "Not set up")];
    const s = t.summary;
    if (s.busy) return ["busy", "Working"];
    if (s.state === "loading") return ["loading", "Loading"];
    if (s.state === "ready") return ["ready", t.supports_unload === false ? "Ready" : "Model ready"];
    if (s.state === "error") return ["error", "Model error"];
    return ["running", "Idle"];
  }

  function renderNav() {
    for (const id of S.state.order) {
      const t = toolOf(id);
      const [tone] = stateTone(t);
      const dot = $(`#navdot-${id}`);
      if (dot) dot.className = `nav-dot ${tone}`;
    }
  }

  function renderHome() {
    const st = S.state; if (!st) return;
    const g = st.system.gpu, orch = st.orchestrator;
    $("#heroTitle").textContent = greeting();
    $("#gpuName").textContent = g.available ? g.name : "No NVIDIA or AMD GPU detected";
    const pill = $("#gpuOwnerPill");
    pill.className = `pill ${orch.owner ? "owner" : ""}`;
    pill.textContent = orch.owner ? `Held by ${nameOf(orch.owner)}` : "Free";
    if (g.available) {
      $("#vramFree").textContent = gb(g.free_mb) + " free";
      $("#vramTotal").textContent = `of ${gb(g.total_mb)}`;
      // Windows does not report VRAM per process, so the card shows what is in use overall and who holds the GPU.
      $("#segTools").style.width = `${(g.used_mb / g.total_mb) * 100}%`;
      $("#segOther").style.width = "0";
      $("#legTools").textContent = gb(g.used_mb) + (orch.owner ? ` · ${nameOf(orch.owner)}` : "");
      $("#legFree").textContent = gb(g.free_mb);
      $("#factUtil").textContent = `${g.util}% · ${g.temp}°C${g.power_w ? ` · ${g.power_w} W` : ""}`;
    } else {
      $("#vramFree").textContent = "—"; $("#vramTotal").textContent = "";
    }
    $("#factPolicy").textContent = orch.policy === "exclusive" ? "One model at a time" : "Share when it fits";
    $("#factIdle").textContent = `${orch.idle_unload_min > 0 ? Math.round(orch.idle_unload_min) + " min" : "never"} · stop after ${orch.idle_stop_min > 0 ? Math.round(orch.idle_stop_min) + " min" : "never"}`;
    $("#factRam").textContent = `${gb(st.system.ram_used_mb)} of ${gb(st.system.ram_total_mb)}`;
    drawSpark();
    renderToolCards();
    renderActivity($("#activityList"), 8);
  }

  function drawSpark() {
    const c = $("#gpuSpark"); if (!c) return;
    const ctx = c.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth || 560, h = 64;
    if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const hist = S.state.gpu_history || [];
    const total = S.state.system.gpu.total_mb || 1;
    if (hist.length < 2) return;
    const step = w / Math.max(1, 89);
    const css = getComputedStyle(document.documentElement), accent = css.getPropertyValue("--accent").trim() || "#E05A4E", teal = css.getPropertyValue("--teal").trim() || "#1C8C7E";
    const x0 = w - step * (hist.length - 1);
    ctx.beginPath();
    hist.forEach(([, used], i) => { const x = x0 + i * step, y = h - 4 - (used / total) * (h - 10); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.strokeStyle = accent; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.stroke();
    ctx.lineTo(w, h); ctx.lineTo(x0, h); ctx.closePath();
    ctx.globalAlpha = .14; ctx.fillStyle = accent; ctx.fill(); ctx.globalAlpha = 1;
    ctx.beginPath();
    hist.forEach(([, , util], i) => { const x = x0 + i * step, y = h - 4 - (util / 100) * (h - 10); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.strokeStyle = teal; ctx.lineWidth = 1.5; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]);
  }

  function renderToolCards() {
    const host = $("#toolCards");
    const tpl = $("#tplToolCard");
    for (const id of S.state.order) {
      const t = toolOf(id);
      let card = host.querySelector(`[data-tool="${id}"]`);
      if (!card) {
        card = tpl.content.firstElementChild.cloneNode(true);
        card.dataset.tool = id;
        card.addEventListener("click", onCardClick);
        host.appendChild(card);
      }
      card.style.setProperty("--tool", t.color);
      card.classList.toggle("owner", !!t.is_owner);
      $(".tool-num", card).innerHTML = studioIcon(id);
      $(".tool-name", card).textContent = t.name;
      const [tone, label] = stateTone(t);
      const pill = $(".tool-pill", card); pill.className = `pill tool-pill ${tone}`; pill.textContent = label;
      $(".tool-tag", card).textContent = t.tagline;
      const s = t.summary;
      $(".sum-label", card).textContent = t.state === "running" ? s.label : (t.state === "starting" ? "Starting up…" : t.state === "error" ? "Stopped with an error" : t.held ? `Paused - ${t.held_by} is using the GPU` : (t.installed ? "Not running - opens in a few seconds" : "Not set up"));
      $(".sum-detail", card).textContent = t.state === "running" ? (s.detail || "") : (t.state === "error" ? (t.error || "").split("\n")[0] : "");
      const job = $(".job", card);
      if (s.busy && s.job) {
        job.hidden = false;
        $(".job-title", job).textContent = s.job.title;
        const prog = $(".progress", job);
        prog.classList.toggle("indeterminate", s.job.percent == null);
        $("i", prog).style.width = s.job.percent == null ? "" : `${s.job.percent}%`;
        $(".job-meta", job).innerHTML = `<span>${esc(s.job.message || "")}</span><span>${esc(s.job.percent != null ? Math.round(s.job.percent) + "%" : "")}${s.job.eta ? " · " + esc(fmtEta(s.job.eta)) : ""}${s.job.queued ? ` · ${s.job.queued} queued` : ""}</span>`;
      } else job.hidden = true;
      const note = $(".note", card);
      const wd = t.watchdog;
      const wdText = wd ? `Watchdog ${wd.dry_run ? "would have restarted" : "restarted"} ${t.short} ${fmtAgo(wd.ts)}: ${wd.reason}` : "";
      const noteText = !t.installed ? t.install_note : (!t.model_present && t.model_note ? t.model_note : wdText);
      note.hidden = !noteText; note.textContent = noteText;
      const running = t.state === "running";
      const unloadable = t.supports_unload !== false;
      $('[data-act="unload"]', card).hidden = !(running && s.loaded && !s.busy && unloadable);
      $('[data-act="warm"]', card).hidden = !(t.installed && t.enabled && !s.busy && !(running && s.loaded && t.is_owner) && unloadable);
      $('[data-act="stop"]', card).hidden = !(running || t.state === "starting");
      $('[data-act="open"]', card).disabled = !t.enabled || !t.installed;
    }
  }

  async function onCardClick(e) {
    const btn = e.target.closest("[data-act]"); if (!btn) return;
    const id = e.currentTarget.dataset.tool, t = toolOf(id);
    const act = btn.dataset.act;
    try {
      if (act === "open") location.hash = `#/tool/${id}`;
      else if (act === "warm") { toast(`Warming up ${t.name}…`); const r = await api(`/api/tools/${id}/prepare`, { method: "POST" }); if (r.warning) toast(r.warning, "warn"); }
      else if (act === "unload") { await api(`/api/tools/${id}/unload`, { method: "POST" }); toast(`${t.name} unloaded its model`, "ok"); }
      else if (act === "start") { await api(`/api/tools/${id}/start`, { method: "POST" }); }
      else if (act === "stop") { await api(`/api/tools/${id}/stop`, { method: "POST" }); }
      else if (act === "menu") openMenu(btn, id);
    } catch (err) { toast(err.message, "error"); }
  }

  function openMenu(anchor, id) {
    closeMenu();
    const t = toolOf(id);
    const m = document.createElement("div");
    m.className = "menu"; m.id = "ctxMenu";
    const items = [
      ...(t.installed && t.enabled && (t.state === "stopped" || t.state === "error") ? [["Start in the background", () => api(`/api/tools/${id}/start`, { method: "POST" }).catch((e) => toast(e.message, "error"))]] : []),
      ["Open in a new tab", () => window.open(proxyUrl(t), "_blank")],
      ["View log", () => showLog(id)],
      ["Restart", () => api(`/api/tools/${id}/restart`, { method: "POST" }).catch((e) => toast(e.message, "error"))],
      ["Open output folder", () => api(`/api/tools/${id}/open-folder`, { method: "POST" }).catch((e) => toast(e.message, "error"))],
    ];
    for (const [label, fn] of items) { const b = document.createElement("button"); b.textContent = label; b.onclick = () => { closeMenu(); fn(); }; m.appendChild(b); }
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect();
    m.style.left = `${Math.min(r.left, window.innerWidth - 220)}px`;
    m.style.top = `${Math.min(r.bottom + 6, window.innerHeight - 180)}px`;
    setTimeout(() => document.addEventListener("click", closeMenu, { once: true }), 0);
  }
  function closeMenu() { const m = $("#ctxMenu"); if (m) m.remove(); }

  async function showLog(id) {
    const t = toolOf(id);
    const data = await api(`/api/tools/${id}/log?lines=300`);
    openModal(`<div class="card-head"><div><div class="eyebrow">${esc(t.name)} · ${esc(data.state)}</div><h3>Log</h3></div></div>
      <div class="log-view" id="logView">${esc(data.lines.join("\n")) || "(empty)"}</div>
      <p style="color:var(--muted);font-size:13px;margin:10px 0 0">${esc(data.path)}</p>`, true);
    const lv = $("#logView"); lv.scrollTop = lv.scrollHeight;
  }

  // ------------------------------------------------------------------ the GPU queue
  // Generate requests from every studio wait in one queue in the hub and run one after another; a model is only
  // switched between jobs. The chip lists the order; the studio you are looking at says where its request stands.
  // What a queued request is, from the route it came in on (the claim's "METHOD /path").
  const REQUEST_NAMES = [[/\/api\/engine\/(start|restart)/, "Start the video engine"], [/\/api\/generate/, "Generate"], [/\/api\/enhance/, "Rewrite the prompt"],
    [/\/api\/describe/, "Describe a clip"], [/\/api\/media\/cutout/, "Cut out a picture"], [/\/api\/media\/mask_preview/, "Mask preview"],
    [/\/api\/model(s)?\/load/, "Load the model"], [/\/api\/tokenizer\//, "Load the tokenizer"], [/\/api\/tts\//, "Speech"], [/\/api\/drama\//, "Drama scene"],
    [/\/api\/dub/, "Dubbing"], [/\/api\/sts/, "Speech to speech"], [/\/api\/transcribe/, "Transcribe"], [/\/api\/plan/, "Plan a song"], [/\/api\/lyrics/, "Write lyrics"],
    [/\/api\/songs\//, "Song edit"], [/txt2img/, "Text to image"], [/img2img/, "Image to image"], [/queue\/join|predict/, "Generate"], [/extra-/, "Upscale"]];
  const requestName = (reason) => { const r = String(reason || ""); const hit = REQUEST_NAMES.find(([re]) => re.test(r)); return hit ? hit[1] : (r.split(" ").pop() || "Request"); };
  const fmtWait = (since) => { const s = Math.max(0, Date.now() / 1000 - since); return s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min`; };

  function queueRows(q, withButtons) {
    return q.map((e) => {
      const color = TOOL_COLORS[e.tool] || "var(--muted)";
      if (e.status === "running") return `<li class="q-row running" style="--tool:${color}"><span class="q-pos">▶</span><span class="q-ico">${studioIcon(e.tool)}</span>
        <div class="q-main"><b>${esc(e.name)}</b> <span class="q-what">running${e.title ? ` - ${esc(e.title)}` : ""}</span><small>${esc(e.message || "")}${e.percent != null ? ` · ${Math.round(e.percent)}%` : ""}</small></div></li>`;
      const needs = [e.need_vram_gb ? `${e.need_vram_gb} GB VRAM` : "", e.need_ram_gb ? `${e.need_ram_gb} GB RAM` : ""].filter(Boolean).join(" · ");
      const gives = e.gives_up_at ? ` · gives up in ${Math.max(0, Math.round((e.gives_up_at - Date.now() / 1000) / 60))} min` : "";
      return `<li class="q-row" style="--tool:${color}"><span class="q-pos">${e.position}</span><span class="q-ico">${studioIcon(e.tool)}</span>
        <div class="q-main"><b>${esc(e.name)}</b> <span class="q-what">${esc(requestName(e.reason))}</span>
          <small>${esc(e.blocked || "waiting its turn")} · waiting ${fmtWait(e.since)}${needs ? ` · needs ${esc(needs)}` : ""}${gives}</small></div>
        ${withButtons ? `<button class="btn small ghost danger-text" data-skip="${esc(e.id)}" title="Take this request out of the queue - ${esc(e.name)} gets an error and does not run it">Skip</button>` : ""}</li>`;
    }).join("");
  }

  async function skipQueued(id) {
    const e = (S.queue || []).find((x) => x.id === id);
    if (!e) return;
    if (!confirm(`Skip ${e.name}'s "${requestName(e.reason)}"? It is taken out of the GPU queue and does not run; ${e.name} shows an error.`)) return;
    try { await api(`/api/queue/${id}/skip`, { method: "POST" }); toast(`${e.name}: request skipped`, "warn"); }
    catch (err) { toast(err.message, "error"); }
  }

  function openQueue() {
    const q = S.queue || [];
    const wait = (S.state && S.state.settings.claim_wait_max_min) || 0;
    const opts = [[0, "never - wait as long as it takes"], [10, "10 min"], [30, "30 min"], [60, "1 hour"], [120, "2 hours"], [240, "4 hours"]];
    if (!opts.some(([v]) => v === wait)) opts.push([wait, `${wait} min`]);
    openModal(`<div class="queue-modal"><div class="eyebrow">One job on the GPU at a time</div><h3 class="serif" style="font-size:26px;margin:4px 0 6px">GPU queue</h3>
      <p class="lead">Requests from every studio wait here in arrival order while another studio's job runs, so two heavy jobs never overlap and run out of memory. A studio's own follow-up requests (an engine restart, a prompt rewrite) never wait behind its own job.</p>
      <ol class="queue-list" id="queueModalList">${queueRows(q, true) || `<li class="empty">Nothing is queued.</li>`}</ol>
      <div class="field"><label>Queued requests give up after</label><div class="help">Applies to requests waiting now too. A request that gives up (or is skipped) returns an error to its studio.</div>
        <div class="control"><select id="queueWaitMax">${opts.map(([v, l]) => `<option value="${v}" ${v === wait ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div></div></div>`, true);
    $("#queueWaitMax").onchange = async (ev) => { try { await api("/api/settings", { method: "PUT", body: { claim_wait_max_min: parseFloat(ev.target.value) } }); toast("Saved", "ok", "", 2000); } catch (err) { toast(err.message, "error"); } };
  }

  function renderQueue(q) {
    S.queue = q;
    const waiting = q.filter((e) => e.status === "waiting");
    const chip = $("#chipQueue");
    chip.hidden = !waiting.length;
    if (waiting.length) {
      $("#chipQueueNum").textContent = waiting.length;
      $("#chipQueueText").textContent = "waiting";
      chip.title = "GPU queue - runs in this order, one job at a time (click for details, skip):\n" + q.map((e) => e.status === "running"
        ? `▶ ${e.name}${e.title ? ` - ${e.title}` : ""}${e.percent != null ? ` (${Math.round(e.percent)}%)` : ""}`
        : `${e.position}. ${e.name} - ${requestName(e.reason)}`).join("\n");
    }
    const card = $("#queueCard");
    if (card) { card.hidden = !waiting.length; if (waiting.length) $("#queueCardList").innerHTML = queueRows(q, true); }
    const ml = $("#queueModalList");
    if (ml && !$("#modal").hidden) ml.innerHTML = queueRows(q, true) || `<li class="empty">Nothing is queued.</li>`;
    const banner = $("#queueBanner");
    const mine = S.view === "tool" ? waiting.find((e) => e.tool === S.tool) : null;
    banner.hidden = !mine;
    if (mine) {
      const ahead = q.slice(0, q.indexOf(mine)).map((e) => e.status === "running" ? `${e.name} (running)` : e.name);
      banner.innerHTML = `<span class="spinner"></span><span><b>Queued #${mine.position}</b> in the GPU queue · starts after ${esc(ahead.join(" → ") || "the current job")}. It runs on its own - you can leave this page.</span>
        <button class="btn small ghost" data-queue-open>Queue</button><button class="btn small ghost danger-text" data-skip="${esc(mine.id)}">Skip</button>`;
    }
  }

  // ------------------------------------------------------------------ job notifications
  // The hub announces every job that ran longer than a minute (Settings → Notifications). The toast always shows; a
  // browser notification only when it is turned on and allowed. Its text is the studio, the kind of job and how it
  // ended - the prompt only with "Include the prompt" on.
  const STATUS_TEXT = { succeeded: "finished", failed: "failed", cancelled: "was cancelled", interrupted: "was interrupted", finished: "finished" };
  function onJobDone(ev) {
    if (!ev.notify) return;
    const st = S.state ? S.state.settings : {};
    const mins = ev.seconds >= 90 ? `${Math.round(ev.seconds / 60)} min` : `${ev.seconds} s`;
    const head = `${ev.name}: ${ev.label} ${STATUS_TEXT[ev.status] || ev.status}`;
    const bad = ev.status === "failed" || ev.status === "interrupted";
    toast(head, bad ? "error" : "ok", `after ${mins}${st.notify_push_details && ev.title ? ` · ${ev.title}` : ""}`, 10000);
    if (st.notify_browser && "Notification" in window && Notification.permission === "granted") {
      try {
        const n = new Notification(head, { body: `${ev.status} after ${mins}${st.notify_push_details && ev.title ? `\n${ev.title}` : ""}`, tag: `${ev.tool}:${ev.key}`, icon: "/brand/favicon.svg" });
        n.onclick = () => { window.focus(); location.hash = `#/tool/${ev.tool}`; n.close(); };
      } catch (e) { /* some browsers only allow notifications from a service worker */ }
    }
  }
  async function askNotifyPermission() {
    if (!("Notification" in window)) { toast("This browser has no notifications", "warn"); return false; }
    if (!window.isSecureContext) { toast("Browser notifications need the hub opened as http://127.0.0.1 or localhost", "warn", "Over the network the browser blocks them; the push URL works anywhere."); return false; }
    if (Notification.permission === "granted") return true;
    const p = await Notification.requestPermission();
    if (p !== "granted") toast("Notifications are blocked for this page", "warn", "Allow them in the browser's site settings.");
    return p === "granted";
  }

  // ------------------------------------------------------------------ activity
  function onEvent(ev) {
    if (!ev || ev.type === "state") return;
    S.events.push(ev);
    if (S.events.length > 300) S.events.shift();
    if (!ev.replay && (ev.type === "log" && ev.source === "orchestrator" || ev.type === "claim" || ev.type === "waiting" || (ev.type === "log" && ev.level === "error"))) {
      if (!$("#drawer").hidden) { /* visible */ } else { S.unread++; const b = $("#activityBadge"); b.hidden = false; b.textContent = S.unread > 9 ? "9+" : S.unread; }
    }
    if (ev.replay) { /* history for the feed only, never toasts */ }
    else if (ev.type === "claim") {
      if (ev.tool) {
        const acts = (ev.actions || []).join(", ");
        toast(`GPU → ${ev.name}`, ev.warning ? "warn" : "ok", ev.warning || (acts ? `${acts}` : "nothing to unload"));
      } else toast("GPU freed", "ok", (ev.actions || []).join(", "));
    } else if (ev.type === "queue") {
      renderQueue(ev.queue || []);
    } else if (ev.type === "waiting") {
      toast(`${nameOf(ev.tool)} is queued behind ${(ev.for || []).map(nameOf).join(", ")}`, "warn", "It starts automatically when the job ahead of it finishes.", 8000);
    } else if (ev.type === "tool") {
      const name = nameOf(ev.tool);
      if (ev.state === "error" && ev.error) toast(`${name}: ${ev.error.split("\n")[0]}`, "error", "", 9000);
      else if (ev.state === "running") toast(`${name} is ready`, "ok");
    } else if (ev.type === "job") {
      onJobDone(ev);
    } else if (ev.type === "watchdog") {
      toast(`Watchdog ${ev.dry_run ? "(dry run) would restart" : "restarted"} ${ev.name}`, "warn", ev.reason, 12000);
    } else if (ev.type === "log" && (ev.level === "error" || ev.level === "warn") && ev.source && ev.source !== "jobs" && ev.source !== "watchdog") {
      toast(ev.message, ev.level, "", 8000);
    }
    renderActivity($("#activityList"), 8);
    if (!$("#drawer").hidden) renderActivity($("#drawerList"), 200);
  }

  function renderActivity(ul, limit) {
    if (!ul) return;
    const evs = S.events.filter((e) => (e.type === "log" && e.level !== "debug") || e.type === "claim" || e.type === "waiting").slice(-limit).reverse();
    ul.innerHTML = evs.map((e) => {
      const who = e.tool ? (toolOf(e.tool) ? toolOf(e.tool).short : e.tool) : (e.source === "orchestrator" ? "GPU" : "Hub");
      const color = e.tool ? (TOOL_COLORS[e.tool] || "var(--muted)") : "var(--muted)";
      const text = e.type === "claim" ? (e.tool ? `GPU handed to ${e.name}${e.actions && e.actions.length ? " - " + e.actions.join(", ") : ""}` : `GPU freed - ${(e.actions || []).join(", ")}`) : e.type === "waiting" ? `${nameOf(e.tool)} waits for ${(e.for || []).map(nameOf).join(", ")}` : e.message;
      return `<li class="${esc(e.level || "")}"><time>${fmtTime(e.ts)}</time><span class="who" style="--tool:${color}">${esc(who)}</span><span class="msg">${esc(text)}</span></li>`;
    }).join("") || `<li><time></time><span class="who">Hub</span><span class="msg" style="color:var(--muted)">Nothing yet.</span></li>`;
  }

  // ------------------------------------------------------------------ library
  function mediaCard(it) {
    const color = TOOL_COLORS[it.tool] || "var(--muted)";
    const kindLabel = { image: "image", audio: it.tool === "tts" ? "voice" : "audio", video: "video", song: "song" }[it.kind] || it.kind;
    let thumb;
    if (it.kind === "image" || (it.kind === "video" && it.thumb)) {
      thumb = `<div class="media-thumb"><img loading="lazy" src="${esc(it.thumb || it.url)}" alt="">${it.kind === "video" ? `<span class="play"><svg viewBox="0 0 24 24"><path d="M7 5v14l12-7z"/></svg></span>` : ""}${it.duration ? `<span class="dur">${fmtDur(it.duration)}</span>` : ""}</div>`;
    } else {
      const bars = Array.from({ length: 22 }, (_, i) => `<i style="--h:${8 + Math.round(Math.abs(Math.sin(i * 1.7 + it.title.length)) * 34)}px"></i>`).join("");
      thumb = `<div class="media-thumb audio" style="--tool:${color}"><div class="wave">${bars}</div>${it.duration ? `<span class="dur">${fmtDur(it.duration)}</span>` : ""}</div>`;
    }
    return `<button class="media-card" data-id="${esc(it.id)}" style="--tool:${color}">${thumb}
      <div class="media-body"><div class="media-title">${esc(it.title)}</div><div class="media-sub">${esc(it.subtitle)}</div>
      <div class="media-meta"><b>${esc(kindLabel)}</b><span>${esc(fmtAgo(it.created))}</span></div></div></button>`;
  }

  async function loadLibrary(reset) {
    if (S.lib.loading) return;
    if (reset) { S.lib.offset = 0; S.lib.items = []; $("#libGrid").innerHTML = ""; }
    S.lib.loading = true;
    try {
      const params = new URLSearchParams({ tool: S.lib.tool, q: S.lib.q, offset: S.lib.offset });
      const data = await api(`/api/library?${params}`);
      S.lib.total = data.total; S.lib.counts = data.counts;
      S.lib.items.push(...data.items);
      S.lib.offset += data.items.length;
      $("#libGrid").insertAdjacentHTML("beforeend", data.items.map(mediaCard).join(""));
      $("#libCount").textContent = `${S.lib.total} item${S.lib.total === 1 ? "" : "s"}`;
      $("#libMore").hidden = S.lib.offset >= S.lib.total;
      if (!S.lib.total) $("#libGrid").innerHTML = `<div class="empty">Nothing here yet${S.lib.q ? " for this search" : ""}.</div>`;
      $$("#libChips .chip").forEach((c) => { const n = c.dataset.tool ? data.counts[c.dataset.tool] : Object.values(data.counts).reduce((a, b) => a + b, 0); c.textContent = `${c.textContent.replace(/\s\(\d+\)$/, "")} (${n})`; });
    } catch (e) { toast(e.message, "error"); }
    S.lib.loading = false;
  }

  async function loadRecent() {
    if (Date.now() - S.recentLoadedAt < 15000) return;
    S.recentLoadedAt = Date.now();
    try {
      const data = await api("/api/library?limit=8");
      const strip = $("#recentStrip");
      strip.innerHTML = data.items.length ? data.items.map(mediaCard).join("") : `<div class="empty">Nothing yet - your first creations will show up here.</div>`;
      S.recentItems = data.items;
    } catch (e) { /* ignore */ }
  }

  function openViewer(it) {
    const color = TOOL_COLORS[it.tool] || "var(--muted)";
    let media;
    if (it.kind === "image") media = `<div class="viewer-media"><img src="${esc(it.url)}" alt=""></div>`;
    else if (it.kind === "video") media = `<div class="viewer-media"><video src="${esc(it.url)}" controls autoplay playsinline></video></div>`;
    else media = `<div class="viewer-media audio" style="--tool:${color}"><div><div class="viewer-audio-art">${it.kind === "song" ? "♪" : "🎙"}</div><audio id="viewerAudio" src="${esc(it.url)}" controls autoplay></audio></div></div>`;
    const kv = [["Studio", nameOf(it.tool)], ["Model", it.model], ["Created", new Date(it.created * 1000).toLocaleString()],
      it.width ? ["Size", `${it.width} × ${it.height}`] : null, it.duration ? ["Length", fmtDur(it.duration)] : null, it.size ? ["File", (it.size / 1048576).toFixed(1) + " MB"] : null]
      .filter(Boolean).map(([k, v]) => `<dt>${esc(k)}</dt><dd title="${esc(v)}">${esc(v)}</dd>`).join("");
    const versions = (it.versions || []).map((v) => `<button class="btn small" data-src="${esc(v.url)}">${esc(v.name)}</button>`).join("");
    openModal(`<div class="viewer">${media}<div class="viewer-side" style="--tool:${color}">
      <div class="eyebrow" style="color:${color}">${esc(nameOf(it.tool))}</div>
      <h3>${esc(it.title)}</h3><div style="color:var(--muted);font-size:14px">${esc(it.subtitle)}</div>
      <dl class="kv">${kv}</dl>
      ${versions ? `<div class="eyebrow">Versions</div><div class="versions"><button class="btn small" data-src="${esc(it.url)}">original</button>${versions}</div>` : ""}
      ${it.lyrics ? `<div class="eyebrow">Lyrics</div><div class="lyrics">${esc(it.lyrics)}</div>` : ""}
      <div class="viewer-actions"><button class="btn primary" id="viewerSend" title="Hand this file to another studio - no download, no upload">Send to…</button>
        <a class="btn" href="${esc(it.download)}">Download</a>
        <button class="btn" id="viewerFolder">Open folder</button>
        <a class="btn ghost" href="#/tool/${esc(it.tool)}" id="viewerOpenTool">Open ${esc(nameOf(it.tool))}</a></div>
    </div></div>`);
    $("#viewerFolder").onclick = () => api("/api/library/open", { method: "POST", body: { folder: it.folder } }).catch((e) => toast(e.message, "error"));
    $("#viewerOpenTool").onclick = closeModal;
    $("#viewerSend").onclick = (e) => { e.stopPropagation(); openSendMenu($("#viewerSend"), it); };
    $$("#modalBody .versions button").forEach((b) => { b.onclick = () => { const a = $("#viewerAudio"); if (a) { a.src = b.dataset.src; a.play(); } }; });
  }

  // ------------------------------------------------------------------ "Send to": hand an output to another studio
  const kindOf = (it) => ({ song: "audio" }[it.kind] || it.kind);
  async function handoffTargets() {
    if (!S.handoff) { try { S.handoff = (await api("/api/handoff/targets")).targets; } catch (e) { S.handoff = []; } }
    return S.handoff;
  }
  async function openSendMenu(anchor, it) {
    closeMenu();
    const kind = kindOf(it);
    const targets = (await handoffTargets()).map((t) => ({ ...t, slots: t.slots.filter((s) => s.kinds.includes(kind)) })).filter((t) => t.slots.length);
    const m = document.createElement("div");
    m.className = "menu send-menu"; m.id = "ctxMenu";
    if (!targets.length) { const p = document.createElement("div"); p.className = "menu-note"; p.textContent = `No studio takes a ${kind} yet.`; m.appendChild(p); }
    for (const t of targets) {
      const h = document.createElement("div"); h.className = "menu-head"; h.style.setProperty("--tool", t.color); h.innerHTML = `<span class="menu-num">${studioIcon(t.id)}</span>${esc(t.name)}`; m.appendChild(h);
      for (const s of t.slots) { const b = document.createElement("button"); b.textContent = s.label; b.onclick = () => { closeMenu(); sendTo(it, t.tool, s); }; m.appendChild(b); }
    }
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect();
    m.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - 340))}px`;
    m.style.top = `${Math.min(r.bottom + 6, window.innerHeight - m.offsetHeight - 12)}px`;
    setTimeout(() => document.addEventListener("click", closeMenu, { once: true }), 0);
  }
  function waitFor(pred, timeoutMs, every = 250) {
    return new Promise((resolve) => { const t0 = Date.now(); const tick = () => { if (pred()) return resolve(true); if (Date.now() - t0 > timeoutMs) return resolve(false); setTimeout(tick, every); }; tick(); });
  }
  async function sendTo(it, tool, slot) {
    const target = toolOf(tool); if (!target) return;
    const rel = it.url.replace(/^\/media\/[^/]+\//, "").split("?")[0];
    const detail = { type: "hub:import", slot: slot.id, slotLabel: slot.label, url: `/__hub/media/${it.tool}/${rel}`, name: rel.split("/").pop(), kind: kindOf(it),
      from: { tool: it.tool, name: nameOf(it.tool), title: it.title || "" }, meta: { width: it.width, height: it.height, duration: it.duration, model: it.model } };
    closeModal();
    toast(`Sending to ${target.name}…`, "", slot.label, 4000);
    location.hash = `#/tool/${tool}`;
    const ready = await waitFor(() => S.frameReady[tool] && S.frames[tool], 240000);
    if (!ready) { toast(`${target.name} did not come up in time`, "error", "Start it from Home and try again.", 9000); return; }
    try { S.frames[tool].contentWindow.postMessage(detail, "*"); } catch (e) { toast(`Could not reach ${target.name}`, "error", String(e.message || e)); }
  }
  window.hubSendTo = (itemId, tool, slotId) => {   // for tests: hubSendTo("image:2026-09-29/x", "video", "i2v")
    const it = [...S.lib.items, ...(S.recentItems || [])].find((x) => x.id === itemId); if (!it) return Promise.reject(new Error("item not loaded in the shell"));
    return handoffTargets().then((ts) => { const t = ts.find((x) => x.tool === tool); const s = t && t.slots.find((x) => x.id === slotId); if (!s) throw new Error("no such slot"); return sendTo(it, tool, s); });
  };

  // for tests: render a made-up queue / job event without running anything (hubTest.queue([...]), hubTest.job({...}))
  window.hubTest = { queue: (q) => renderQueue(q), job: (ev) => onJobDone(ev), openQueue: () => openQueue(),
    watchdog: (tool, a) => { const t = toolOf(tool); if (t) { t.watchdog = a; renderHome(); } }, event: (ev) => onEvent(ev) };

  // ------------------------------------------------------------------ settings
  const SETTINGS_SCHEMA = [
    { title: "Graphics card", lead: "How the hub shares one GPU between your studios.", fields: [
      { key: "gpu_policy", label: "Sharing policy", type: "select", options: [["auto", "Automatic (recommended)"], ["exclusive", "One model at a time"], ["budget", "Share when it fits"]], help: "Automatic keeps one model at a time on cards under 20 GB and lets small models share on bigger cards." },
      { key: "prepare_on_switch", label: "Load ahead when I switch studios", type: "bool", help: "Off (recommended with the GPU queue): a model loads only when a job reaches the front of the queue, so opening a studio never unloads another. On: a few seconds after you open a studio its model is loaded, so its first Generate is instant - when the GPU is free." },
      { key: "prepare_delay_s", label: "Delay before loading ahead", type: "number", step: 0.5, min: 0.5, max: 60, unit: "s" },
      { key: "idle_unload_min", label: "Unload an idle model after", type: "number", step: 1, min: -1, max: 1440, unit: "min", help: "0 = automatic (8 min on 8 GB cards, longer on bigger ones), -1 = never." },
      { key: "idle_stop_min", label: "Stop an idle studio after", type: "number", step: 1, min: -1, max: 1440, unit: "min", help: "Frees the RAM and the CUDA context too. 0 = automatic, -1 = never. Pinned studios are never stopped." },
      { key: "claim_wait_max_min", label: "Queued requests give up after", type: "number", step: 1, min: 0, max: 1440, unit: "min", help: "Generate requests from every studio wait in one queue and run one after another; a model is switched only between jobs. 0 = wait as long as it takes." },
      { key: "vram_headroom_gb", label: "VRAM headroom", type: "number", step: 0.1, min: 0, max: 8, unit: "GB", help: "Extra free memory the hub tries to keep for the desktop and the browser." },
      { key: "release_ollama", label: "Release Ollama models when VRAM is short", type: "bool", help: "Music Studio's lyric writer uses a local Ollama model, which stays resident for minutes." },
      { key: "pin_memory", label: "Pinned memory in the studios", type: "bool", help: "Page-locked RAM speeds up the CPU↔GPU weight transfers of offload modes. Applies the next time a studio starts; each studio can override it in its own settings. Turn off when RAM is short (studios default to off on AMD)." },
    ] },
    { title: "Notifications", lead: "When a job that ran longer than a minute finishes or fails in any studio.", fields: [
      { key: "notify_browser", label: "Browser notifications", type: "bool", help: "Shown by this browser while a hub tab is open (it asks for permission when you turn this on)." },
      { key: "notify_push_url", label: "Push URL", type: "text", placeholder: "https://ntfy.sh/your-own-topic", help: "Optional: an ntfy.sh-style topic URL of your own; the hub POSTs a short message to it. Off when empty. It sends the studio, how the job ended, the kind of job and how long it ran - no prompts or files." },
      { key: "notify_push_details", label: "Include the prompt", type: "bool", help: "Also send the job's title (the start of its prompt) and an error message. Off by default." },
      { key: "notify_min_s", label: "Only for jobs longer than", type: "number", step: 10, min: 0, max: 7200, unit: "s" },
      { key: "_notify_test", label: "Test the push URL", type: "button", text: "Send a test", help: "Sends a sample message (no details) to the push URL." },
    ] },
    { title: "Health watchdog", lead: "Restarts a studio that hung - never one whose job is still moving.", fields: [
      { key: "watchdog_enabled", label: "Watch the studios", type: "bool", help: "Checks every 30 s. Before a restart it also checks that the GPU and (Video Studio) the engine log have gone quiet. At most one restart per studio per 30 min; it shows in Activity and on the studio's card." },
      { key: "watchdog_stall_min", label: "Restart when a running job has not moved for", type: "number", step: 1, min: 5, max: 240, unit: "min", help: "Its progress, stage and step all unchanged. Video Studio's MiniMax H3 moves every ~20 s step." },
      { key: "watchdog_health_min", label: "… or the studio has not answered for", type: "number", step: 1, min: 2, max: 120, unit: "min", help: "Every status check failed while its container is up." },
      { key: "watchdog_dry_run", label: "Dry run", type: "bool", help: "Only log what it would restart." },
    ] },
    { title: "Appearance", lead: "The theme of the shell and, if you like, of the studios inside it.", fields: [
      { key: "theme", label: "Theme", type: "select", options: [["light", "Light"], ["dark", "Dark"]] },
      { key: "restyle_tools", label: "Keep the studios' theme in step with the shell", type: "bool", help: "Light or dark follows the shell inside every studio. Turn off to let each studio keep its own setting. Takes effect when a studio is reloaded." },
      { key: "brand_fonts_in_tools", label: "Hub font inside the studios", type: "bool", help: "Use the hub's font (Inter) for headings and buttons inside older studio versions that do not ship it themselves." },
    ] },
    { title: "Network", lead: "Changes here need a restart of the hub.", fields: [
      { key: "bind_host", label: "Listen on", type: "select", options: [["127.0.0.1", "This PC only"], ["0.0.0.0", "Everyone on my network"]], help: "Sharing on the network also needs a firewall rule for ports 7900-7905 (see README)." },
      { key: "hub_port", label: "Hub port", type: "number", step: 1, min: 1024, max: 65535 },
      { key: "open_browser", label: "Open the browser on start", type: "bool" },
      { key: "stop_tools_on_exit", label: "Stop all studios when the hub closes", type: "bool" },
    ] },
    { title: "Models", lead: "Tokens the Models page uses for downloads. Stored on this PC only.", fields: [
      { key: "hf_token", label: "HuggingFace token", type: "password", help: "Needed for gated repositories (huggingface.co/settings/tokens)" },
      { key: "civitai_token", label: "Civitai API key", type: "password", help: "Needed for some Civitai downloads (civitai.com/user/account)" },
    ] },
  ];

  function renderSettings(rebuild) {
    const st = S.state; if (!st) return;
    const grid = $("#settingsGrid");
    const s = st.settings;
    if (rebuild || !grid.children.length) {
      grid.innerHTML = SETTINGS_SCHEMA.map((sec) => `<section class="card"><h3>${esc(sec.title)}</h3><p class="lead">${esc(sec.lead)}</p>
        ${sec.fields.map((f) => `<div class="field" data-key="${f.key}"><label>${esc(f.label)}</label>${f.help ? `<div class="help">${esc(f.help)}</div>` : ""}<div class="control">${control(f, s[f.key])}</div></div>`).join("")}</section>`).join("")
        + `<section class="card"><h3>Studios</h3><p class="lead">Ports are used on this PC only; the shell talks to the entrance port. Pinned studios are never stopped for being idle.</p><div class="tool-settings" id="toolSettings"></div></section>`
        + `<section class="card"><h3>Check-up</h3><p class="lead">What the hub found on this PC.</p><ul class="doctor" id="doctorList"><li>Loading…</li></ul></section>`
        + `<section class="card"><h3>Keyboard</h3><p class="lead">Shortcuts work anywhere in the shell.</p><div class="shortcuts"><kbd>Alt</kbd>+<kbd>1</kbd>…<kbd>${S.state.order.length}</kbd><span>Open a studio</span><kbd>Alt</kbd>+<kbd>H</kbd><span>Home</span><kbd>Alt</kbd>+<kbd>L</kbd><span>Library</span><kbd>Alt</kbd>+<kbd>M</kbd><span>Models</span><kbd>Alt</kbd>+<kbd>A</kbd><span>Activity</span><kbd>Alt</kbd>+<kbd>T</kbd><span>Theme</span><kbd>Alt</kbd>+<kbd>G</kbd><span>Free the GPU</span></div>
          <p class="lead" style="margin-top:14px">${esc(st.app.name)} ${esc(st.app.version)} · ${esc(st.app.platform || "")} · hub on port ${st.app.hub_port} · <a href="/api/docs" target="_blank">API</a></p></section>`;
      grid.querySelectorAll(".field").forEach((f) => f.addEventListener("change", onSettingChange));
      grid.querySelectorAll("[data-action='_notify_test']").forEach((b) => b.addEventListener("click", async () => {
        b.disabled = true;
        try { const r = await api("/api/notify/test", { method: "POST", body: {} }); toast("Test sent", "ok", r.info, 4000); }
        catch (err) { toast(err.message, "error"); }
        b.disabled = false;
      }));
      grid.querySelectorAll(".switch").forEach((b) => b.addEventListener("click", () => { b.classList.toggle("on"); onSettingChange({ currentTarget: b.closest(".field, .tool-row"), target: b }); }));
      loadDoctor();
    }
    renderToolSettings(s);
  }

  function control(f, val) {
    if (f.type === "bool") return `<button type="button" class="switch ${val ? "on" : ""}" data-key="${f.key}" aria-label="${esc(f.label)}"></button>`;
    if (f.type === "button") return `<button type="button" class="btn small" data-action="${f.key}">${esc(f.text || f.label)}</button>`;
    if (f.type === "password" || f.type === "text") return `<input type="${f.type}" data-key="${f.key}" value="${esc(val || "")}" placeholder="${esc(f.placeholder || (f.type === "password" ? "paste a token" : ""))}" autocomplete="off" spellcheck="false">`;
    if (f.type === "select") return `<select data-key="${f.key}">${f.options.map(([v, l]) => `<option value="${esc(v)}" ${String(val) === String(v) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
    return `<input type="number" data-key="${f.key}" value="${esc(val)}" step="${f.step || 1}" ${f.min != null ? `min="${f.min}"` : ""} ${f.max != null ? `max="${f.max}"` : ""}>${f.unit ? `<span class="unit">${esc(f.unit)}</span>` : ""}`;
  }

  async function onSettingChange(e) {
    const field = e.currentTarget;
    const ctl = field.querySelector("[data-key]") || e.target;
    const key = ctl.dataset.key;
    if (!key || field.classList.contains("tool-row")) return;
    let val;
    if (ctl.classList.contains("switch")) val = ctl.classList.contains("on");
    else if (ctl.tagName === "SELECT") val = ctl.value;
    else if (ctl.type === "password" || ctl.type === "text") val = ctl.value.trim();
    else val = parseFloat(ctl.value);
    if (key === "notify_browser" && val && !(await askNotifyPermission())) { ctl.classList.remove("on"); return; }
    try {
      S.settingsDirty = true;
      await api("/api/settings", { method: "PUT", body: { [key]: val } });
      if (key === "theme") { S.themeTouched = true; applyTheme(val, false); }
      toast("Saved", "ok", ["bind_host", "hub_port"].includes(key) ? "Restart the hub to apply." : "", 2500);
    } catch (err) { toast(err.message, "error"); }
    S.settingsDirty = false;
  }

  function renderToolSettings(s) {
    const host = $("#toolSettings"); if (!host) return;
    host.innerHTML = S.state.order.map((id) => { const t = toolOf(id), c = s.tools[id] || {}; return `<div class="tool-row" data-tool="${id}" style="--tool:${t.color}">
      <div class="tool-num">${studioIcon(id)}</div>
      <div class="row-main"><b>${esc(t.name)}</b><small title="${esc(t.dir)}">${esc(t.dir)}</small><small>backend :${esc(t.port || c.port)} · entrance :${esc(t.proxy_port)} · ${t.installed ? (t.backend === "docker" ? "container " + esc(t.container) : "installed") : "not set up"}</small></div>
      <div class="row-ctl">
        <label>Enabled <button type="button" class="switch ${c.enabled ? "on" : ""}" data-tkey="enabled"></button></label>
        <label>Start with hub <button type="button" class="switch ${c.autostart ? "on" : ""}" data-tkey="autostart"></button></label>
        <label>Pinned <button type="button" class="switch ${c.pinned ? "on" : ""}" data-tkey="pinned"></button></label>
        ${id === "tts" ? `<label title="Also start the Chatterbox clone engine with Voice Studio">Chatterbox <button type="button" class="switch ${c.chatterbox !== false ? "on" : ""}" data-tkey="chatterbox"></button></label>` : ""}
        ${t.port_fixed ? `<label title="Set by the studio's linux/compose.yml">Port <input type="number" value="${esc(t.port)}" disabled></label>` : `<label>Port <input type="number" data-tkey="port" value="${esc(c.port)}" min="1024" max="65535"></label>`}
        <label class="wide">Folder <input type="text" data-tkey="dir" value="${esc(c.dir || "")}" placeholder="folder name or full path" title="Folder name inside (or next to) the hub folder, or a full path"></label>
      </div></div>`; }).join("");
    host.querySelectorAll(".switch").forEach((b) => b.addEventListener("click", async () => {
      b.classList.toggle("on");
      const id = b.closest(".tool-row").dataset.tool;
      try { await api("/api/settings", { method: "PUT", body: { tools: { [id]: { [b.dataset.tkey]: b.classList.contains("on") } } } }); toast("Saved", "ok", b.dataset.tkey === "enabled" ? "Restart the hub to apply." : "", 2500); }
      catch (err) { toast(err.message, "error"); }
    }));
    host.querySelectorAll("input[data-tkey]").forEach((inp) => inp.addEventListener("change", async () => {
      const id = inp.closest(".tool-row").dataset.tool;
      const value = inp.type === "number" ? parseInt(inp.value, 10) : inp.value.trim();
      try { await api("/api/settings", { method: "PUT", body: { tools: { [id]: { [inp.dataset.tkey]: value } } } }); toast("Saved", "ok", "Applies the next time the studio starts.", 2500); }
      catch (err) { toast(err.message, "error"); }
    }));
  }

  async function loadDoctor() {
    try {
      const d = await api("/api/doctor");
      $("#doctorList").innerHTML = d.checks.map((c) => `<li><span class="${c.ok ? "ok" : "bad"}">${c.ok ? "✓" : "✕"}</span><div>${esc(c.name)}<small>${esc(c.detail)}</small></div></li>`).join("");
    } catch (e) { /* ignore */ }
  }


  // ------------------------------------------------------------------ models
  const fmtBytes = (n) => { if (!n) return ""; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return `${n.toFixed(i >= 3 ? 1 : 0)} ${u[i]}`; };
  const isActiveDl = (d) => d && ["queued", "running", "verifying"].includes(d.status);

  async function loadModels(force) {
    const M = S.models;
    if (M.loading) return;
    M.loading = true;
    if (!M.data || force) $("#modelCount").textContent = "scanning…";
    try {
      M.data = await api("/api/models");
      M.downloads = {};
      for (const d of M.data.downloads || []) M.downloads[d.id] = d;
      renderModelChips();
      renderModels();
      renderDownloads();
    } catch (e) { $("#modelsGrid").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
    M.loading = false;
  }

  function renderModelChips() {
    const host = $("#modelChips");
    const built = host.dataset.built;
    const ids = S.models.data.studios.map((s) => s.tool).join(",");
    if (built === ids) return;
    host.dataset.built = ids;
    host.innerHTML = `<button class="chip filter ${S.models.tool ? "" : "active"}" data-tool="">All studios</button>` + S.models.data.studios.map((s) => `<button class="chip filter ${S.models.tool === s.tool ? "active" : ""}" data-tool="${s.tool}" style="--tool:${s.color}">${esc(s.name)}</button>`).join("");
  }

  function dlFor(tool, kind, name) {
    return Object.values(S.models.downloads).find((d) => isActiveDl(d) && d.tool === tool && d.kind === kind && d.name === name);
  }

  function renderModels() {
    const M = S.models; if (!M.data) return;
    const studios = M.data.studios.filter((s) => !M.tool || s.tool === M.tool);
    let count = 0, bytes = 0;
    const html = studios.map((s) => {
      const kinds = (s.kinds || []).map((k) => {
        const items = k.layout === "delegated" ? k.items : k.installed;
        count += items.filter((i) => i.installed !== false).length;
        bytes += items.reduce((a, i) => a + (i.installed === false ? 0 : (i.size || 0)), 0);
        const rows = items.map((it) => modelRow(s, k, it, false)).join("");
        const avail = k.layout === "delegated" ? "" : (k.catalog || []).filter((c) => !c.installed).map((c) => modelRow(s, k, c, true)).join("");
        const canAdd = k.layout !== "delegated" && (k.sources || []).some((x) => x !== "hf" || k.layout === "folder");
        return `<div class="mk" data-kind="${k.id}">
          <div class="mk-head"><div><b>${esc(k.label)}</b><small title="${esc(k.dir || "")}">${esc(k.dir || "")}</small></div>
            <span class="mk-count">${items.filter((i) => i.installed !== false).length}</span>
            ${canAdd ? `<button class="btn small" data-act="add" data-kind="${k.id}">Add…</button>` : ""}</div>
          ${rows || avail ? `<div class="mk-rows">${rows}${avail ? `<div class="mk-sub">Available</div>${avail}` : ""}</div>` : `<div class="empty small">Nothing here yet${canAdd ? " - use Add… to download one" : ""}.</div>`}
        </div>`;
      }).join("");
      const off = s.mode === "delegated" && s.offline ? `<div class="note warn">${esc(s.name)} is not running - start it to see and manage its models.${s.error ? " " + esc(s.error) : ""} <button class="btn small" data-act="start">Start</button></div>` : "";
      return `<section class="card studio-models" data-tool="${s.tool}" style="--tool:${s.color}">
        <div class="sm-head"><div class="tool-num">${studioIcon(s.tool)}</div><div><h3>${esc(s.name)}</h3><p class="lead">${esc(s.note || "")}</p></div>${s.active && s.active.model ? `<span class="pill owner" title="Model in use">${esc(String(s.active.model).split("/").pop())}</span>` : ""}</div>
        ${off}${kinds}
      </section>`;
    }).join("");
    $("#modelsGrid").innerHTML = html || `<div class="empty">No studios.</div>`;
    $("#modelCount").textContent = count ? `${count} installed · ${fmtBytes(bytes)}` : "";
  }

  function modelRow(s, k, it, available) {
    const name = it.name;
    const dl = dlFor(s.tool, k.id, name);
    const active = s.active || {};
    const inUse = (k.id === "model" && (active.model === name || active.model === name.split("/").pop())) || (k.id === "checkpoint" && active.checkpoint && (active.checkpoint === name || active.checkpoint.startsWith(name.replace(/\.[^.]+$/, "")))) || (k.id === "variant" && Object.values(active).includes(name));
    const label = it.label && it.label !== name ? `<b>${esc(it.label)}</b><small class="mono">${esc(name)}</small>` : `<b class="${name.includes("/") ? "mono" : ""}">${esc(name)}</b>`;
    const detail = [it.detail, it.group && it.group !== "tokenizer" ? it.group : "", it.partial ? "partially downloaded" : ""].filter(Boolean).join(" · ");
    const size = it.size_h || fmtBytes(it.size);
    const date = it.modified ? fmtAgo(it.modified) : "";
    const btn = (act, text, cls = "", title = "") => `<button class="btn small ${cls}" data-act="${act}" data-kind="${k.id}" data-name="${esc(name)}" title="${esc(title)}">${text}</button>`;
    let actions = "";
    if (dl) actions = `<span class="dl-inline"><span class="spinner"></span> ${dl.percent != null ? dl.percent + "%" : dl.status}</span>${btn("cancel-dl", "Cancel", "ghost", "")}`;
    else if (k.layout === "delegated") {
      if (it.installed) actions = (k.selectable && it.group !== "tokenizer" ? btn("use", inUse ? "In use" : "Use", inUse ? "primary" : "", "Make this the studio's model") : "") + (s.tool === "video" ? btn("dl-delegated", "Redownload", "ghost") : "") + (it.group !== "tokenizer" ? btn("delete", "Delete", "ghost danger-text") : "");
      else actions = btn("dl-delegated", it.partial ? "Resume" : "Download", "", s.tool === "tts" ? "Loads the model, downloading it first" : "");
    } else if (available) actions = btn("download", "Download", "", `From ${it.source && it.source.repo ? it.source.repo : it.source && it.source.url ? it.source.url : "the source"}`);
    else {
      const cat = (k.catalog || []).find((c) => c.name === name);
      actions = (k.selectable ? btn("use", inUse ? "In use" : "Use", inUse ? "primary" : "", "Switch the studio to this model") : "")
        + (cat ? btn("verify", "Verify", "ghost", "Check every file against the source") + btn("download", "Redownload", "ghost", "Fetch again from the source (missing or wrong files are replaced)") : "")
        + btn("delete", "Delete", "ghost danger-text", "Remove from disk");
    }
    return `<div class="mrow ${available ? "avail" : ""} ${inUse ? "inuse" : ""}">
      <div class="mrow-main">${label}${detail ? `<small>${esc(detail)}</small>` : ""}</div>
      <div class="mrow-meta">${esc(size)}${date ? `<small>${esc(date)}</small>` : ""}</div>
      <div class="mrow-actions">${actions}</div>
    </div>`;
  }

  async function onModelAction(e) {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const card = b.closest(".studio-models"); if (!card) return;
    const tool = card.dataset.tool, act = b.dataset.act, kind = b.dataset.kind, name = b.dataset.name;
    const s = S.models.data.studios.find((x) => x.tool === tool);
    const k = s && (s.kinds || []).find((x) => x.id === kind);
    const busy = (on) => { b.disabled = on; };
    try {
      if (act === "start") { await api(`/api/tools/${tool}/start`, { method: "POST" }); toast(`${s.name} is starting`, "ok", "The models appear when it is ready."); setTimeout(() => loadModels(true), 6000); setTimeout(() => loadModels(true), 20000); }
      else if (act === "add") openAddModel(s, k);
      else if (act === "download") {
        const cat = (k.catalog || []).find((c) => c.name === name);
        if (!cat) return;
        busy(true);
        const r = await api("/api/models/download", { method: "POST", body: { tool, kind, name, source: cat.source } });
        S.models.downloads[r.download.id] = r.download; renderDownloads(); renderModels();
        toast(`Downloading ${name}`, "ok", cat.size_h ? `${cat.size_h} - see the Downloads panel` : "");
      }
      else if (act === "dl-delegated") {
        busy(true);
        const body = tool === "video" ? { tool, variant_id: name } : { tool, name };
        await api("/api/models/download", { method: "POST", body });
        toast(`${s.name} is downloading ${name.split("/").pop()}`, "ok", tool === "video" ? "Progress shows on its Models tab and here." : "It loads when the download completes.");
        setTimeout(() => loadModels(true), 3000);
      }
      else if (act === "cancel-dl") { const d = dlFor(tool, kind, name); if (d) await api(`/api/models/downloads/${d.id}/cancel`, { method: "POST", body: {} }); }
      else if (act === "use") {
        busy(true); b.innerHTML = `<span class="spinner"></span>`;
        const r = await api("/api/models/use", { method: "POST", body: { tool, kind, name } });
        toast(r.message || "Switched", "ok");
        setTimeout(() => loadModels(true), 1500);
      }
      else if (act === "verify") {
        busy(true); b.innerHTML = `<span class="spinner"></span>`;
        const r = await api("/api/models/verify", { method: "POST", body: { tool, kind, name } });
        toast(`${name}: ${r.message}`, r.ok ? "ok" : "warn", (r.missing || []).slice(0, 3).join(", "), 9000);
        b.disabled = false; b.textContent = "Verify";
      }
      else if (act === "delete") {
        if (!confirm(`Delete ${name} from ${s.name}? This removes it from disk.`)) return;
        busy(true);
        await api("/api/models/delete", { method: "POST", body: { tool, kind, name } });
        toast(`Deleted ${name.split("/").pop()}`, "ok");
        loadModels(true);
      }
    } catch (err) { toast(err.message, "error", "", 9000); busy(false); if (act === "use") b.textContent = "Use"; if (act === "verify") b.textContent = "Verify"; }
  }

  // ---- Add… dialog: HuggingFace repo / file, Civitai search, direct URL
  function openAddModel(s, k) {
    const sources = k.sources || ["hf", "url"];
    const tabs = [["hf", "HuggingFace"], ["civitai", "Civitai"], ["url", "Direct link"]].filter(([id]) => sources.includes(id) || (id === "hf" && sources.includes("hf_file")));
    const folder = k.layout === "folder";
    openModal(`<div class="add-model" data-tool="${s.tool}" data-kind="${k.id}">
      <div class="eyebrow">${esc(s.name)} · ${esc(k.label)}</div>
      <h3 class="serif" style="font-size:26px;margin:4px 0 14px">Add a model</h3>
      <div class="chips am-tabs">${tabs.map(([id, l], i) => `<button class="chip filter ${i ? "" : "active"}" data-tab="${id}">${l}</button>`).join("")}</div>
      <div class="am-pane" data-pane="hf" ${tabs[0][0] !== "hf" ? "hidden" : ""}>
        <p class="lead">${folder ? "A repository id such as <code>Qwen/Qwen-Image-2.1</code>. The whole repository is downloaded into a folder with the name you give it." : "A repository id and the file inside it; the single file lands in this folder."}</p>
        <div class="am-row"><input type="text" id="amRepo" placeholder="owner/repository" spellcheck="false"><button class="btn" id="amLookup">Look up</button></div>
        <div id="amHfInfo" class="am-info"></div>
        <div class="am-row"><label>Save as <input type="text" id="amName" placeholder="${folder ? "folder name" : "file name"}"></label><button class="btn primary" id="amHfGo" disabled>Download</button></div>
      </div>
      <div class="am-pane" data-pane="civitai" ${tabs[0][0] !== "civitai" ? "hidden" : ""}>
        <p class="lead">Search Civitai for ${esc((k.civitai && k.civitai.types) || "models")}${k.civitai && k.civitai.base ? ` for ${esc(k.civitai.base)}` : ""}. The latest version's primary file is downloaded and checked against its SHA-256.</p>
        <div class="am-row"><input type="search" id="amCiv" placeholder="search…" ${k.civitai && k.civitai.base ? `data-base="${esc(k.civitai.base)}"` : ""} data-types="${esc((k.civitai && k.civitai.types) || "")}"><button class="btn" id="amCivGo">Search</button></div>
        <div id="amCivList" class="am-list"></div>
      </div>
      <div class="am-pane" data-pane="url" ${tabs[0][0] !== "url" ? "hidden" : ""}>
        <p class="lead">Any direct download link (a .safetensors file, a Civitai or HuggingFace file URL…). Resumes if interrupted.</p>
        <div class="am-row"><input type="url" id="amUrl" placeholder="https://…" spellcheck="false"></div>
        <div class="am-row"><label>Save as <input type="text" id="amUrlName" placeholder="file name (from the link if empty)"></label><label>SHA-256 <input type="text" id="amSha" placeholder="optional" spellcheck="false"></label><button class="btn primary" id="amUrlGo">Download</button></div>
      </div>
    </div>`, true);
    const root = $("#modalBody .add-model");
    root.querySelectorAll(".am-tabs .chip").forEach((c) => c.onclick = () => { root.querySelectorAll(".am-tabs .chip").forEach((x) => x.classList.toggle("active", x === c)); root.querySelectorAll(".am-pane").forEach((p) => p.hidden = p.dataset.pane !== c.dataset.tab); });
    const start = async (name, source) => {
      const r = await api("/api/models/download", { method: "POST", body: { tool: s.tool, kind: k.id, name, source } });
      S.models.downloads[r.download.id] = r.download; renderDownloads(); renderModels(); closeModal();
      toast(`Downloading ${r.download.name}`, "ok", "See the Downloads panel on the Models page.");
    };
    // HuggingFace
    let hf = null;
    const lookup = async () => {
      const repo = $("#amRepo").value.trim(); if (!repo.includes("/")) { toast("Enter owner/repository", "warn"); return; }
      $("#amHfInfo").innerHTML = `<span class="spinner"></span> looking up…`;
      try {
        hf = await api(`/api/models/hf?repo=${encodeURIComponent(repo)}`);
        const files = folder ? "" : `<label>File <select id="amHfFile">${hf.single_files.map((f) => `<option value="${esc(f.path)}">${esc(f.path)} (${fmtBytes(f.size)})</option>`).join("")}</select></label>`;
        $("#amHfInfo").innerHTML = `<div class="am-found"><b>${esc(hf.repo)}</b> · ${hf.files} files · ${esc(hf.size_h)}${hf.gated ? ` · <span class="pill warn">gated - needs your HF token</span>` : ""}${hf.pipeline ? ` · ${esc(hf.pipeline)}` : ""}${files}${!folder && !hf.single_files.length ? `<div class="note warn">No single weight file in this repository.</div>` : ""}</div>`;
        if (!$("#amName").value) $("#amName").value = folder ? repo.split("/").pop() : (hf.single_files[0] ? hf.single_files[0].path.split("/").pop() : "");
        if (!folder) { const sel = $("#amHfFile"); if (sel) sel.onchange = () => { $("#amName").value = sel.value.split("/").pop(); }; }
        $("#amHfGo").disabled = !(folder || hf.single_files.length);
      } catch (e) { $("#amHfInfo").innerHTML = `<div class="note danger">${esc(e.message)}</div>`; }
    };
    $("#amLookup").onclick = lookup; $("#amRepo").onkeydown = (e) => { if (e.key === "Enter") lookup(); };
    $("#amHfGo").onclick = async () => {
      if (!hf) return;
      const name = $("#amName").value.trim() || hf.repo.split("/").pop();
      try {
        if (folder) await start(name, { type: "hf", repo: hf.repo });
        else await start(name, { type: "hf_file", repo: hf.repo, path: $("#amHfFile").value });
      } catch (e) { toast(e.message, "error"); }
    };
    // Civitai
    const civ = async () => {
      const inp = $("#amCiv"); const q = inp.value.trim(); if (!q) return;
      $("#amCivList").innerHTML = `<span class="spinner"></span> searching…`;
      try {
        const r = await api(`/api/models/civitai?q=${encodeURIComponent(q)}&types=${encodeURIComponent(inp.dataset.types || "")}&base=${encodeURIComponent(inp.dataset.base || "")}`);
        if (!r.items.length) { $("#amCivList").innerHTML = `<div class="empty small">Nothing found.</div>`; return; }
        $("#amCivList").innerHTML = r.items.map((m, i) => `<div class="mrow"><div class="mrow-main"><b>${esc(m.name)}</b><small>${esc([m.version, m.base_model, m.type, m.creator ? "by " + m.creator : "", m.downloads ? m.downloads.toLocaleString() + " downloads" : "", m.nsfw ? "NSFW" : ""].filter(Boolean).join(" · "))}</small><small class="mono">${esc(m.file || "")}</small></div><div class="mrow-meta">${esc(m.size_h)}</div><div class="mrow-actions"><a class="btn small ghost" href="${esc(m.page)}" target="_blank" rel="noopener">Page</a><button class="btn small primary" data-civ="${i}">Download</button></div></div>`).join("");
        $("#amCivList").querySelectorAll("[data-civ]").forEach((btn) => btn.onclick = async () => {
          const m = r.items[+btn.dataset.civ];
          try { btn.disabled = true; await start(m.file || m.name, { type: "civitai", url: m.url, sha256: m.sha256, size: m.size, page: m.page }); } catch (e) { toast(e.message, "error"); btn.disabled = false; }
        });
      } catch (e) { $("#amCivList").innerHTML = `<div class="note danger">${esc(e.message)}</div>`; }
    };
    $("#amCivGo").onclick = civ; $("#amCiv").onkeydown = (e) => { if (e.key === "Enter") civ(); };
    // URL
    $("#amUrlGo").onclick = async () => {
      const url = $("#amUrl").value.trim(); if (!/^https?:\/\//.test(url)) { toast("Enter a full http(s) link", "warn"); return; }
      let name = $("#amUrlName").value.trim();
      if (!name) { try { name = decodeURIComponent(new URL(url).pathname.split("/").pop() || ""); } catch (e) { name = ""; } }
      if (!name) { toast("Give the file a name", "warn"); return; }
      try { await start(name, { type: "url", url, sha256: $("#amSha").value.trim() }); } catch (e) { toast(e.message, "error"); }
    };
    setTimeout(() => { const first = root.querySelector(".am-pane:not([hidden]) input"); if (first) first.focus(); }, 50);
  }

  // ---- downloads panel (SSE "download" events)
  function onDownload(d) {
    const prev = S.models.downloads[d.id];
    S.models.downloads[d.id] = d;
    if (prev && isActiveDl(prev) && !isActiveDl(d)) {
      toast(d.status === "done" ? `${d.name} is ready` : d.status === "cancelled" ? `${d.name} cancelled` : `${d.name} failed`, d.status === "done" ? "ok" : d.status === "cancelled" ? "warn" : "error", d.message || "", d.status === "error" ? 12000 : 6000);
      if (S.view === "models") loadModels(true);
    } else if (!prev && S.view === "models") renderModels();
    if (S.view === "models") renderDownloads();
  }

  function renderDownloads() {
    const list = Object.values(S.models.downloads).sort((a, b) => (b.started || 0) - (a.started || 0));
    const panel = $("#dlPanel");
    panel.hidden = !list.length;
    if (!list.length) return;
    $("#dlList").innerHTML = list.map((d) => {
      const active = isActiveDl(d);
      const pct = d.percent != null ? d.percent : null;
      const meta = active ? [d.status === "verifying" ? "verifying checksum" : `${fmtBytes(d.bytes)}${d.total ? " / " + fmtBytes(d.total) : ""}`, d.speed ? fmtBytes(d.speed) + "/s" : "", d.eta != null ? fmtEta(d.eta) : ""].filter(Boolean).join(" · ") : d.message;
      return `<div class="job dl ${d.status}">
        <div class="dl-head"><span class="pill" style="--tool:${TOOL_COLORS[d.tool] || "var(--muted)"}">${esc(nameOf(d.tool))}</span><div class="job-title" title="${esc(d.dest)}">${esc(d.name)}</div>${active ? `<button class="btn small ghost" data-cancel="${d.id}">Cancel</button>` : `<span class="pill ${d.status === "done" ? "ready" : d.status === "error" ? "error" : "warn"}">${d.status}</span>`}</div>
        ${active ? `<div class="progress ${pct == null ? "indeterminate" : ""}"><i style="width:${pct == null ? 40 : pct}%"></i></div>` : ""}
        <div class="job-meta"><span>${esc(meta || "")}</span>${active && pct != null ? `<span>${pct}%</span>` : ""}</div>
      </div>`;
    }).join("");
  }

  // ------------------------------------------------------------------ storage
  // Space per studio and per category (hub/disk.py). Video Studio has a clean-up: tick categories (orphans and Trash
  // older than 7 days are ticked by default; anything that belongs to a job in History only when you tick it), review
  // the exact list in a dry run, then confirm. Nothing is deleted any other way.
  S.storage = { data: null, plan: null, ticked: null, loading: false };
  const CAT_TONES = ["var(--text-2)", "var(--muted)", "var(--faint)", "var(--line-2)"];

  async function loadStorage(force) {
    const st = S.storage;
    if (st.loading) return;
    st.loading = true;
    $("#storageMeta").innerHTML = `<span class="spinner"></span> measuring…`;
    try {
      const [data, plan] = await Promise.all([api(`/api/storage${force ? "?force=1" : ""}`), api("/api/storage/video/plan").catch(() => null)]);
      st.data = data; st.plan = plan;
      if (plan && !st.ticked) st.ticked = new Set(plan.categories.filter((c) => c.cleanable && c.default).map((c) => c.id));
      renderStorage();
    } catch (e) { $("#storageBody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
    st.loading = false;
  }

  function renderStorage() {
    const { data, plan } = S.storage; if (!data) return;
    const d = data.disk;
    $("#storageMeta").textContent = `measured ${fmtAgo(data.scanned_at)}` + (d ? ` · disk: ${fmtBytes(d.free)} free of ${fmtBytes(d.total)}` : "");
    const max = Math.max(1, ...data.studios.map((x) => x.total));
    const diskCard = d ? `<section class="card storage-disk"><div class="card-head"><div><div class="eyebrow">This disk</div><h3>${fmtBytes(d.used)} used · ${fmtBytes(d.free)} free</h3></div></div>
      <div class="stack storage-bar disk">${data.studios.map((x) => `<i class="seg" title="${esc(x.name)} · ${fmtBytes(x.total)}" style="width:${(x.total / d.total) * 100}%;background:${x.color}"></i>`).join("")}<i class="seg" title="Everything else on this disk" style="width:${Math.max(0, (d.used - data.studios.reduce((a, x) => a + x.total, 0)) / d.total) * 100}%;background:var(--line-2)"></i></div>
      <div class="stack-legend">${data.studios.map((x) => `<span><b class="sw" style="background:${x.color}"></b>${esc(x.name)} <em>${fmtBytes(x.total) || "0 B"}</em></span>`).join("")}<span><b class="sw" style="background:var(--line-2)"></b>Other files <em>${fmtBytes(Math.max(0, d.used - data.studios.reduce((a, x) => a + x.total, 0)))}</em></span></div></section>` : "";
    const cards = data.studios.map((x) => {
      const cats = x.categories.filter((c) => c.bytes > 0 || c.cleanable);
      const sum = Math.max(1, cats.filter((c) => !(c.id === "models" && /shared/.test(c.label))).reduce((a, c) => a + c.bytes, 0));
      const bar = cats.filter((c) => c.bytes > 0 && !(c.id === "models" && /shared/.test(c.label))).map((c, i) => `<i class="seg" title="${esc(c.label)} · ${fmtBytes(c.bytes)}" style="width:${(c.bytes / sum) * (x.total / max) * 100}%;background:${c.cleanable ? "var(--accent)" : CAT_TONES[i % CAT_TONES.length]}"></i>`).join("");
      return `<section class="card storage-studio" style="--tool:${x.color}">
        <div class="sm-head"><div class="tool-num">${studioIcon(x.tool)}</div><div><h3>${esc(x.name)}</h3><p class="lead mono" title="${esc(x.dir)}">${esc(x.dir)}</p></div><span class="storage-total">${fmtBytes(x.total) || "0 B"}</span></div>
        <div class="stack storage-bar">${bar}</div>
        <table class="storage-cats">${cats.map((c) => `<tr class="${c.cleanable ? "cleanable" : ""}"><td title="${esc(c.help || "")}">${esc(c.label)}${c.cleanable ? ` <span class="pill warn">can be cleaned</span>` : ""}</td><td>${c.files ? c.files.toLocaleString() + (c.files === 1 ? " file" : " files") : ""}</td><td>${fmtBytes(c.bytes) || "0 B"}</td></tr>`).join("")}</table>
        ${x.cleanup && plan ? cleanupPanel(plan) : ""}
      </section>`;
    }).join("");
    $("#storageBody").innerHTML = diskCard + cards;
  }

  function cleanupPanel(plan) {
    const ticked = S.storage.ticked || new Set();
    const cats = plan.categories.filter((c) => c.cleanable || c.id === "orphans");
    const selBytes = cats.filter((c) => ticked.has(c.id)).reduce((a, c) => a + c.bytes, 0);
    const selItems = cats.filter((c) => ticked.has(c.id)).reduce((a, c) => a + c.items.length, 0);
    const trashNeedsApi = !plan.api && cats.some((c) => c.id.startsWith("trash") && c.items.length);
    return `<div class="cleanup">
      <div class="cleanup-head"><b>Clean up</b><span class="as-dim">job list from Video Studio's ${plan.api ? "API and database" : "database (read-only - the studio is not running)"} · ${plan.jobs} jobs</span></div>
      ${cats.map((c) => { const history = c.id.startsWith("history"); const empty = !c.items.length;
        return `<label class="cleanup-cat ${history ? "history" : ""} ${empty ? "empty" : ""}" title="${esc(c.help)}">
          <input type="checkbox" data-cat="${c.id}" ${ticked.has(c.id) ? "checked" : ""} ${empty || !c.cleanable ? "disabled" : ""}>
          <span class="cc-main"><b>${esc(c.label)}</b><small>${esc(c.help)}</small>${history ? `<small class="cc-warn">Belongs to jobs that are still in History - only if you tick it.</small>` : ""}</span>
          <span class="cc-size">${c.items.length ? `${c.items.length} · ${fmtBytes(c.bytes)}` : "none"}</span></label>`; }).join("")}
      ${trashNeedsApi ? `<div class="note warn">Video Studio is not running: Trash items are deleted through its own API, so start it to empty the Trash. <button class="btn small" data-storage-act="start">Start Video Studio</button></div>` : ""}
      <div class="cleanup-actions"><button class="btn primary" data-storage-act="review" ${selItems ? "" : "disabled"}>Review ${selItems} item${selItems === 1 ? "" : "s"} · ${fmtBytes(selBytes) || "0 B"}…</button>
        <span class="as-dim">Nothing is deleted until you confirm the list.</span></div>
    </div>`;
  }

  function onStorageChange(e) {
    const cb = e.target.closest("[data-cat]"); if (!cb) return;
    const t = S.storage.ticked || (S.storage.ticked = new Set());
    if (cb.checked) t.add(cb.dataset.cat); else t.delete(cb.dataset.cat);
    renderStorage();
  }

  async function onStorageClick(e) {
    const b = e.target.closest("[data-storage-act]"); if (!b) return;
    if (b.dataset.storageAct === "start") { await api("/api/tools/video/start", { method: "POST" }).catch((err) => toast(err.message, "error")); toast("Video Studio is starting", "ok", "Rescan when it is ready."); return; }
    if (b.dataset.storageAct === "review") reviewCleanup();
  }

  async function reviewCleanup() {
    const { plan, ticked } = S.storage;
    const categories = [...ticked];
    const items = plan.categories.filter((c) => ticked.has(c.id)).flatMap((c) => c.items.map((it) => it.id));
    let dry;
    try { dry = await api("/api/storage/video/cleanup", { method: "POST", body: { categories, items, dry_run: true } }); }
    catch (err) { toast(err.message, "error"); return; }
    const byCat = {};
    for (const it of dry.deleted) (byCat[it.category] = byCat[it.category] || []).push(it);
    const label = (cid) => (plan.categories.find((c) => c.id === cid) || {}).label || cid;
    const nFiles = dry.deleted.reduce((a, it) => a + it.files.length, 0);
    openModal(`<div class="cleanup-review"><div class="eyebrow">Video Studio · confirm the clean-up</div>
      <h3 class="serif" style="font-size:26px;margin:4px 0 6px">Delete ${dry.deleted.length} item${dry.deleted.length === 1 ? "" : "s"} · ${fmtBytes(dry.bytes) || "0 B"}?</h3>
      <p class="lead">Exactly these ${nFiles} file${nFiles === 1 ? "" : "s"} go. Trash jobs are deleted through Video Studio (as "Delete for good" in its Trash); the other files are removed from disk. This cannot be undone.</p>
      ${dry.failed.length ? `<div class="note warn">${dry.failed.length} item(s) cannot be deleted now: ${esc(dry.failed[0].why)}</div>` : ""}
      <div class="cleanup-list">${Object.entries(byCat).map(([cid, its]) => `<details open><summary><b>${esc(label(cid))}</b> · ${its.length} · ${fmtBytes(its.reduce((a, it) => a + it.bytes, 0))}</summary>
        <ul>${its.map((it) => `<li><span class="mono">${it.files.map(esc).join("<br>")}</span><em>${fmtBytes(it.bytes) || "0 B"}</em>${it.label && !it.files.some((f) => f.endsWith(it.label)) ? `<small>${esc(it.label)}</small>` : ""}</li>`).join("")}</ul></details>`).join("")}</div>
      <div class="cleanup-actions"><button class="btn danger" id="cleanupGo" ${dry.deleted.length ? "" : "disabled"}>Delete ${dry.deleted.length} item${dry.deleted.length === 1 ? "" : "s"} (${fmtBytes(dry.bytes) || "0 B"})</button><button class="btn ghost" id="cleanupCancel">Cancel</button></div></div>`, true);
    $("#cleanupCancel").onclick = closeModal;
    $("#cleanupGo").onclick = async () => {
      const go = $("#cleanupGo"); go.disabled = true; go.innerHTML = `<span class="spinner"></span> Deleting…`;
      try {
        const r = await api("/api/storage/video/cleanup", { method: "POST", body: { categories, items: dry.deleted.map((it) => it.id), dry_run: false } });
        closeModal();
        toast(`Freed ${fmtBytes(r.bytes) || "0 B"}`, r.failed.length ? "warn" : "ok", `${r.deleted.length} deleted${r.failed.length ? ` · ${r.failed.length} failed: ${r.failed[0].why}` : ""}${r.skipped.length ? ` · ${r.skipped.length} skipped` : ""}`, 9000);
        S.storage.ticked = null; loadStorage(true);
      } catch (err) { toast(err.message, "error"); go.disabled = false; go.textContent = "Delete"; }
    };
  }

  // ------------------------------------------------------------------ modal / drawer
  function openModal(html, narrow = false) {
    $("#modalBody").innerHTML = html;
    $("#modalCard").classList.toggle("narrow", narrow);
    $("#modal").hidden = false;
  }
  function closeModal() { $("#modal").hidden = true; $("#modalBody").innerHTML = ""; }
  function toggleDrawer(force) {
    const d = $("#drawer");
    const show = force != null ? force : d.hidden;
    d.hidden = !show;
    if (show) { S.unread = 0; $("#activityBadge").hidden = true; renderActivity($("#drawerList"), 200); }
  }

  // ------------------------------------------------------------------ SSE
  function connect() {
    if (S.es) { try { S.es.close(); } catch (e) { /* ignore */ } }
    const es = new EventSource("/api/events");
    S.es = es;
    es.onopen = () => { S.focusSent = undefined; sendFocus(S.view === "tool" ? S.tool : null); };
    es.addEventListener("state", (e) => { S.connected = true; onState(JSON.parse(e.data)); });
    for (const type of ["log", "tool", "claim", "waiting", "queue", "summary", "settings", "download", "job", "watchdog"]) {
      es.addEventListener(type, (e) => { const ev = JSON.parse(e.data); if (type === "settings") { if (S.state) S.state.settings = ev.settings; return; } if (type === "download") { onDownload(ev); return; } onEvent({ type, ...ev }); });
    }
    es.onerror = () => { S.connected = false; $("#conn").classList.add("off"); es.close(); setTimeout(connect, 2500); };
  }

  // ------------------------------------------------------------------ boot
  function bind() {
    window.addEventListener("hashchange", route);
    $("#btnTheme").onclick = () => { S.themeTouched = true; applyTheme(S.theme === "dark" ? "light" : "dark"); };
    $("#btnLook").onclick = () => { S.lookTouched = true; applyLook(currentLook() === "cinema" ? "classic" : "cinema"); };
    applyLook(currentLook(), false);
    $("#btnCollapse").onclick = () => { document.body.classList.toggle("collapsed"); try { localStorage.setItem("pxx-rail", document.body.classList.contains("collapsed") ? "1" : "0"); } catch (e) { /* ignore */ } };
    $("#btnActivity").onclick = () => toggleDrawer();
    $("#chipQueue").onclick = openQueue;
    $("#queueCardOpen").onclick = openQueue;
    document.addEventListener("click", (e) => {
      const sk = e.target.closest("[data-skip]"); if (sk) { e.preventDefault(); skipQueued(sk.dataset.skip); return; }
      if (e.target.closest("[data-queue-open]")) openQueue();
    });
    $("#storageRefresh").onclick = () => loadStorage(true);
    $("#storageBody").addEventListener("click", onStorageClick);
    $("#storageBody").addEventListener("change", onStorageChange);
    $("#drawerClose").onclick = () => toggleDrawer(false);
    $("#modalClose").onclick = closeModal;
    $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
    $("#btnFreeGpu").onclick = async () => { try { const r = await api("/api/gpu/free", { method: "POST", body: {} }); if (!r.actions.length) toast("The GPU is already free", "ok"); } catch (e) { toast(e.message, "error"); } };
    $$("[data-global]").forEach((b) => b.onclick = async () => {
      const stop = b.dataset.global === "free-stop";
      if (stop && !confirm("Stop every studio? Running jobs would be interrupted.")) return;
      try { await api("/api/gpu/free", { method: "POST", body: { stop } }); } catch (e) { toast(e.message, "error"); }
    });
    $("#overlayStart").onclick = () => { if (S.tool) api(`/api/tools/${S.tool}/start`, { method: "POST" }).then(() => { const f = S.frames[S.tool]; if (f) f.src = proxyUrl(toolOf(S.tool)); }).catch((e) => toast(e.message, "error")); };
    $("#overlayLog").onclick = () => { if (S.tool) showLog(S.tool); };
    $("#libChips").addEventListener("click", (e) => { const c = e.target.closest(".chip"); if (!c) return; $$("#libChips .chip").forEach((x) => x.classList.toggle("active", x === c)); S.lib.tool = c.dataset.tool; loadLibrary(true); });
    $("#libSearch").addEventListener("input", (e) => { clearTimeout(S.libTimer); S.libTimer = setTimeout(() => { S.lib.q = e.target.value.trim(); loadLibrary(true); }, 300); });
    $("#libMore").onclick = () => loadLibrary(false);
    $("#modelChips").addEventListener("click", (e) => { const c = e.target.closest(".chip"); if (!c) return; $$("#modelChips .chip").forEach((x) => x.classList.toggle("active", x === c)); S.models.tool = c.dataset.tool; renderModels(); });
    $("#modelRefresh").onclick = () => loadModels(true);
    $("#dlClear").onclick = async () => { await api("/api/models/downloads/clear", { method: "POST", body: {} }).catch(() => {}); Object.keys(S.models.downloads).forEach((k) => { if (!isActiveDl(S.models.downloads[k])) delete S.models.downloads[k]; }); renderDownloads(); };
    $("#modelsGrid").addEventListener("click", onModelAction);
    $("#dlList").addEventListener("click", (e) => { const b = e.target.closest("[data-cancel]"); if (b) api(`/api/models/downloads/${b.dataset.cancel}/cancel`, { method: "POST", body: {} }).catch((err) => toast(err.message, "error")); });
    document.addEventListener("click", (e) => {
      const card = e.target.closest(".media-card"); if (!card) return;
      const it = [...S.lib.items, ...(S.recentItems || [])].find((x) => x.id === card.dataset.id);
      if (it) openViewer(it);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { closeModal(); toggleDrawer(false); closeMenu(); return; }
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      const map = { h: "#/home", l: "#/library", m: "#/models" };
      if (S.state) S.state.order.forEach((id, i) => { map[String(i + 1)] = `#/tool/${id}`; });
      const k = e.key.toLowerCase();
      if (map[k]) { location.hash = map[k]; e.preventDefault(); }
      else if (k === "a") { toggleDrawer(); e.preventDefault(); }
      else if (k === "t") { $("#btnTheme").click(); e.preventDefault(); }
      else if (k === "g") { $("#btnFreeGpu").click(); e.preventDefault(); }
    });
    window.addEventListener("message", (e) => {
      const d = e.data || {};
      if (d.type === "hub:ready" && d.tool) { S.frameReady[d.tool] = true; const f = S.frames[d.tool]; if (f) { try { f.contentWindow.postMessage({ type: "hub:theme", theme: S.theme, look: currentLook() }, "*"); } catch (err) { /* ignore */ } } }
      if (d.type === "hub:navigate" && d.hash) location.hash = d.hash;
      if (d.type === "hub:imported" && d.tool) {
        const t = toolOf(d.tool);
        if (d.ok) toast(`${t ? t.name : d.tool} received it`, "ok", d.message || "", 5000);
        else toast(`${t ? t.name : d.tool} could not take it`, "error", d.message || "", 9000);
      }
    });
    window.addEventListener("resize", () => { if (S.view === "home") drawSpark(); });
    setInterval(() => { if (S.view === "home") loadRecent(); }, 20000);
  }

  (function init() {
    try { if (localStorage.getItem("pxx-rail") === "1") document.body.classList.add("collapsed"); } catch (e) { /* ignore */ }
    bind();
    connect();
    api("/api/activity").then((d) => { S.events = d.events || []; renderActivity($("#activityList"), 8); }).catch(() => {});
    route();
  })();
})();
