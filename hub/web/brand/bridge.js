/* Hub bridge - injected into each studio's page by the hub's entrance proxy.
   Keeps the tool's theme in step with the shell, shows what the GPU orchestrator is doing while a
   generate request waits, and tells the shell when the page is ready. Nothing here changes how the
   tools work. */
(function () {
  "use strict";
  var cfg = window.__HUB || {};
  var html = document.documentElement;
  var theme = cfg.theme || "light";
  var applying = false;

  function applyTheme(t) {
    theme = t;
    applying = true;
    html.setAttribute("data-hub-theme", t);
    if (cfg.fonts) html.setAttribute("data-hub-fonts", "1"); else html.removeAttribute("data-hub-fonts");
    if (!cfg.restyle) { applying = false; return; }
    // Every studio implements the shared design language natively and honours html[data-theme];
    // the React and Gradio apps also read a "dark" class, so both are kept in step.
    html.setAttribute("data-theme", t);
    html.classList.toggle("dark", t === "dark"); html.classList.toggle("light", t !== "dark");
    if (document.body) { document.body.classList.toggle("dark", t === "dark"); document.body.classList.toggle("light", t !== "dark"); }
    html.style.colorScheme = t;
    var meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.setAttribute("content", t);
    if (cfg.tool === "image") { try { localStorage.setItem("qis_theme", t); } catch (e) { /* ignore */ } }
    applying = false;
  }
  applyTheme(theme);
  // Tools re-apply their own saved theme on load; keep ours until they settle.
  var reassert = [80, 400, 1200, 3000];
  reassert.forEach(function (ms) { setTimeout(function () { applyTheme(theme); }, ms); });
  if (cfg.restyle && window.MutationObserver) {
    new MutationObserver(function () {
      if (applying) return;
      var want = theme;
      if (html.getAttribute("data-theme") !== want || html.classList.contains("dark") !== (want === "dark")) applyTheme(want);
    }).observe(html, { attributes: true, attributeFilter: ["data-theme", "class"] });
  }

  // ------------------------------------------------------------------ banner for orchestrator feedback
  var banner, bannerTimer, pendingClaims = 0;
  function ensureBanner() {
    if (banner) return banner;
    banner = document.createElement("div");
    banner.id = "hub-banner";
    banner.innerHTML = '<span class="hub-banner-lines"><i></i><i></i><i></i><i></i><i></i></span><span class="hub-banner-text"></span>';
    document.body.appendChild(banner);
    return banner;
  }
  function showBanner(text, tone, ttl) {
    var b = ensureBanner();
    b.querySelector(".hub-banner-text").textContent = text;
    b.className = "show " + (tone || "");
    clearTimeout(bannerTimer);
    if (ttl) bannerTimer = setTimeout(hideBanner, ttl);
  }
  function hideBanner() { if (banner) banner.className = ""; }

  function isClaim(method, url) {
    try {
      var u = new URL(url, location.href);
      if (u.origin !== location.origin) return false;
      var path = u.pathname;
      var claims = cfg.claims || [];
      for (var i = 0; i < claims.length; i++) {
        var methods = claims[i][0], prefix = claims[i][1];
        if (methods.indexOf(method) >= 0 && path.indexOf(prefix) === 0) {
          var ex = cfg.exclude || [];
          for (var j = 0; j < ex.length; j++) if (path.slice(-ex[j].length) === ex[j]) return false;
          return true;
        }
      }
    } catch (e) { /* ignore */ }
    return false;
  }

  // Wrap fetch: while a GPU request is on its way through the orchestrator, say so.
  if (window.fetch) {
    var origFetch = window.fetch;
    window.fetch = function (input, init) {
      var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
      var url = typeof input === "string" ? input : (input && input.url) || "";
      var claim = isClaim(method, url);
      var slowTimer;
      if (claim) {
        pendingClaims++;
        slowTimer = setTimeout(function () { showBanner("Making room on the GPU for " + (cfg.name || "this studio") + "…", "busy"); }, 900);
      }
      var p = origFetch.apply(this, arguments);
      if (claim) {
        p.then(function (r) {
          clearTimeout(slowTimer); pendingClaims--;
          if (r && r.status === 503) {
            r.clone().json().then(function (d) { showBanner((d && d.detail) || "The GPU could not be reserved", "warn", 9000); }).catch(function () { showBanner("The GPU could not be reserved", "warn", 9000); });
          } else if (r && r.status >= 400 && r.status < 500) {
            // The studio rejected the request (missing picture, wrong model for the mode, ...): say why, prominently.
            var who = cfg.name || "The studio";
            r.clone().json().then(function (d) { showBanner(who + " did not accept this: " + ((d && (d.detail || d.error)) || ("HTTP " + r.status)), "warn", 12000); }).catch(function () { showBanner(who + " did not accept this (HTTP " + r.status + ")", "warn", 12000); });
          } else if (pendingClaims <= 0) hideBanner();
        }, function () { clearTimeout(slowTimer); pendingClaims--; if (pendingClaims <= 0) hideBanner(); });
      }
      return p;
    };
  }
  // Same for XMLHttpRequest (older code paths / uploads).
  if (window.XMLHttpRequest) {
    var origOpen = XMLHttpRequest.prototype.open, origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) { this.__hubClaim = isClaim(String(method).toUpperCase(), url); return origOpen.apply(this, arguments); };
    XMLHttpRequest.prototype.send = function () {
      if (this.__hubClaim) {
        var xhr = this, t = setTimeout(function () { showBanner("Making room on the GPU for " + (cfg.name || "this studio") + "…", "busy"); }, 900);
        xhr.addEventListener("loadend", function () {
          clearTimeout(t);
          if (xhr.status === 503) showBanner("The GPU could not be reserved", "warn", 9000);
          else if (xhr.status >= 400 && xhr.status < 500) {
            var why = "";
            try { var d = JSON.parse(xhr.responseText); why = (d && (d.detail || d.error)) || ""; } catch (e) { /* not JSON */ }
            showBanner((cfg.name || "The studio") + " did not accept this: " + (why || ("HTTP " + xhr.status)), "warn", 12000);
          } else hideBanner();
        });
      }
      return origSend.apply(this, arguments);
    };
  }

  // ------------------------------------------------------------------ "Send to": receiving an output from another studio
  // The shell posts {type:"hub:import", slot, url, name, kind, mime, from:{tool,title}}. ``url`` is same-origin
  // (/__hub/media/<tool>/<path>), so the page can simply fetch() it. A studio takes the file by defining
  //   window.hubImport = async function (detail) { ...; return { ok: true, message: "..." }; }
  // and, if it lists what it accepts, window.hubImportSlots = ["edit", ...]. Until the receiver exists the
  // request waits (the app may still be booting), then the outcome is reported back to the shell.
  var pendingImports = [];
  function reply(detail, ok, message) {
    try { if (window.parent && window.parent !== window) window.parent.postMessage({ type: "hub:imported", tool: cfg.tool, slot: detail.slot, ok: !!ok, message: message || "" }, "*"); } catch (e) { /* ignore */ }
  }
  function deliver(detail) {
    var fn = window.hubImport;
    if (typeof fn !== "function") return false;
    var res;
    try { res = fn(detail); } catch (err) { reply(detail, false, String(err && err.message || err)); return true; }
    Promise.resolve(res).then(function (r) {
      var ok = !r || r.ok !== false;
      reply(detail, ok, (r && r.message) || (ok ? "Received" : "The studio could not take this file"));
      if (ok) showBanner((r && r.message) || ("Received from " + ((detail.from && detail.from.tool) || "the hub")), "", 4000);
      else showBanner((r && r.message) || "Could not take this file", "warn", 8000);
    }, function (err) { reply(detail, false, String(err && err.message || err)); showBanner("Could not take this file: " + (err && err.message || err), "warn", 8000); });
    return true;
  }
  window.hubImportFetch = function (detail) {
    // Helper for receivers: the file as a File object (name and type filled in).
    return fetch(detail.url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status + " fetching the file");
      return r.blob();
    }).then(function (blob) {
      var type = detail.mime || blob.type || "application/octet-stream";
      try { return new File([blob], detail.name || "import", { type: type }); } catch (e) { blob.name = detail.name; return blob; }
    });
  };
  var importTimer = setInterval(function () {
    if (!pendingImports.length) return;
    if (typeof window.hubImport !== "function") return;
    var queue = pendingImports; pendingImports = [];
    queue.forEach(function (q) { clearTimeout(q.timer); deliver(q.detail); });
  }, 400);
  function onImport(detail) {
    if (deliver(detail)) return;
    showBanner("Waiting for " + (cfg.name || "the studio") + " to be ready…", "busy", 30000);
    var q = { detail: detail };
    q.timer = setTimeout(function () {
      var i = pendingImports.indexOf(q); if (i >= 0) pendingImports.splice(i, 1);
      reply(detail, false, (cfg.name || "This studio") + " has no receiver for \"Send to\" yet");
      showBanner("This studio cannot receive files yet", "warn", 8000);
    }, 60000);
    pendingImports.push(q);
  }

  // ------------------------------------------------------------------ talk to the shell
  window.addEventListener("message", function (e) {
    var d = e.data || {};
    if (d.type === "hub:theme" && d.theme) applyTheme(d.theme);
    if (d.type === "hub:notice" && d.text) showBanner(d.text, d.tone, d.ttl || 6000);
    if (d.type === "hub:import" && d.url && d.slot) onImport(d);
  });
  function announce() { try { if (window.parent && window.parent !== window) window.parent.postMessage({ type: "hub:ready", tool: cfg.tool }, "*"); } catch (e) { /* ignore */ } }
  if (document.readyState === "complete" || document.readyState === "interactive") announce(); else window.addEventListener("DOMContentLoaded", announce);
})();
