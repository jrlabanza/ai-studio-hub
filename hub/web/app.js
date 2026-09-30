/* AI Studio Hub - shell logic (no build step). */
(() => {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const S = {
    state: null, view: "home", tool: null, theme: document.documentElement.dataset.theme || "light",
    frames: {}, frameGen: {}, frameWasRunning: {}, events: [], unread: 0, es: null, connected: false,
    lib: { tool: "", q: "", items: [], offset: 0, total: 0, loading: false, counts: {} }, libTimer: null,
    settingsDirty: false, recentLoadedAt: 0, lastToast: { text: "", at: 0 }, focusSent: null,
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
        <span class="nav-num">${esc(t.number)}</span><span class="nav-label">${esc(t.name)}</span><span class="nav-dot" id="navdot-${id}"></span></a>`; }).join("");
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

  // ------------------------------------------------------------------ theme
  function applyTheme(theme, save = true) {
    S.theme = theme;
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("pxx-theme", theme); } catch (e) { /* ignore */ }
    Object.values(S.frames).forEach((f) => { try { f.contentWindow.postMessage({ type: "hub:theme", theme }, "*"); } catch (e) { /* ignore */ } });
    if (save) api("/api/settings", { method: "PUT", body: { theme } }).catch(() => {});
  }

  // ------------------------------------------------------------------ router
  function route() {
    const hash = location.hash || "#/home";
    const m = hash.match(/^#\/(home|tool|library|settings)(?:\/(\w+))?/);
    const view = m ? m[1] : "home";
    const tool = m && m[2];
    showView(view, tool);
  }

  function showView(view, tool) {
    S.view = view;
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
      num.hidden = false; num.textContent = t ? t.number : ""; num.style.setProperty("--tool", t ? t.color : "var(--muted)");
      $("#topSub").textContent = t ? t.tagline : "";
      mountTool(tool);
      sendFocus(tool);
    } else {
      S.tool = null;
      num.hidden = true;
      $("#topTitle").textContent = { home: "Home", library: "Library", settings: "Settings" }[view] || "Home";
      $("#topSub").textContent = { home: "Orchestrating your GPU", library: "Everything your studios have made", settings: "How the hub shares the GPU" }[view] || "";
      sendFocus(null);
      if (view === "library") loadLibrary(true);
      if (view === "settings") renderSettings(true);
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
    $("#overlayNum").textContent = t.number;
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
    if (first && S.view === "tool" && S.tool && !S.frames[S.tool]) mountTool(S.tool);
    if (S.view === "home") renderHome();
    if (S.view === "tool") { const t = toolOf(S.tool); if (t) { $("#topTitle").textContent = t.name; $("#topSub").textContent = t.tagline; const num = $("#topNum"); num.textContent = t.number; num.style.setProperty("--tool", t.color); } }
    syncFrames();
    if (S.view === "settings" && !S.settingsDirty) renderSettings(false);
    if (st.theme && st.theme !== S.theme && !S.themeTouched) applyTheme(st.theme, false);
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
      $("#railGpuText").textContent = "No NVIDIA GPU";
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
    $("#gpuName").textContent = g.available ? g.name : "No NVIDIA GPU detected";
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
    const x0 = w - step * (hist.length - 1);
    ctx.beginPath();
    hist.forEach(([, used], i) => { const x = x0 + i * step, y = h - 4 - (used / total) * (h - 10); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.strokeStyle = "#34C6A3"; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.stroke();
    ctx.lineTo(w, h); ctx.lineTo(x0, h); ctx.closePath();
    ctx.fillStyle = "rgba(52,198,163,.15)"; ctx.fill();
    ctx.beginPath();
    hist.forEach(([, , util], i) => { const x = x0 + i * step, y = h - 4 - (util / 100) * (h - 10); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.strokeStyle = "#FFC23B"; ctx.lineWidth = 1.5; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]);
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
      $(".tool-num", card).textContent = t.number;
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
      const noteText = !t.installed ? t.install_note : (!t.model_present && t.model_note ? t.model_note : "");
      note.hidden = !noteText; note.textContent = noteText;
      const running = t.state === "running";
      const unloadable = t.supports_unload !== false;
      $('[data-act="unload"]', card).hidden = !(running && s.loaded && !s.busy && unloadable);
      $('[data-act="warm"]', card).hidden = !(t.installed && t.enabled && !s.busy && !(running && s.loaded && t.is_owner) && unloadable);
      $('[data-act="start"]', card).hidden = !(t.installed && t.enabled && (t.state === "stopped" || t.state === "error"));
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
    } else if (ev.type === "waiting") {
      toast(`${nameOf(ev.tool)} is waiting for ${(ev.for || []).map(nameOf).join(", ")} to finish`, "warn", "Your request is queued and starts automatically.", 8000);
    } else if (ev.type === "tool") {
      const name = nameOf(ev.tool);
      if (ev.state === "error" && ev.error) toast(`${name}: ${ev.error.split("\n")[0]}`, "error", "", 9000);
      else if (ev.state === "running") toast(`${name} is ready`, "ok");
    } else if (ev.type === "log" && (ev.level === "error" || ev.level === "warn") && ev.source) {
      toast(ev.message, ev.level, "", 8000);
    }
    renderActivity($("#activityList"), 8);
    if (!$("#drawer").hidden) renderActivity($("#drawerList"), 200);
  }

  function renderActivity(ul, limit) {
    if (!ul) return;
    const evs = S.events.filter((e) => e.type === "log" || e.type === "claim" || e.type === "waiting").slice(-limit).reverse();
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
      <div class="viewer-actions"><a class="btn primary" href="${esc(it.download)}">Download</a>
        <button class="btn" id="viewerFolder">Open folder</button>
        <a class="btn ghost" href="#/tool/${esc(it.tool)}" id="viewerOpenTool">Open ${esc(nameOf(it.tool))}</a></div>
    </div></div>`);
    $("#viewerFolder").onclick = () => api("/api/library/open", { method: "POST", body: { folder: it.folder } }).catch((e) => toast(e.message, "error"));
    $("#viewerOpenTool").onclick = closeModal;
    $$("#modalBody .versions button").forEach((b) => { b.onclick = () => { const a = $("#viewerAudio"); if (a) { a.src = b.dataset.src; a.play(); } }; });
  }

  // ------------------------------------------------------------------ settings
  const SETTINGS_SCHEMA = [
    { title: "Graphics card", lead: "How the hub shares one GPU between your studios.", fields: [
      { key: "gpu_policy", label: "Sharing policy", type: "select", options: [["auto", "Automatic (recommended)"], ["exclusive", "One model at a time"], ["budget", "Share when it fits"]], help: "Automatic keeps one model at a time on cards under 20 GB and lets small models share on bigger cards." },
      { key: "prepare_on_switch", label: "Load ahead when I switch studios", type: "bool", help: "A few seconds after you open a studio its model is loaded (and the others unloaded) so Generate is instant." },
      { key: "prepare_delay_s", label: "Delay before loading ahead", type: "number", step: 0.5, min: 0.5, max: 60, unit: "s" },
      { key: "idle_unload_min", label: "Unload an idle model after", type: "number", step: 1, min: -1, max: 1440, unit: "min", help: "0 = automatic (8 min on 8 GB cards, longer on bigger ones), -1 = never." },
      { key: "idle_stop_min", label: "Stop an idle studio after", type: "number", step: 1, min: -1, max: 1440, unit: "min", help: "Frees the RAM and the CUDA context too. 0 = automatic, -1 = never. Pinned studios are never stopped." },
      { key: "claim_wait_max_min", label: "Wait for a busy studio up to", type: "number", step: 1, min: 1, max: 600, unit: "min", help: "A request that needs the GPU waits this long for another studio's job before giving up." },
      { key: "vram_headroom_gb", label: "VRAM headroom", type: "number", step: 0.1, min: 0, max: 8, unit: "GB", help: "Extra free memory the hub tries to keep for the desktop and the browser." },
      { key: "release_ollama", label: "Release Ollama models when VRAM is short", type: "bool", help: "Music Studio's lyric writer uses a local Ollama model, which stays resident for minutes." },
    ] },
    { title: "Appearance", lead: "The theme of the shell and, if you like, of the studios inside it.", fields: [
      { key: "theme", label: "Theme", type: "select", options: [["light", "Light"], ["dark", "Dark"]] },
      { key: "restyle_tools", label: "Apply the hub theme inside the studios", type: "bool", help: "Turn off to see each tool's original look. Takes effect when a studio is reloaded." },
      { key: "brand_fonts_in_tools", label: "Hub font inside the studios", type: "bool", help: "Use the hub's font (Inter) for headings and buttons inside the tools." },
    ] },
    { title: "Network", lead: "Changes here need a restart of the hub.", fields: [
      { key: "bind_host", label: "Listen on", type: "select", options: [["127.0.0.1", "This PC only"], ["0.0.0.0", "Everyone on my network"]], help: "Sharing on the network also needs a firewall rule for ports 7900-7906 (see README)." },
      { key: "hub_port", label: "Hub port", type: "number", step: 1, min: 1024, max: 65535 },
      { key: "open_browser", label: "Open the browser on start", type: "bool" },
      { key: "stop_tools_on_exit", label: "Stop all studios when the hub closes", type: "bool" },
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
        + `<section class="card"><h3>Keyboard</h3><p class="lead">Shortcuts work anywhere in the shell.</p><div class="shortcuts"><kbd>Alt</kbd>+<kbd>1</kbd>…<kbd>${S.state.order.length}</kbd><span>Open a studio</span><kbd>Alt</kbd>+<kbd>H</kbd><span>Home</span><kbd>Alt</kbd>+<kbd>L</kbd><span>Library</span><kbd>Alt</kbd>+<kbd>A</kbd><span>Activity</span><kbd>Alt</kbd>+<kbd>T</kbd><span>Theme</span><kbd>Alt</kbd>+<kbd>G</kbd><span>Free the GPU</span></div>
          <p class="lead" style="margin-top:14px">${esc(st.app.name)} ${esc(st.app.version)} · ${esc(st.app.platform || "")} · hub on port ${st.app.hub_port} · <a href="/api/docs" target="_blank">API</a></p></section>`;
      grid.querySelectorAll(".field").forEach((f) => f.addEventListener("change", onSettingChange));
      grid.querySelectorAll(".switch").forEach((b) => b.addEventListener("click", () => { b.classList.toggle("on"); onSettingChange({ currentTarget: b.closest(".field, .tool-row"), target: b }); }));
      loadDoctor();
    }
    renderToolSettings(s);
  }

  function control(f, val) {
    if (f.type === "bool") return `<button type="button" class="switch ${val ? "on" : ""}" data-key="${f.key}" aria-label="${esc(f.label)}"></button>`;
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
    else val = parseFloat(ctl.value);
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
      <div class="tool-num">${t.number}</div>
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
    for (const type of ["log", "tool", "claim", "waiting", "summary", "settings"]) {
      es.addEventListener(type, (e) => { const ev = JSON.parse(e.data); if (type === "settings") { if (S.state) S.state.settings = ev.settings; return; } onEvent({ type, ...ev }); });
    }
    es.onerror = () => { S.connected = false; $("#conn").classList.add("off"); es.close(); setTimeout(connect, 2500); };
  }

  // ------------------------------------------------------------------ boot
  function bind() {
    window.addEventListener("hashchange", route);
    $("#btnTheme").onclick = () => { S.themeTouched = true; applyTheme(S.theme === "dark" ? "light" : "dark"); };
    $("#btnCollapse").onclick = () => { document.body.classList.toggle("collapsed"); try { localStorage.setItem("pxx-rail", document.body.classList.contains("collapsed") ? "1" : "0"); } catch (e) { /* ignore */ } };
    $("#btnActivity").onclick = () => toggleDrawer();
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
    document.addEventListener("click", (e) => {
      const card = e.target.closest(".media-card"); if (!card) return;
      const it = [...S.lib.items, ...(S.recentItems || [])].find((x) => x.id === card.dataset.id);
      if (it) openViewer(it);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { closeModal(); toggleDrawer(false); closeMenu(); return; }
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      const map = { h: "#/home", l: "#/library" };
      if (S.state) S.state.order.forEach((id, i) => { map[String(i + 1)] = `#/tool/${id}`; });
      const k = e.key.toLowerCase();
      if (map[k]) { location.hash = map[k]; e.preventDefault(); }
      else if (k === "a") { toggleDrawer(); e.preventDefault(); }
      else if (k === "t") { $("#btnTheme").click(); e.preventDefault(); }
      else if (k === "g") { $("#btnFreeGpu").click(); e.preventDefault(); }
    });
    window.addEventListener("message", (e) => {
      const d = e.data || {};
      if (d.type === "hub:ready" && d.tool) { const f = S.frames[d.tool]; if (f) { try { f.contentWindow.postMessage({ type: "hub:theme", theme: S.theme }, "*"); } catch (err) { /* ignore */ } } }
      if (d.type === "hub:navigate" && d.hash) location.hash = d.hash;
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
