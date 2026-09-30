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
    if (cfg.tool === "image" || cfg.tool === "tts" || cfg.tool === "music") html.setAttribute("data-theme", t);
    if (cfg.tool === "video") { html.classList.toggle("dark", t === "dark"); html.classList.toggle("light", t !== "dark"); }
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
      if ((cfg.tool === "image" || cfg.tool === "tts" || cfg.tool === "music") && html.getAttribute("data-theme") !== want) applyTheme(want);
      if (cfg.tool === "video" && html.classList.contains("dark") !== (want === "dark")) applyTheme(want);
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
        xhr.addEventListener("loadend", function () { clearTimeout(t); if (xhr.status === 503) showBanner("The GPU could not be reserved", "warn", 9000); else hideBanner(); });
      }
      return origSend.apply(this, arguments);
    };
  }

  // ------------------------------------------------------------------ talk to the shell
  window.addEventListener("message", function (e) {
    var d = e.data || {};
    if (d.type === "hub:theme" && d.theme) applyTheme(d.theme);
    if (d.type === "hub:notice" && d.text) showBanner(d.text, d.tone, d.ttl || 6000);
  });
  function announce() { try { if (window.parent && window.parent !== window) window.parent.postMessage({ type: "hub:ready", tool: cfg.tool }, "*"); } catch (e) { /* ignore */ } }
  if (document.readyState === "complete" || document.readyState === "interactive") announce(); else window.addEventListener("DOMContentLoaded", announce);
})();
