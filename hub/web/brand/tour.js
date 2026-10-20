/* Hub tour engine - a small, dependency-free guided-tour runner shared by the hub shell (/static/brand/tour.js)
   and the studio bridge (/__hub/brand/tour.js). Works offline; injects its own styles.

   HubTour.start(tour, opts)   tour = { id, title, steps: [step, ...] }
     step = { sel, title, text, list, before, when, skipIfMissing, wait, actions, pad }
       sel      where to point: a CSS selector, "css::text(Label)" (exact or leading visible text, aria-label or
                title), "css::contains(Label)", an array of those (first visible wins) or a function returning an
                element. No sel, or nothing found: the card is shown centred (or the step is skipped when
                skipIfMissing is set).
       text     1-3 short sentences (trusted HTML: the tours are written in this repo); list = short bullet lines.
       before   optional async (ctx) => {} run before the step, e.g. open a page or a tab. It is given 4 s and its
                errors are swallowed: a tour never gets stuck or throws into the page.
       when     optional () => bool; false skips the step.
       actions  optional extra buttons on the card [{ label, run(ctx), primary }] (the last step's "pick a studio").
     opts = { onEnd({ completed, index }), onStep(info) }
   HubTour.offer({ title, text, startLabel, laterLabel, onStart, onLater }) - the small "New here?" card.
   HubTour.next() / back() / stop() / active() / info() - for keyboard, tests and the walker script.
   Tours only point and explain: they never click Generate, upload, delete or change a setting. */
(function () {
  "use strict";
  if (window.HubTour) return;

  var Z = 2147483000;
  var CSS = [
    ".ht-root{--ht-bg:#FFFFFF;--ht-text:#1F1A17;--ht-muted:#7A6E68;--ht-line:#E6DFDA;--ht-accent:#E05A4E;--ht-ink:#FFFFFF;--ht-soft:rgba(224,90,78,.12);--ht-dim:rgba(20,14,12,.55);--ht-shadow:0 18px 50px rgba(0,0,0,.22);",
    "font:400 14px/1.45 Inter,'Segoe UI Variable','Segoe UI',system-ui,-apple-system,sans-serif;color:var(--ht-text);-webkit-font-smoothing:antialiased;letter-spacing:normal;text-transform:none}",
    ".ht-root[data-ht-theme=dark]{--ht-bg:#1F1B19;--ht-text:#F2ECE8;--ht-muted:#B3A79F;--ht-line:rgba(255,255,255,.14);--ht-accent:#FF7B6C;--ht-ink:#1A0E0C;--ht-soft:rgba(255,123,108,.16);--ht-dim:rgba(0,0,0,.66);--ht-shadow:0 18px 50px rgba(0,0,0,.6)}",
    ".ht-root[data-ht-look=cinema]{--ht-bg:#161616;--ht-text:#FFFFFF;--ht-muted:#A3A4A5;--ht-line:rgba(255,255,255,.12);--ht-accent:#D1FE17;--ht-ink:#0B0B0B;--ht-soft:rgba(209,254,23,.13);--ht-dim:rgba(0,0,0,.7);--ht-shadow:0 18px 50px rgba(0,0,0,.65)}",
    ".ht-root *{box-sizing:border-box}",
    ".ht-block{position:fixed;inset:0;z-index:" + Z + ";background:transparent;cursor:default}",
    // The backdrop is four panels around the target (a huge box-shadow froze some studio pages while painting).
    ".ht-dim{position:fixed;z-index:" + (Z + 1) + ";left:0;top:0;width:0;height:0;background:var(--ht-dim);pointer-events:none;transition:left .2s ease,top .2s ease,width .2s ease,height .2s ease}",
    ".ht-ring{position:fixed;z-index:" + (Z + 1) + ";border-radius:6px;pointer-events:none;outline:2px solid var(--ht-accent);outline-offset:0;transition:left .2s ease,top .2s ease,width .2s ease,height .2s ease}",
    ".ht-ring.ht-none{display:none}",
    ".ht-card{position:fixed;z-index:" + (Z + 2) + ";width:min(380px,calc(100vw - 32px));max-height:calc(100vh - 32px);overflow:auto;background:var(--ht-bg);color:var(--ht-text);border:1px solid var(--ht-line);border-radius:14px;box-shadow:var(--ht-shadow);padding:16px 18px 14px;transition:left .22s ease,top .22s ease;animation:ht-in .18s ease-out}",
    "@keyframes ht-in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}",
    ".ht-top{display:flex;align-items:center;gap:10px;margin:0 0 6px;font-size:11.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--ht-muted)}",
    ".ht-top .ht-count{margin-left:auto;letter-spacing:.04em;text-transform:none;font-weight:600}",
    ".ht-card .ht-h{margin:0 0 6px;font-weight:700;font-size:17px;line-height:1.25;font-family:inherit;color:var(--ht-text);letter-spacing:.005em;text-transform:none}",
    ".ht-root[data-ht-look=cinema] .ht-h{font-family:'Space Grotesk',Inter,system-ui,sans-serif}",
    ".ht-body p{margin:0 0 6px;color:var(--ht-text)}",
    ".ht-body ul{margin:4px 0 6px;padding:0 0 0 18px}.ht-body li{margin:2px 0}.ht-body b{font-weight:700}",
    ".ht-body kbd{font-weight:600;font-size:12px;line-height:1;font-family:inherit;padding:2px 5px;border:1px solid var(--ht-line);border-radius:5px}",
    ".ht-bar{height:3px;border-radius:3px;background:var(--ht-line);margin:10px 0 12px;overflow:hidden}.ht-bar i{display:block;height:100%;background:var(--ht-accent);transition:width .22s ease}",
    ".ht-acts{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 12px}",
    ".ht-btns{display:flex;align-items:center;gap:8px}.ht-btns .ht-sp{flex:1}",
    ".ht-btn{appearance:none;font-weight:600;font-size:13px;line-height:1.1;font-family:inherit;padding:8px 13px;border-radius:9px;border:1px solid var(--ht-line);background:transparent;color:var(--ht-text);cursor:pointer;white-space:nowrap;text-transform:none;letter-spacing:normal;min-height:34px}",
    ".ht-btn:hover{background:var(--ht-soft)}",
    ".ht-btn.ht-primary{background:var(--ht-accent);border-color:var(--ht-accent);color:var(--ht-ink)}.ht-btn.ht-primary:hover{filter:brightness(1.06)}",
    ".ht-btn.ht-link{border-color:transparent;color:var(--ht-muted);padding-left:4px;padding-right:4px}",
    ".ht-btn:focus-visible{outline:2px solid var(--ht-accent);outline-offset:2px}",
    ".ht-x{position:absolute;top:8px;right:8px;width:30px;height:30px;padding:0;border-radius:8px;border:0;background:transparent;color:var(--ht-muted);cursor:pointer;font-weight:400;font-size:18px;line-height:1;font-family:inherit}",
    ".ht-x:hover{background:var(--ht-soft);color:var(--ht-text)}",
    ".ht-offer{position:fixed;z-index:" + (Z - 2) + ";right:20px;bottom:20px;width:min(340px,calc(100vw - 32px));background:var(--ht-bg);color:var(--ht-text);border:1px solid var(--ht-line);border-left:4px solid var(--ht-accent);border-radius:14px;box-shadow:var(--ht-shadow);padding:14px 16px 12px;animation:ht-in .2s ease-out}",
    ".ht-offer .ht-h{margin:0 26px 4px 0;font-weight:700;font-size:15.5px;line-height:1.3;font-family:inherit;color:var(--ht-text);text-transform:none;letter-spacing:normal}",
    ".ht-offer p{margin:0 0 10px;color:var(--ht-muted);font-size:13.5px}",
    ".ht-launch{position:fixed;z-index:" + (Z - 3) + ";left:14px;bottom:14px;display:inline-flex;align-items:center;gap:6px;height:30px;padding:0 11px 0 7px;border-radius:999px;border:1px solid var(--ht-line);background:var(--ht-bg);color:var(--ht-muted);font:600 12.5px/1 Inter,'Segoe UI',system-ui,sans-serif;box-shadow:0 4px 14px rgba(0,0,0,.18);opacity:.86;cursor:pointer;white-space:nowrap;text-transform:none;letter-spacing:normal}",
    ".ht-launch:hover,.ht-launch:focus-visible{opacity:1;color:var(--ht-text);border-color:var(--ht-accent);outline:none}",
    ".ht-launch .ht-q{width:18px;height:18px;border-radius:50%;background:var(--ht-soft);color:var(--ht-accent);display:grid;place-items:center;font-weight:800;font-size:11.5px}",
    ".ht-launch.ht-docked{position:static;box-shadow:none;opacity:1;flex:none;margin:0 4px}",
    ".ht-launch[hidden]{display:none!important}",
    "@media (max-width:600px){.ht-offer{left:16px;right:16px;bottom:16px;width:auto}}",
    "@media (prefers-reduced-motion:reduce){.ht-ring,.ht-dim,.ht-card,.ht-bar i{transition:none!important}.ht-card,.ht-offer{animation:none!important}}",
  ].join("\n");

  function injectCss() {
    if (document.getElementById("ht-style")) return;
    var s = document.createElement("style");
    s.id = "ht-style";
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  // Light / dark / cinema: the shell sets data-theme + data-look, the bridge data-hub-theme + data-hub-look.
  function themeInfo() {
    var h = document.documentElement;
    var look = h.getAttribute("data-hub-look") || h.getAttribute("data-look") || "";
    var t = h.getAttribute("data-hub-theme") || h.getAttribute("data-theme") || (h.classList.contains("dark") ? "dark" : "");
    if (!t) { try { t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"; } catch (e) { t = "light"; } }
    return { theme: look === "cinema" ? "dark" : (t === "dark" ? "dark" : "light"), look: look === "cinema" ? "cinema" : "classic" };
  }
  function paint(el) {
    var ti = themeInfo();
    el.setAttribute("data-ht-theme", ti.theme);
    el.setAttribute("data-ht-look", ti.look);
  }

  // ------------------------------------------------------------------ finding targets
  function visible(el) {
    if (!el || !el.isConnected || !el.getBoundingClientRect) return false;
    var r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    var cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none" || parseFloat(cs.opacity) === 0) return false;
    return true;
  }
  var norm = function (s) { return String(s || "").replace(/\s+/g, " ").trim().toLowerCase(); };
  function labelOf(el) { return [norm(el.textContent), norm(el.getAttribute("aria-label")), norm(el.getAttribute("title"))]; }
  function queryOne(spec) {
    var m = /^(.*?)::(text|contains)\((.*)\)$/.exec(spec);
    if (!m) {
      var all = document.querySelectorAll(spec);
      for (var i = 0; i < all.length; i++) if (visible(all[i])) return all[i];
      return null;
    }
    var css = m[1].trim() || "*", mode = m[2], want = norm(m[3]);
    var els = document.querySelectorAll(css), best = null;
    for (var j = 0; j < els.length; j++) {
      var el = els[j];
      if (!visible(el)) continue;
      var labels = labelOf(el), hit = false;
      for (var k = 0; k < labels.length; k++) {
        var l = labels[k];
        if (!l) continue;
        if (mode === "contains" ? l.indexOf(want) >= 0 : (l === want || l.indexOf(want + " ") === 0 || l.indexOf(want) === 0 && l.length <= want.length + 3)) { hit = true; break; }
      }
      if (hit) { if (norm(el.textContent) === want) return el; if (!best) best = el; }
    }
    return best;
  }
  function find(spec) {
    try {
      if (!spec) return null;
      if (typeof spec === "function") { var r = spec(); return r && visible(r) ? r : null; }
      if (Array.isArray(spec)) { for (var i = 0; i < spec.length; i++) { var e = find(spec[i]); if (e) return e; } return null; }
      return queryOne(String(spec));
    } catch (e) { return null; }
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function waitFind(spec, ms) {
    var t0 = Date.now();
    return new Promise(function (resolve) {
      (function tick() {
        var el = find(spec);
        if (el || Date.now() - t0 >= ms) return resolve(el);
        setTimeout(tick, 120);
      })();
    });
  }
  function withTimeout(p, ms) { return Promise.race([Promise.resolve(p).catch(function () {}), sleep(ms)]); }
  // Helpers handed to before() and actions: open a page, click a tab, wait.
  var ctx = {
    find: find, wait: sleep, waitFind: waitFind,
    go: function (hash) { if (location.hash !== hash) location.hash = hash; return sleep(60); },
    click: function (spec) { var el = find(spec); if (el) { try { el.click(); } catch (e) { /* ignore */ } } return sleep(el ? 250 : 0).then(function () { return !!el; }); },
  };

  // ------------------------------------------------------------------ the running tour
  var T = null;          // { tour, opts, idx, token, el, nodes..., report: [] }

  function build() {
    injectCss();
    var root = document.createElement("div");
    root.className = "ht-root";
    root.innerHTML = '<div class="ht-block" aria-hidden="true"></div><div class="ht-dim" aria-hidden="true"></div><div class="ht-dim" aria-hidden="true"></div><div class="ht-dim" aria-hidden="true"></div><div class="ht-dim" aria-hidden="true"></div><div class="ht-ring ht-none" aria-hidden="true"></div>' +
      '<div class="ht-card" role="dialog" aria-modal="true" aria-labelledby="ht-title" aria-describedby="ht-body" tabindex="-1">' +
      '<div class="ht-top"><span class="ht-tour"></span><span class="ht-count" aria-live="polite"></span></div>' +
      '<div class="ht-h" role="heading" aria-level="2" id="ht-title"></div><div class="ht-body" id="ht-body"></div><div class="ht-bar"><i></i></div><div class="ht-acts" hidden></div>' +
      '<div class="ht-btns"><button type="button" class="ht-btn ht-link" data-ht="skip">Skip tour</button><span class="ht-sp"></span>' +
      '<button type="button" class="ht-btn" data-ht="back">Back</button><button type="button" class="ht-btn ht-primary" data-ht="next">Next</button></div></div>';
    document.body.appendChild(root);
    paint(root);
    root.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest("[data-ht]");
      if (!b) { e.stopPropagation(); return; }
      e.preventDefault(); e.stopPropagation();
      var a = b.getAttribute("data-ht");
      if (a === "next") next(); else if (a === "back") back(); else if (a === "skip") stop(false);
    });
    // Nothing behind the tour can be clicked or scrolled into by accident.
    ["mousedown", "pointerdown", "touchstart", "dblclick", "contextmenu"].forEach(function (ev) {
      root.querySelector(".ht-block").addEventListener(ev, function (e) { e.preventDefault(); e.stopPropagation(); }, { passive: false });
    });
    return root;
  }

  function onKey(e) {
    if (!T) return;
    var k = e.key;
    if (k === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); stop(false); return; }
    if (k === "ArrowRight" || k === "ArrowLeft") {
      e.preventDefault(); e.stopImmediatePropagation();
      if (k === "ArrowRight") next(); else back();
      return;
    }
    if (k === "Tab") {
      var card = T.root.querySelector(".ht-card");
      var f = Array.prototype.filter.call(card.querySelectorAll("button"), function (b) { return !b.hidden && !b.disabled && b.offsetParent !== null; });
      if (!f.length) return;
      var i = f.indexOf(document.activeElement);
      e.preventDefault(); e.stopImmediatePropagation();
      var n = e.shiftKey ? (i <= 0 ? f.length - 1 : i - 1) : (i < 0 || i >= f.length - 1 ? 0 : i + 1);
      f[n].focus();
      return;
    }
    // Typing must not reach the page behind the tour (Enter / Space still press the focused tour button).
    if (!T.root.contains(document.activeElement)) { e.stopImmediatePropagation(); }
  }

  // Scroll / resize / the timer only ask for a placement; it runs once per frame at most.
  var frame = 0;
  function schedule() { if (frame || !T) return; frame = requestAnimationFrame(function () { frame = 0; place(); }); }

  function place() {
    if (!T) return;
    var root = T.root, ring = root.querySelector(".ht-ring"), card = root.querySelector(".ht-card");
    paint(root);
    var vw = window.innerWidth, vh = window.innerHeight, M = 12, G = 16;
    var el = T.el && T.el.isConnected && visible(T.el) ? T.el : null;
    var narrow = vw < 600;
    card.style.width = narrow ? (vw - 2 * G) + "px" : "";
    var cw = card.offsetWidth, ch = card.offsetHeight;
    var dims = root.querySelectorAll(".ht-dim");
    var box = function (d, x, y, w, h) { d.style.left = x + "px"; d.style.top = y + "px"; d.style.width = Math.max(0, w) + "px"; d.style.height = Math.max(0, h) + "px"; };
    if (!el) {
      box(dims[0], 0, 0, vw, vh); box(dims[1], 0, vh, vw, 0); box(dims[2], 0, 0, 0, 0); box(dims[3], vw, 0, 0, 0);
      ring.classList.add("ht-none");
      card.style.left = Math.max(G, (vw - cw) / 2) + "px";
      card.style.top = Math.max(G, (vh - ch) / 2) + "px";
      return;
    }
    var r = el.getBoundingClientRect(), pad = T.pad;
    var x1 = Math.max(4, r.left - pad), y1 = Math.max(4, r.top - pad), x2 = Math.min(vw - 4, r.right + pad), y2 = Math.min(vh - 4, r.bottom + pad);
    if (x2 - x1 < 8 || y2 - y1 < 8) { x1 = r.left; y1 = r.top; x2 = r.right; y2 = r.bottom; }
    box(dims[0], 0, 0, vw, y1); box(dims[1], 0, y2, vw, vh - y2); box(dims[2], 0, y1, x1, y2 - y1); box(dims[3], x2, y1, vw - x2, y2 - y1);
    ring.classList.remove("ht-none");
    ring.style.left = x1 + "px"; ring.style.top = y1 + "px"; ring.style.width = (x2 - x1) + "px"; ring.style.height = (y2 - y1) + "px";
    var left, top;
    var clampX = function (x) { return Math.max(G, Math.min(vw - cw - G, x)); };
    var clampY = function (y) { return Math.max(G, Math.min(vh - ch - G, y)); };
    if (narrow) {
      left = G;
      var mid = (y1 + y2) / 2;
      if (vh - y2 - M >= ch + G) top = y2 + M;                 // room under the target
      else if (y1 - M >= ch + G) top = y1 - M - ch;            // room above it
      else top = mid > vh / 2 ? G : vh - ch - G;               // dock to the far edge
    } else if (vh - y2 - M >= ch + G) { top = y2 + M; left = clampX((x1 + x2) / 2 - cw / 2); }
    else if (y1 - M >= ch + G) { top = y1 - M - ch; left = clampX((x1 + x2) / 2 - cw / 2); }
    else if (vw - x2 - M >= cw + G) { left = x2 + M; top = clampY((y1 + y2) / 2 - ch / 2); }
    else if (x1 - M >= cw + G) { left = x1 - M - cw; top = clampY((y1 + y2) / 2 - ch / 2); }
    else { left = vw - cw - G - 8; top = vh - ch - G - 8; }   // a target that fills the screen: card in its corner
    card.style.left = Math.round(left) + "px";
    card.style.top = Math.round(Math.max(G, top)) + "px";
  }

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function val(v) { try { return typeof v === "function" ? v(ctx) : v; } catch (e) { return ""; } }

  function render(step) {
    var root = T.root, card = root.querySelector(".ht-card"), steps = T.tour.steps;
    var shown = T.visibleCount(), pos = T.position();
    root.querySelector(".ht-tour").textContent = T.tour.title || "Tour";
    root.querySelector(".ht-count").textContent = "Step " + pos + " of " + shown;
    root.querySelector("#ht-title").textContent = val(step.title) || "";
    var text = val(step.text), list = val(step.list);
    var html = "";
    (Array.isArray(text) ? text : (text ? [text] : [])).forEach(function (p) { html += "<p>" + p + "</p>"; });
    if (list && list.length) html += "<ul>" + list.map(function (li) { return "<li>" + li + "</li>"; }).join("") + "</ul>";
    root.querySelector("#ht-body").innerHTML = html;
    root.querySelector(".ht-bar i").style.width = Math.round((pos / Math.max(1, shown)) * 100) + "%";
    var acts = root.querySelector(".ht-acts"), list2 = val(step.actions) || [];
    acts.hidden = !list2.length; acts.innerHTML = "";
    list2.forEach(function (a) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "ht-btn" + (a.primary ? " ht-primary" : "");
      b.textContent = a.label;
      b.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); var fn = a.run; stop(true); try { fn && fn(ctx); } catch (err) { /* ignore */ } });
      acts.appendChild(b);
    });
    var last = T.idx === lastIndex();
    var nextBtn = root.querySelector('[data-ht="next"]');
    nextBtn.textContent = last ? "Done" : "Next";
    root.querySelector('[data-ht="back"]').hidden = pos <= 1;
    root.querySelector('[data-ht="skip"]').hidden = last;
    place();
    try { nextBtn.focus({ preventScroll: true }); } catch (e) { nextBtn.focus(); }
  }
  function lastIndex() {
    for (var i = T.tour.steps.length - 1; i >= 0; i--) if (!T.skipped[i]) return i;
    return T.tour.steps.length - 1;
  }

  async function show(i, dir) {
    if (!T) return;
    var token = ++T.token, steps = T.tour.steps;
    while (i >= 0 && i < steps.length) {
      var st = steps[i] || {};
      var ok = true;
      if (st.when) { try { ok = !!st.when(ctx); } catch (e) { ok = false; } }
      if (ok) {
        if (st.before) await withTimeout(Promise.resolve().then(function () { return st.before(ctx); }), 4000);
        if (!T || token !== T.token) return;
        var el = st.sel ? await waitFind(st.sel, st.wait != null ? st.wait : 1500) : null;
        if (!T || token !== T.token) return;
        T.report[i] = { index: i, title: val(st.title), sel: typeof st.sel === "function" ? "(function)" : st.sel || null, found: st.sel ? !!el : null, shown: !(st.sel && !el && st.skipIfMissing) };
        if (!(st.sel && !el && st.skipIfMissing)) {
          T.skipped[i] = false;
          T.idx = i; T.el = el; T.pad = st.pad != null ? st.pad : 6;
          if (el) {
            var r = el.getBoundingClientRect();
            if (r.top < 0 || r.bottom > window.innerHeight || r.left < 0 || r.right > window.innerWidth) {
              // instant: a smooth scroll of a tall target could keep some pages busy re-laying out every frame
              try { el.scrollIntoView({ block: r.height > window.innerHeight * 0.8 ? "start" : "center", inline: "nearest", behavior: "auto" }); } catch (e) { /* ignore */ }
            }
          }
          render(st);
          if (T.opts.onStep) { try { T.opts.onStep(info()); } catch (e) { /* ignore */ } }
          return;
        }
      } else {
        T.report[i] = { index: i, title: val(st.title), sel: null, found: null, shown: false, reason: "not applicable" };
      }
      T.skipped[i] = true;
      i += dir || 1;
    }
    if (i < 0) { if (T.idx >= 0) return; return show(0, 1); }
    stop(true);
  }

  function next() { if (T) { if (T.idx === lastIndex()) stop(true); else show(T.idx + 1, 1); } }
  function back() { if (T && T.idx > 0) show(T.idx - 1, -1); }
  function stop(completed) {
    if (!T) return;
    var t = T; T = null;
    window.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", schedule, true);
    window.removeEventListener("scroll", schedule, true);
    clearInterval(t.timer);
    try { t.root.remove(); } catch (e) { /* ignore */ }
    try { if (t.prevFocus && t.prevFocus.focus && t.prevFocus.isConnected) t.prevFocus.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
    HubTour.lastReport = { id: t.tour.id, completed: !!completed, steps: t.report.filter(Boolean) };
    if (t.opts.onEnd) { try { t.opts.onEnd({ completed: !!completed, index: t.idx, report: HubTour.lastReport }); } catch (e) { /* ignore */ } }
  }
  function info() {
    if (!T) return null;
    var st = T.tour.steps[T.idx] || {};
    return { tour: T.tour.id, index: T.idx, position: T.position(), of: T.visibleCount(), title: val(st.title), found: st.sel ? !!T.el : null, sel: typeof st.sel === "function" ? "(function)" : st.sel || null };
  }

  function start(tour, opts) {
    if (!tour || !tour.steps || !tour.steps.length) return false;
    if (T) stop(false);
    closeOffer();
    opts = opts || {};
    T = { tour: tour, opts: opts, idx: -1, token: 0, el: null, pad: 6, report: [], skipped: {}, prevFocus: document.activeElement };
    T.visibleCount = function () { var n = 0; for (var i = 0; i < tour.steps.length; i++) if (!T.skipped[i]) n++; return n; };
    T.position = function () { var n = 0; for (var i = 0; i <= T.idx; i++) if (!T.skipped[i]) n++; return Math.max(1, n); };
    T.root = build();
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", schedule, true);
    window.addEventListener("scroll", schedule, true);
    T.timer = setInterval(schedule, 300);          // layouts that move without a scroll / resize event (panels, fonts)
    show(0, 1);
    return true;
  }

  // ------------------------------------------------------------------ the first-visit offer
  var offerEl = null;
  function closeOffer() { if (offerEl) { try { offerEl.remove(); } catch (e) { /* ignore */ } offerEl = null; } }
  function offer(o) {
    injectCss();
    closeOffer();
    var root = document.createElement("div");
    root.className = "ht-root";
    paint(root);
    var tid = "ht-offer-t-" + Math.random().toString(36).slice(2, 7);
    root.innerHTML = '<div class="ht-offer" role="dialog" aria-modal="false" aria-labelledby="' + tid + '">' +
      '<button type="button" class="ht-x" data-o="later" aria-label="Close">×</button>' +
      '<div class="ht-h" role="heading" aria-level="2" id="' + tid + '">' + esc(o.title || "New here?") + '</div><p>' + esc(o.text || "") + '</p>' +
      '<div class="ht-btns"><button type="button" class="ht-btn ht-primary" data-o="start">' + esc(o.startLabel || "Take the tour") + '</button>' +
      '<button type="button" class="ht-btn ht-link" data-o="later">' + esc(o.laterLabel || "Not now") + '</button></div></div>';
    root.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest("[data-o]");
      if (!b) return;
      e.preventDefault(); e.stopPropagation();
      var a = b.getAttribute("data-o");
      closeOffer();
      try { if (a === "start") { o.onStart && o.onStart(); } else { o.onLater && o.onLater(); } } catch (err) { /* ignore */ }
    });
    root.addEventListener("keydown", function (e) { if (e.key === "Escape") { e.stopPropagation(); closeOffer(); try { o.onLater && o.onLater(); } catch (err) { /* ignore */ } } });
    document.body.appendChild(root);
    offerEl = root;
    var themeTimer = setInterval(function () { if (!offerEl || offerEl !== root) { clearInterval(themeTimer); return; } paint(root); }, 800);
    return root;
  }

  // ------------------------------------------------------------------ a small "Tour" button
  // opts = { label, title, onClick, dock: element to sit before (inside a studio's top bar), style: {css} }
  function launcher(o) {
    injectCss();
    var b = document.createElement("button");
    b.type = "button";
    b.className = "ht-root ht-launch" + (o.dock ? " ht-docked" : "");
    b.id = o.id || "hub-tour-btn";
    b.title = o.title || "Guided tour";
    b.setAttribute("aria-label", o.title || "Start the guided tour");
    b.innerHTML = '<span class="ht-q" aria-hidden="true">?</span>' + esc(o.label || "Tour");
    if (o.style) for (var k in o.style) b.style[k] = o.style[k];
    b.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); try { o.onClick && o.onClick(); } catch (err) { /* ignore */ } });
    paint(b);
    setInterval(function () { paint(b); b.hidden = !!T || !!offerEl; }, 700);
    if (o.dock && o.dock.parentNode) o.dock.parentNode.insertBefore(b, o.dock); else document.body.appendChild(b);
    return b;
  }

  var HubTour = window.HubTour = {
    start: start, stop: function () { stop(false); }, next: next, back: back, info: info,
    active: function () { return !!T; }, offer: offer, launcher: launcher, closeOffer: closeOffer, offerOpen: function () { return !!offerEl; },
    find: find, ctx: ctx, lastReport: null, version: 1,
  };
})();
