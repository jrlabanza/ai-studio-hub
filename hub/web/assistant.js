/* Assistant: describe an image, the local planner picks checkpoint + LoRAs + prompt (skills owned by the studio),
   you check or edit the plan, the studio renders. Phase 1: Forge. */
(function () {
  const $ = (q, el = document) => el.querySelector(q);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const A = { session: null, studio: "forge", plan: null, msgs: [], busy: false, state: null, inv: null };

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
    $("#asText").placeholder = A.studio === "video"
      ? "Describe the clip… or \"animate it\" to bring the last image to life (Enter to plan)"
      : "Describe the image… (Enter to plan, Shift+Enter for a new line)";
    const dev = st.device === "auto" ? `Auto (now ${st.would_use === "gpu" ? "GPU" : "CPU"})` : st.device.toUpperCase();
    $("#asDevice").innerHTML = `<label title="Where the assistant's language model thinks. Auto: the GPU when it is free (an idle studio is unloaded first), the CPU while a studio is rendering. It is always unloaded before a render.">Planner: <select id="asDev">
      ${["auto", "gpu", "cpu"].map((d) => `<option value="${d}" ${d === st.device ? "selected" : ""}>${d === "auto" ? "Auto" : d.toUpperCase()}</option>`).join("")}</select></label>
      <span class="as-model">${esc(st.model)} · ${dev}</span>`;
    $("#asDev").onchange = async (e) => { A.state = await api("/api/assistant/settings", { device: e.target.value }, "PUT"); renderStudios(); };
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
    const wait = addMsg("assistant", `<span class="spinner"></span> Planning…`);
    A.busy = true; $("#asSend").disabled = true;
    try {
      const d = await api("/api/assistant/plan", { studio: A.studio, message: text, session: A.session });
      A.session = d.session; A.plan = d.plan;
      const t = d.timing;
      const what = d.plan.studio === "video"
        ? `${esc(d.plan.engine_name)} · ${d.plan.mode === "i2v" ? "image to video" : "text to video"} · ${d.plan.seconds} s · ${esc(d.plan.aspect)}`
        : `${esc(d.plan.checkpoint)} · ${d.plan.loras.length} LoRA${d.plan.loras.length === 1 ? "" : "s"}`;
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
      <div class="as-plan-head"><span class="eyebrow">Plan · Video Studio</span><span class="badge">${esc(p.engine_name)}</span></div>
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
  }

  function collectVideo() {
    const p = A.plan;
    p.prompt = $("#vPrompt").value.trim();
    p.seconds = Math.max(1, Math.min(20, parseFloat($("#vSec").value) || p.seconds));
    p.aspect = $("#vAsp").value; p.size = $("#vSize").value; if ($("#vTurbo")) p.turbo = $("#vTurbo").value === "1";
  }

  function collect() {
    if (A.plan && A.plan.studio === "video") return collectVideo();
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
    const studioName = A.plan.studio === "video" ? "Video Studio" : "Forge";
    A.busy = true; $("#pGo").disabled = true; $("#pState").innerHTML = `<span class="spinner"></span> Rendering in ${studioName}…`;
    const t0 = Date.now();
    let stage = "";
    const tick = setInterval(async () => {
      if (A.plan.studio === "video") {
        const pr = await api(`/api/assistant/progress/${A.session}`).catch(() => ({}));
        if (pr.stage) stage = ` · ${pr.stage}${pr.progress ? ` ${Math.round(pr.progress * 100)}%` : ""}`;
      }
      $("#pState").innerHTML = `<span class="spinner"></span> Rendering in ${studioName}… ${Math.round((Date.now() - t0) / 1000)}s${esc(stage)}`;
    }, 2000);
    try {
      const d = await api("/api/assistant/render", { session: A.session, plan: A.plan });
      const res = $("#asResults"); res.hidden = false;
      if (d.videos) {
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
