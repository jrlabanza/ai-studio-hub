/* Assistant: describe an image, the local planner picks checkpoint + LoRAs + prompt (skills owned by the studio),
   you check or edit the plan, the studio renders. Phase 1: Forge. */
(function () {
  const $ = (q, el = document) => el.querySelector(q);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const store = { get: (k, d) => { try { return localStorage.getItem(k) || d; } catch { return d; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private window */ } } };
  const A = { session: null, studio: "forge", plan: null, msgs: [], busy: false, state: null, inv: null,
              engine: store.get("as.engine", "auto"), lastText: "" };
  const ENGINES = { auto: "Auto", h3: "MiniMax H3", ltx25: "LTX-2.5" };

  async function api(path, body, method) {
    const r = await fetch(path, body ? { method: method || "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) } : {});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `${r.status}`);
    return d;
  }

  function shell() {
    const root = $("#view-assistant");
    if (root.dataset.built) return root;
    root.dataset.built = "1";
    root.innerHTML = `
      <div class="as-wrap">
        <div class="as-chat card">
          <div class="as-head">
            <div class="chips" id="asStudios"></div>
            <span class="as-device" id="asDevice"></span>
          </div>
          <div class="as-log" id="asLog">
            <div class="as-empty">
              <div class="serif as-hello">What should we make?</div>
              <p>Say it plainly - <i>"a cheerful fairy serving matcha in a tea house, anime style"</i>, <i>"Anby Demara at the beach at sunset"</i>, <i>"a realistic photo of an old fisherman at dawn"</i>. The assistant picks the model, the LoRAs and the prompt from what you have installed, shows you the plan, and renders when you say so. Ask for changes in your own words.</p>
            </div>
          </div>
          <form class="as-input" id="asForm">
            <textarea id="asText" rows="2" placeholder="Describe the image… (Enter to plan, Shift+Enter for a new line)"></textarea>
            <button class="btn primary" id="asSend" type="submit">Plan</button>
          </form>
        </div>
        <div class="as-side">
          <div class="card as-plan" id="asPlan"><div class="as-plan-empty">The plan appears here.</div></div>
          <div class="card as-results" id="asResults" hidden></div>
        </div>
      </div>`;
    $("#asForm").addEventListener("submit", (e) => { e.preventDefault(); send(); });
    $("#asText").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
    return root;
  }

  function renderStudios() {
    const st = A.state; if (!st) return;
    $("#asStudios").innerHTML = Object.entries(st.studios).map(([id, s]) =>
      `<button class="chip filter ${id === A.studio ? "active" : ""}" data-id="${id}" ${s.ready ? "" : "disabled title='Coming in phase 2'"}>${esc(s.name)}${s.ready ? "" : " · soon"}</button>`).join("");
    $("#asStudios").querySelectorAll("button[data-id]").forEach((b) => b.onclick = () => { if (!b.disabled) { A.studio = b.dataset.id; renderStudios(); } });
    $("#asText").placeholder = {
      video: "Describe the clip… or \"animate it\" to bring the last image to life (Enter to plan)",
      image: "Describe the image, a poster or a logo… or \"edit it: …\" to change the last image (Enter to plan)",
      music: "Describe the song: what it is about, the genre, the language, the mood… (Enter to plan)",
      tts: "What should be said, and by whom? e.g. a calm narrator reading \"…\" (Enter to plan)",
    }[A.studio] || "Describe the image… (Enter to plan, Shift+Enter for a new line)";
    const dev = st.device === "auto" ? `Auto (now ${st.would_use === "gpu" ? "GPU" : "CPU"})` : st.device.toUpperCase();
    $("#asDevice").innerHTML = `<label title="Where the assistant's language model thinks. Auto: the GPU when it is free (an idle studio is unloaded first), the CPU while a studio is rendering. It is always unloaded before a render.">Planner: <select id="asDev">
      ${["auto", "gpu", "cpu"].map((d) => `<option value="${d}" ${d === st.device ? "selected" : ""}>${d === "auto" ? "Auto" : d.toUpperCase()}</option>`).join("")}</select></label>
      <span class="as-model">${esc(st.model)} · ${dev}</span>
      ${A.studio === "video" ? `<label title="Which video engine to plan for. Auto: MiniMax H3 for talking, singing, acting and anime; LTX-2.5 for cinematic footage and clips over 15 s.">Engine: <select id="asEng">
        ${Object.entries(ENGINES).map(([k, n]) => `<option value="${k}" ${k === A.engine ? "selected" : ""}>${n}</option>`).join("")}</select></label>` : ""}`;
    $("#asDev").onchange = async (e) => { A.state = await api("/api/assistant/settings", { device: e.target.value }, "PUT"); renderStudios(); };
    if ($("#asEng")) $("#asEng").onchange = (e) => { A.engine = e.target.value; store.set("as.engine", A.engine); };
  }

  function addMsg(role, html) {
    const log = $("#asLog");
    log.querySelector(".as-empty")?.remove();
    const el = document.createElement("div");
    el.className = `as-msg ${role}`;
    el.innerHTML = html;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  async function send() {
    const ta = $("#asText");
    const text = ta.value.trim();
    if (!text || A.busy) return;
    ta.value = "";
    addMsg("user", esc(text));
    A.lastText = text;
    await planFor(text, A.studio === "video" && A.engine !== "auto" ? A.engine : null);
  }

  async function planFor(text, engine) {
    if (A.busy) return;
    const wait = addMsg("assistant", `<span class="spinner"></span> Planning…`);
    A.busy = true; $("#asSend").disabled = true;
    try {
      const d = await api("/api/assistant/plan", { studio: A.studio, message: text, session: A.session, ...(engine ? { engine } : {}) });
      A.session = d.session; A.plan = d.plan;
      const t = d.timing;
      const P = d.plan;
      const what = P.studio === "video" ? `${esc(P.engine_name)} · ${P.mode === "i2v" ? "image to video" : "text to video"} · ${P.seconds} s · ${esc(P.aspect)}`
        : P.studio === "image" ? `Qwen-Image · ${{ t2i: "text to image", edit: "edit", rgba: "transparent" }[P.mode]} · ${esc(P.aspect)} · ${P.count} image${P.count > 1 ? "s" : ""}`
        : P.studio === "music" ? `YuE2 · "${esc(P.title)}"${P.instrumental ? " · instrumental" : ""}`
        : P.studio === "tts" ? `Qwen3-TTS · ${P.cast.length} voice${P.cast.length > 1 ? "s" : ""} · ${P.lines.length} line${P.lines.length > 1 ? "s" : ""}`
        : `${esc(P.checkpoint)} · ${P.loras.length} LoRA${P.loras.length === 1 ? "" : "s"}`;
      wait.innerHTML = `${esc(d.plan.summary || "Here is the plan.")}<div class="as-meta">${what} · planned in ${t.seconds}s on the ${t.device.toUpperCase()}${(t.freed || []).length ? ` (${esc(t.freed.join(", "))})` : ""}</div>` +
        (d.plan.notes || []).map((n) => `<div class="as-note">${esc(n)}</div>`).join("");
      renderPlan();
    } catch (e) {
      wait.innerHTML = `<span class="as-err">Could not plan: ${esc(e.message)}</span>`;
    } finally {
      A.busy = false; $("#asSend").disabled = false;
      A.state = await api("/api/assistant/state").catch(() => A.state); renderStudios();
    }
  }

  function renderPlan() {
    if (A.plan && A.plan.studio === "video") return renderVideoPlan();
    if (A.plan && A.plan.studio === "image") return renderImagePlan();
    if (A.plan && A.plan.studio === "music") return renderMusicPlan();
    if (A.plan && A.plan.studio === "tts") return renderVoicePlan();
    const p = A.plan; const box = $("#asPlan");
    if (!p) return;
    box.innerHTML = `
      <div class="as-plan-head"><span class="eyebrow">Plan · Forge</span><span class="badge">${esc(p.label.split(" — ")[0])}</span></div>
      <label class="as-f"><span>Checkpoint</span><input id="pCk" value="${esc(p.checkpoint)}" disabled></label>
      <label class="as-f"><span>Prompt</span><textarea id="pPrompt" rows="5">${esc(p.prompt_core)}</textarea></label>
      <div class="as-f"><span>LoRAs</span><div class="as-loras" id="pLoras">${p.loras.length ? p.loras.map((l, i) =>
        `<div class="as-lora" title="${esc(l.why)}"><b>${esc(l.title || l.name)}</b><input type="number" step="0.05" min="0" max="1.5" value="${l.weight}" data-i="${i}"><button class="btn ghost" data-x="${i}" title="Remove">✕</button></div>`).join("") : `<i class="as-dim">none</i>`}</div></div>
      <div class="as-grid">
        <label class="as-f"><span>Width</span><input id="pW" type="number" step="64" value="${p.width}"></label>
        <label class="as-f"><span>Height</span><input id="pH" type="number" step="64" value="${p.height}"></label>
        <label class="as-f"><span>Steps</span><input id="pSteps" type="number" value="${p.steps}"></label>
        <label class="as-f"><span>CFG</span><input id="pCfg" type="number" step="0.5" value="${p.cfg}"></label>
        <label class="as-f"><span>Images</span><input id="pN" type="number" min="1" max="4" value="${p.count}"></label>
      </div>
      <details class="as-more"><summary>Negative prompt · quality tags</summary>
        <label class="as-f"><span>Negative</span><textarea id="pNeg" rows="3">${esc(p.negative)}</textarea></label>
        <div class="as-dim">Added automatically: ${esc(p.quality || "-")} · sampler ${esc(p.sampler)}</div></details>
      <div class="as-actions"><button class="btn primary" id="pGo">Generate</button><span class="as-dim" id="pState"></span></div>`;
    box.querySelectorAll("[data-x]").forEach((b) => b.onclick = () => { collect(); A.plan.loras.splice(+b.dataset.x, 1); renderPlan(); });
    $("#pGo").onclick = render;
  }

  function renderVideoPlan() {
    const p = A.plan; const box = $("#asPlan");
    box.innerHTML = `
      <div class="as-plan-head"><span class="eyebrow">Plan · Video Studio</span>
        <label class="as-eng" title="Switching re-plans the clip for that engine - LTX-2.5 and MiniMax H3 need differently written prompts">Engine
          <select id="vEng">${Object.entries(p.engines || { [p.engine]: p.engine_name }).map(([k, n]) => `<option value="${k}" ${k === p.engine ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label></div>
      ${p.image ? `<div class="as-src"><img src="${esc(p.image.url)}" alt=""><div><b>Image to video</b><div class="as-dim">first frame: ${esc(p.image.label)}</div></div></div>` : ""}
      <label class="as-f"><span>Prompt (${p.engine === "h3" ? "MiniMax H3 format" : "LTX-2.5"})</span><textarea id="vPrompt" rows="${p.engine === "h3" ? 11 : 6}">${esc(p.prompt)}</textarea></label>
      <div class="as-grid">
        <label class="as-f"><span>Seconds</span><input id="vSec" type="number" step="0.5" value="${p.seconds}"></label>
        <label class="as-f"><span>Shape</span><select id="vAsp">${["16:9", "9:16", "1:1"].map((a) => `<option ${a === p.aspect ? "selected" : ""}>${a}</option>`).join("")}</select></label>
        <label class="as-f"><span>Size</span><select id="vSize">${(p.sizes || [p.size]).map((z) => `<option ${z === p.size ? "selected" : ""}>${z}</option>`).join("")}</select></label>
        ${p.engine === "h3" ? `<label class="as-f"><span>Speed</span><select id="vTurbo"><option value="1" ${p.turbo ? "selected" : ""}>Turbo</option><option value="0" ${p.turbo ? "" : "selected"}>Full</option></select></label>` : ""}
      </div>
      <div class="as-actions"><button class="btn primary" id="pGo">Generate video</button><span class="as-dim" id="pState"></span></div>`;
    $("#pGo").onclick = render;
    $("#vEng").onchange = async (e) => {
      const eng = e.target.value;
      addMsg("user", `Use ${esc(ENGINES[eng] || eng)} for this clip`);
      await planFor(A.lastText || "the same clip", eng);
    };
  }

  const opt = (list, cur) => list.map((x) => `<option ${x === cur ? "selected" : ""}>${esc(x)}</option>`).join("");
  const goBtn = (label) => `<div class="as-actions"><button class="btn primary" id="pGo">${label}</button><span class="as-dim" id="pState"></span></div>`;

  function renderImagePlan() {
    const p = A.plan; const box = $("#asPlan");
    const modeName = { t2i: "Text to image", edit: "Edit the image", rgba: "Transparent background" }[p.mode];
    box.innerHTML = `
      <div class="as-plan-head"><span class="eyebrow">Plan · Image Studio</span><span class="badge">${modeName}</span></div>
      ${p.image ? `<div class="as-src"><img src="${esc(p.image.url)}" alt=""><div><b>Editing</b><div class="as-dim">${esc(p.image.label)}</div></div></div>` : ""}
      <label class="as-f"><span>${p.mode === "edit" ? "Edit instruction" : "Prompt (Qwen-Image)"}</span><textarea id="iPrompt" rows="6">${esc(p.prompt)}</textarea></label>
      <div class="as-grid">
        ${p.mode === "edit" ? "" : `<label class="as-f"><span>Shape</span><select id="iAsp">${opt(p.aspects, p.aspect)}</select></label>
        <label class="as-f"><span>Size</span><select id="iTier">${opt(p.tiers, p.tier)}</select></label>`}
        <label class="as-f"><span>Images</span><input id="iN" type="number" min="1" max="4" value="${p.count}"></label>
        <label class="as-f"><span>Steps</span><input id="iSteps" type="number" min="8" max="80" value="${p.steps}"></label>
      </div>${goBtn("Generate image")}`;
    $("#pGo").onclick = render;
  }

  function renderMusicPlan() {
    const p = A.plan; const box = $("#asPlan");
    box.innerHTML = `
      <div class="as-plan-head"><span class="eyebrow">Plan · Music Studio</span><span class="badge">${p.instrumental ? "Instrumental" : "Song"}</span></div>
      <label class="as-f"><span>Title</span><input id="mTitle" value="${esc(p.title)}"></label>
      <label class="as-f"><span>Style</span><textarea id="mStyle" rows="2">${esc(p.style)}</textarea></label>
      <label class="as-f"><span>Lyrics</span><textarea id="mLyrics" rows="14">${esc(p.lyrics)}</textarea></label>
      <div class="as-grid"><label class="as-f"><span>Takes</span><input id="mTakes" type="number" min="1" max="4" value="${p.takes}"></label></div>
      ${goBtn("Make the song")}<div class="as-dim">A song takes about 3 minutes per take.</div>`;
    $("#pGo").onclick = render;
  }

  function renderVoicePlan() {
    const p = A.plan; const box = $("#asPlan");
    const voiceOpts = (c) => c.kind === "preset" ? p.speakers.map((sp) => `<option value="${sp.id}" ${sp.id === c.voice ? "selected" : ""}>${esc(sp.name)} - ${esc(sp.desc)}</option>`).join("")
      : c.kind === "library" ? p.voices.map((v) => `<option value="${esc(v.id)}" ${v.id === c.voice ? "selected" : ""}>${esc(v.name)}</option>`).join("") : "";
    box.innerHTML = `
      <div class="as-plan-head"><span class="eyebrow">Plan · Voice Studio</span><span class="badge">${p.cast.length} voice${p.cast.length > 1 ? "s" : ""}</span></div>
      <div class="as-f"><span>Cast</span>${p.cast.map((c, i) => `<div class="as-cast" data-i="${i}">
        <b>${esc(c.name)}</b>
        <select class="cKind">${["design", "preset", "library"].map((k) => `<option ${k === c.kind ? "selected" : ""} ${k === "library" && !p.voices.length ? "disabled" : ""}>${k}</option>`).join("")}</select>
        ${c.kind === "design" ? `<input class="cDesc" value="${esc(c.description)}" placeholder="describe the voice">` : `<select class="cVoice">${voiceOpts(c)}</select>`}
      </div>`).join("")}</div>
      <label class="as-f"><span>Script (Name: line)</span><textarea id="tScript" rows="8">${esc(p.lines.map((l) => `${l.speaker}: ${l.text}`).join("\n"))}</textarea></label>
      <div class="as-grid"><label class="as-f"><span>Language</span><select id="tLang">${opt(p.languages, p.language)}</select></label></div>
      ${goBtn("Speak it")}`;
    box.querySelectorAll(".cKind").forEach((sel) => sel.onchange = () => {
      collectVoice(); const c = A.plan.cast[+sel.closest(".as-cast").dataset.i]; c.kind = sel.value;
      if (c.kind === "preset") c.voice = c.voice && A.plan.speakers.some((x) => x.id === c.voice) ? c.voice : A.plan.speakers[0].id;
      if (c.kind === "library") c.voice = (A.plan.voices[0] || {}).id || "";
      if (c.kind === "design" && !c.description) c.description = "a clear, natural adult voice";
      renderVoicePlan();
    });
    $("#pGo").onclick = render;
  }

  function collectImage() {
    const p = A.plan;
    p.prompt = $("#iPrompt").value.trim();
    if ($("#iAsp")) { p.aspect = $("#iAsp").value; p.tier = $("#iTier").value; }
    p.count = Math.max(1, Math.min(4, parseInt($("#iN").value, 10) || 1));
    p.steps = Math.max(8, Math.min(80, parseInt($("#iSteps").value, 10) || p.steps));
  }
  function collectMusic() {
    const p = A.plan;
    p.title = $("#mTitle").value.trim() || p.title; p.style = $("#mStyle").value.trim(); p.lyrics = $("#mLyrics").value.trim();
    p.takes = Math.max(1, Math.min(4, parseInt($("#mTakes").value, 10) || 1));
  }
  function collectVoice() {
    const p = A.plan;
    $("#asPlan").querySelectorAll(".as-cast").forEach((row) => {
      const c = p.cast[+row.dataset.i]; c.kind = row.querySelector(".cKind").value;
      if (row.querySelector(".cDesc")) c.description = row.querySelector(".cDesc").value.trim();
      if (row.querySelector(".cVoice")) c.voice = row.querySelector(".cVoice").value;
    });
    const names = Object.fromEntries(p.cast.map((c) => [c.name.toLowerCase(), c.name]));
    if ($("#tScript")) p.lines = $("#tScript").value.split("\n").map((ln) => {
      const m = ln.match(/^\s*([^:]{1,40}):\s*(.+)$/);
      return m ? { speaker: names[m[1].trim().toLowerCase()] || p.cast[0].name, text: m[2].trim() } : (ln.trim() ? { speaker: p.cast[0].name, text: ln.trim() } : null);
    }).filter(Boolean);
    if ($("#tLang")) p.language = $("#tLang").value;
  }

  function collectVideo() {
    const p = A.plan;
    p.prompt = $("#vPrompt").value.trim();
    p.seconds = Math.max(1, Math.min(20, parseFloat($("#vSec").value) || p.seconds));
    p.aspect = $("#vAsp").value; p.size = $("#vSize").value; if ($("#vTurbo")) p.turbo = $("#vTurbo").value === "1";
  }

  function collect() {
    if (A.plan && A.plan.studio === "video") return collectVideo();
    if (A.plan && A.plan.studio === "image") return collectImage();
    if (A.plan && A.plan.studio === "music") return collectMusic();
    if (A.plan && A.plan.studio === "tts") return collectVoice();
    const p = A.plan; const num = (id, d) => { const v = parseFloat($(id).value); return isFinite(v) ? v : d; };
    p.prompt_core = $("#pPrompt").value.trim();
    p.negative = $("#pNeg").value.trim();
    p.width = Math.round(num("#pW", p.width) / 64) * 64; p.height = Math.round(num("#pH", p.height) / 64) * 64;
    p.steps = Math.max(1, Math.min(80, Math.round(num("#pSteps", p.steps))));
    p.cfg = Math.max(1, Math.min(20, num("#pCfg", p.cfg)));
    p.count = Math.max(1, Math.min(4, Math.round(num("#pN", p.count))));
    $("#pLoras").querySelectorAll("input[data-i]").forEach((i) => { p.loras[+i.dataset.i].weight = Math.max(0, Math.min(1.5, parseFloat(i.value) || 0)); });
  }

  async function render() {
    if (A.busy) return;
    collect();
    const studioName = { video: "Video Studio", image: "Image Studio", music: "Music Studio", tts: "Voice Studio" }[A.plan.studio] || "Forge";
    A.busy = true; $("#pGo").disabled = true; $("#pState").innerHTML = `<span class="spinner"></span> Rendering in ${studioName}…`;
    const t0 = Date.now();
    let stage = "";
    const tick = setInterval(async () => {
      if (A.plan.studio !== "forge") {
        const pr = await api(`/api/assistant/progress/${A.session}`).catch(() => ({}));
        if (pr.stage) stage = ` · ${pr.stage}${pr.progress ? ` ${Math.round(pr.progress * 100)}%` : ""}`;
      }
      $("#pState").innerHTML = `<span class="spinner"></span> Rendering in ${studioName}… ${Math.round((Date.now() - t0) / 1000)}s${esc(stage)}`;
    }, 2000);
    try {
      const d = await api("/api/assistant/render", { session: A.session, plan: A.plan });
      const res = $("#asResults"); res.hidden = false;
      if (d.audios) {
        res.insertAdjacentHTML("afterbegin", d.audios.map((u) => `<div class="as-vid"><audio src="${u}" controls preload="metadata"></audio><div class="as-dim">${esc(d.info || "")}</div></div>`).join(""));
        $("#pState").textContent = `Done in ${d.seconds}s - it is in ${studioName} too.`;
        addMsg("assistant", `Done in ${d.seconds}s - press play on the right.`);
      } else if (d.videos) {
        res.insertAdjacentHTML("afterbegin", d.videos.map((u) => `<div class="as-vid"><video src="${u}" controls loop playsinline></video><div class="as-dim">${esc(d.info || "")}</div></div>`).join(""));
        $("#pState").textContent = `Done in ${d.seconds}s - it is in Video Studio's History too.`;
        addMsg("assistant", `Rendered the clip in ${d.seconds}s.`);
      } else {
        res.insertAdjacentHTML("afterbegin", d.images.map((u) => `<a href="${u}" target="_blank" class="as-img"><img src="${u}" alt=""></a>`).join(""));
        $("#pState").textContent = `Done in ${d.seconds}s - saved in Forge's outputs too. Ask for changes on the left.`;
        addMsg("assistant", `Rendered ${d.images.length} image${d.images.length > 1 ? "s" : ""} in ${d.seconds}s. To animate it: pick Video Studio above and say "animate it".`);
      }
    } catch (e) {
      $("#pState").innerHTML = `<span class="as-err">${esc(e.message)}</span>`;
    } finally {
      clearInterval(tick); A.busy = false; $("#pGo").disabled = false;
    }
  }

  window.Assistant = {
    async show() {
      shell();
      try { A.state = await api("/api/assistant/state"); renderStudios(); } catch (e) { /* hub restarting */ }
      $("#asText").focus();
    },
  };
  if (location.hash.startsWith("#/assistant")) setTimeout(() => window.Assistant.show(), 0);
})();
