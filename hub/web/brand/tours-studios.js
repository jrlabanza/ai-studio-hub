/* Studio tours - one per studio, run inside the studio's own page by the hub bridge (bridge.js) with the shared
   engine (tour.js). The selectors were picked from each studio's UI code: ids, data-* attributes, title attributes
   and exact visible text, never generated class chains. A step whose target is missing is shown centred, so a
   changed studio UI never breaks a tour.

   Safety: the "before" actions only click tabs / mode switches / a History card (views that change nothing on the
   server). They never press Generate, Plan, Enhance, upload, delete, a model switch that loads, or a setting.
   Whatever they switch is put back when the tour ends (start() returns the restore function).

   The shell loads this file too (/static/brand/tours-studios.js) to list the tours on its Tours page. */
(function () {
  "use strict";
  var F = function (s) { return window.HubTour ? window.HubTour.find(s) : null; };
  var up = function (sel, closest) { return function () { var el = F(sel); return el ? (closest ? el.closest(closest) : el.parentElement) : null; }; };
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  function click(sel) { var el = F(sel); if (el) { try { el.click(); } catch (e) { /* ignore */ } } return sleep(el ? 300 : 0); }
  function ls(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

  var T = {};

  // ------------------------------------------------------------------ Image Studio (qwen-image-studio/web)
  T.image = {
    name: "Image Studio", minutes: 1, blurb: "Modes, Qwen or Iris, the prompt, references, Generate and History.",
    ready: "#modeTabs", button: { dock: "#btnSettings" },
    start: function () {
      var on = document.querySelector("#modeTabs button.on");
      var mode = on && on.getAttribute("data-mode");
      return function () { if (mode) click('#modeTabs button[data-mode="' + mode + '"]'); };
    },
    steps: [
      { sel: ["#modelPill", "header.topbar"], title: "Welcome to Image Studio",
        text: ["Pictures from a description, edits of your own photos, cut-outs and transparent images.", "This light shows whether the model is loaded. The first Generate loads it for you."] },
      { sel: "#modeTabs", title: "Pick what to do",
        list: ["<b>Text to Image</b> - a new picture from words", "<b>Edit &amp; Combine</b> - change or mix up to 10 photos", "<b>Local Edit</b> - change just one area", "<b>Transparent</b> and <b>Extract Subject</b> - cut-outs"] },
      { before: function () { return click('#modeTabs button[data-mode="t2i"]'); }, sel: ["#modelSeg", "#modelBlock"], title: "Qwen or Iris",
        text: ["<b>Qwen-Image</b> gives the best quality. <b>Iris Turbo</b> makes fast drafts.", "Switching only changes what the next Generate uses."] },
      { sel: "#prompt", title: "Describe the picture",
        text: ["Say what you want to see: the subject, the place, the style, the light.", "Examples has ideas; Enhance rewrites your words into a fuller prompt."] },
      { sel: ["#sizeModeSeg", "#sizeReadout"], title: "Shape and size",
        text: "Pick a preset shape or let the studio match your picture. Bigger sizes and more steps take longer." },
      { before: function () { return click('#modeTabs button[data-mode="edit"]'); }, sel: ["#refBlock", "#dropzone"], title: "Reference pictures",
        text: ["In Edit &amp; Combine, drop your photos here.", "Refer to them in the prompt as \"image 1\", \"image 2\"."] },
      { before: function () { return click('#modeTabs button[data-mode="local"]'); }, sel: ["#localBlock", "#localDrop"], title: "Change one area",
        text: "Local Edit lets you mark the exact spot to change; the rest of the picture stays as it is." },
      { before: function () { return click('#modeTabs button[data-mode="t2i"]'); }, sel: "#btnGenerate", title: "Generate",
        text: ["Press Generate (or Ctrl+Enter).", "If another studio is busy on the GPU, your picture waits its turn in the hub's queue."] },
      { sel: ["#stage", "#viewer"], title: "Your result",
        text: "The picture appears here, with buttons to download it, edit it further or use it elsewhere." },
      { sel: ["#gallery", "#galleryGrid"], title: "History",
        text: "Everything you made is kept here. Search it, or click a picture to see it and its settings again." },
      { sel: "#btnSettings", title: "Settings",
        text: "Memory use, output format and model options. That's the tour - happy creating!" },
    ],
  };

  // ------------------------------------------------------------------ Voice Studio (qwen-tts/app/static)
  T.tts = {
    name: "Voice Studio", minutes: 2, blurb: "Preset voices, design, cloning, the voice library, Drama scenes and History.",
    ready: "#tabs .tab", button: { dock: "#btnModels" },
    start: function () {
      var on = document.querySelector("#tabs .tab.active") || document.querySelector('.tab[data-tab="' + (ls("tab") || "custom") + '"]');
      var tab = on && on.getAttribute("data-tab");
      return function () { if (tab) click('.tab[data-tab="' + tab + '"]'); };
    },
    steps: [
      { sel: "#tabs", title: "Welcome to Voice Studio",
        text: ["Each tab is a tool: speech from text, new voices, clones, long reads, conversations and whole drama scenes.", "Speech to Speech and Dubbing re-voice or translate a recording."] },
      { before: function () { return click('.tab[data-tab="custom"]'); }, sel: ["#speakerGrid", "#tab-custom"], wait: 4000, title: "Preset voices",
        text: "Pick a built-in voice. The play button on a card plays a sample." },
      { sel: ["#cvText", "#tab-custom textarea"], title: "What should be said",
        text: "Type the lines here. A style such as \"very happy\" or \"whispering\" goes in the box above." },
      { sel: "#cvGenerate", title: "Generate",
        text: "Press Generate to speak it. The right model loads by itself; if the GPU is busy it waits its turn." },
      { sel: "#cvResults", title: "Your clips",
        text: "Clips appear here to play, download, edit or save as a voice." },
      { before: function () { return click('.tab[data-tab="design"]'); }, sel: ["#vdInstruct", "#tab-design"], title: "Design a voice",
        text: "Describe a voice that doesn't exist yet - age, tone, accent - and hear it." },
      { before: function () { return click('.tab[data-tab="clone"]'); }, sel: ["#cloneDrop", "#tab-clone"], title: "Clone a voice",
        text: ["Drop a clear 3-10 second recording of someone speaking.", "Pick Qwen or Chatterbox as the clone engine; save the result to your library."] },
      { before: function () { return click('.tab[data-tab="library"]'); }, sel: ["#voiceList", "#tab-library"], title: "Voice library",
        text: "Saved voices live here and can be used in every tab - and by Video Studio." },
      { before: function () { return click('.tab[data-tab="drama"]'); }, sel: ["#drScript", "#tab-drama"], wait: 3000, title: "Drama: the script",
        text: ["Write a scene line by line as <b>Name: what they say</b>.", "Stage notes and emotions shape how each line is acted."] },
      { sel: ["#drCast", "#tab-drama"], wait: 3000, title: "Drama: the cast",
        text: "Every speaker in the script gets a voice here - a preset, a saved voice or a clone." },
      { sel: ["#drGenerate", "#drResults"], title: "Perform the scene",
        text: "Perform renders the whole scene in one take, with every voice in place." },
      { before: function () { return click('.tab[data-tab="history"]'); }, sel: ["#histList", "#tab-history"], wait: 3000, title: "History",
        text: "Every clip you made, searchable, with favourites. The Models button up top shows which model is loaded." },
    ],
  };

  // ------------------------------------------------------------------ Video Studio (video-generator/frontend)
  // The page has no top-level tabs: a Create panel (left), the editor / result (centre), History (right, >= 1280 px).
  var vAside = function () { return document.querySelector("aside"); };
  var vModel = function (name) { return function () { var a = vAside(); if (!a) return null; return Array.prototype.find.call(a.querySelectorAll("button"), function (b) { return (b.textContent || "").indexOf(name) >= 0 && b.querySelector("span"); }) || null; }; };
  var vTab = function (label) { return function () { var a = vAside(); if (!a) return null; return Array.prototype.find.call(a.querySelectorAll('[role="tablist"] [role="tab"]'), function (b) { return (b.textContent || "").trim() === label; }) || null; }; };
  var vClickFn = function (fn) { var el = fn(); if (el) { try { el.click(); } catch (e) { /* ignore */ } } return sleep(el ? 350 : 0); };
  var vEyebrow = function (txt, closest) { return function () { var e = Array.prototype.find.call(document.querySelectorAll(".eyebrow"), function (x) { return (x.textContent || "").trim() === txt && x.offsetParent !== null; }); return e ? (closest ? e.closest(closest) : e) : null; }; };
  T.video = {
    name: "Video Studio", minutes: 3, blurb: "Models and modes, prompt, references and voices, storyboard shots, Generate, results, History and the editor.",
    ready: '[role="tablist"] [role="tab"]', button: { dock: "header button::contains(Models)" },
    start: function () {
      var a = vAside(), model = null, mode = null;
      if (a) {
        var cards = Array.prototype.filter.call(a.querySelectorAll("button"), function (b) { return /LTX-2\.5|MiniMax H3/.test(b.textContent || "") && b.querySelector("span"); });
        cards.forEach(function (b) { if (/bg-accent-soft/.test(b.className)) model = (b.textContent || "").indexOf("MiniMax H3") >= 0 ? "MiniMax H3" : "LTX-2.5"; });
        var sel = a.querySelector('[role="tablist"] [role="tab"][aria-selected="true"]');
        mode = sel && (sel.textContent || "").trim();
      }
      var result = !!F("button::text(Back to edit)");
      return async function () {
        if (model) await vClickFn(vModel(model));
        if (mode) await vClickFn(vTab(mode));
        if (!result && F("button::text(Back to edit)")) await click("button::text(Back to edit)");
      };
    },
    steps: [
      { sel: up("aside button::contains(MiniMax H3)"), title: "Welcome to Video Studio - pick a model",
        text: ["<b>LTX-2.5</b>: fast, cinematic footage with sound, and long clips.", "<b>MiniMax H3</b>: talking, singing and acting people, picture and voice references, character swap."] },
      { sel: 'aside [role="tablist"]', title: "Pick a mode",
        text: ["<b>Text</b>, <b>Image</b> (animate a picture), <b>Start + End</b>, <b>Extend</b> a clip, <b>Storyboard</b> for several shots, <b>Fix</b> a clip.", "MiniMax H3 adds References, Swap, Keyframe, Edit and Motion."] },
      { before: function () { return vClickFn(vTab("Text")); }, sel: ['aside textarea[maxlength="20000"]', "aside textarea"], title: "The prompt",
        text: ["Describe who is there, where, what happens and who says what.", "Stuck? The hub's Assistant has a Video prompt writer that writes it for you."] },
      { before: async function () { await vClickFn(vModel("MiniMax H3")); await vClickFn(vTab("References")); },
        sel: [up("#image-slots", "label"), "#image-slots"], title: "References: people, places, props",
        text: ["With MiniMax H3, add pictures of your characters and places so they look the same in every shot.", "<b>Characters</b> keeps a picture set and its voice; <b>Angle set</b> asks Image Studio for four new camera angles."] },
      { sel: [up("aside span.eyebrow::text(Reference audio (optional))", "label"), "aside span.eyebrow::text(Reference audio)"], title: "Voices",
        text: "Add a voice recording - or a Voice Studio voice - so a character speaks with that voice." },
      { before: function () { return vClickFn(vTab("Storyboard")); }, sel: [vEyebrow("Shot 2", ".rounded-card"), vEyebrow("Shot 1", ".rounded-card")], title: "Storyboard shots",
        text: ["A film is a list of shots, each with its own prompt and length.", "<b>Cut</b> starts a fresh shot; <b>Continue</b> carries on from the end of the one before. Tick which references each shot uses."] },
      { sel: [up('[title^="A line someone off-screen says"]'), '[title^="A line someone off-screen says"]'], title: "Off-screen lines and Try 3 takes",
        text: ["<b>Off-screen lines</b>: Voice Studio speaks a line and mixes it in, so nobody on screen mouths it.", "<b>Try 3 takes</b> renders just this shot three times so you can pick the best one."] },
      { sel: ['[title^="Music Studio makes an instrumental"]', vEyebrow("Soundtrack (optional)")], title: "Make with Music Studio",
        text: "No soundtrack? Music Studio can make an instrumental to fit the film - it runs when you press it." },
      { sel: ["aside .border-t > button.w-full", "aside button::text(Generate)"], title: "Generate",
        text: ["Generate renders it. Long jobs wait their turn in the hub's GPU queue and run on their own.", "You can leave the page; the hub tells you when it's done."] },
      { sel: [function () { var b = F("button::text(History)"); return b ? b.closest("aside, section") || b.parentElement : null; }, "button::text(History)"], title: "History",
        text: "Every render lands here. Click one to see it; drag it onto the timeline to edit it." },
      { before: async function () { if (!F("button::text(Back to edit)")) await click('[title="Click to view · drag onto the timeline to edit"]'); await sleep(500); },
        sel: [function () { var b = F("button::text(Fix up)"); return b ? b.parentElement : null; }, '[title="Trim it, grab frames, mark timestamps, extend or edit it"]'], wait: 2500, title: "The result",
        text: ["Below a finished film: <b>Quality check</b> lists stray frames or misheard lines, and <b>Fix up</b> cuts them in a new copy.", "<b>Edit video</b> trims and extends; <b>Re-roll shots</b> renders only the shots you pick again."] },
      { before: async function () { if (F("button::text(Back to edit)")) await click("button::text(Back to edit)"); await click("button.chip::text(Preview)"); },
        sel: [up('[title="Drag to make the timeline taller or shorter"]'), '[title="Split at the playhead (S)"]'], wait: 2000, title: "The editor timeline",
        text: ["Put clips, voices and music on the timeline, split and trim them, and export the cut.", "Above it, <b>Wide</b> hides the side panels for a bigger preview and the expand button plays it full screen. Have fun making films!"] },
    ],
  };

  // ------------------------------------------------------------------ Music Studio (yue2/webui)
  T.music = {
    name: "Music Studio", minutes: 1, blurb: "Style and lyrics, the score, rendering a song and the Library.",
    ready: "#lyrics", button: { dock: "#pill-settings" },
    steps: [
      { sel: ["#pill-model", "header.topbar"], title: "Welcome to Music Studio",
        text: ["Whole songs with vocals, from a style and your lyrics.", "This shows whether the music model is loaded; Generate loads it when needed."] },
      { sel: ["aside.panel.composer", "aside"], title: "Compose",
        text: "Everything for a new song starts here, top to bottom." },
      { sel: "#style", title: "Style",
        text: "Genre, instruments, voice, language, tempo - or pick a preset next to it." },
      { sel: "#lyrics", title: "Lyrics",
        text: "Write or paste the lyrics. Mark the parts with [Verse], [Chorus] and [Bridge]." },
      { sel: [".tools.lyric-tools", "#btn-lyrics-continue"], title: "Help with the words",
        text: "Continue or write lyrics from the title with the local language model - or open \"Just describe it\" and say the song in one sentence." },
      { sel: [up("#cot", ".field"), "#cot"], title: "The score",
        text: ["Plan sets how the song is sketched first: melody and chords, melody only, or no plan.", "<b>Plan score</b> (below) writes that score so you can check it before a full render; \"Score &amp; sampling\" lets you edit it by hand."] },
      { sel: ["#btn-generate"], title: "Generate the song",
        text: "Generate song queues the render - roughly a minute of GPU time per minute of music. It waits its turn if the GPU is busy." },
      { sel: ["#player", "#result-title"], skipIfMissing: true, title: "Your song",
        text: "The open song plays here. Below it, switch between Sheet music and ABC text to read its score." },
      { sel: [".panel.library", "#songs"], title: "Library",
        text: "Every song you made. Search, filter favourites and click one to open it again." },
      { sel: "#pill-settings", title: "Settings",
        text: "Your name, notifications and the song model. That's the tour - enjoy!" },
    ],
  };

  // ------------------------------------------------------------------ Forge (its own tour, extensions/sd-forge-workspace)
  T.forge = {
    name: "Forge", minutes: 1, blurb: "Forge's own guided tour of its workspace.", external: true,
    ready: function () { return window.jrlTour && typeof window.jrlTour.start === "function"; },
    run: function () { window.jrlTour.start(); },
    resetKeys: ["jrl_tour_done"],
  };

  window.HubTourStudios = T;
})();
