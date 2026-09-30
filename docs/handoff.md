# "Send to" - handing an output from one studio to another

Every studio's outputs are in the hub's Library. From there (the viewer's **Send to…** button) an image,
clip, video or song can be handed straight to another studio: no download, no upload. The hub opens the
target studio in the shell, waits for it, and delivers the file into the slot you chose - the start frame
of a video, the reference voice of a clone, the image to edit, and so on.

## How it travels

1. The shell asks `GET /api/handoff/targets?kind=image` which studios take an image and into which slots.
   The slots are declared by each studio's `ToolSpec.import_slots` in `hub/tools.py`.
2. The shell navigates to the target studio and, once that studio's page has announced itself
   (`hub:ready`), posts a message into its frame:

   ```json
   { "type": "hub:import", "slot": "i2v", "slotLabel": "Image to video - as the start frame",
     "url": "/__hub/media/image/2026-09-29/abc.png", "name": "abc.png", "kind": "image",
     "from": { "tool": "image", "name": "Image Studio", "title": "a red bicycle…" },
     "meta": { "width": 1024, "height": 1024, "duration": null, "model": "Qwen-Image-2.1" } }
   ```

   `url` is served by the target's own entrance (`/__hub/media/<source tool>/<path>`), so the page can
   `fetch()` it as a same-origin URL - the hub reads the file from the source studio's output folder.
3. The hub bridge (`bridge.js`, injected into every studio page) receives the message and calls the
   studio's receiver. The outcome goes back to the shell as `hub:imported` and shows as a toast; the
   studio's page shows the hub banner.

## What a studio implements: the receiver

One global function on the studio's page, set by its own front-end code once the app is ready:

```js
window.hubImport = async function (detail) {
  const file = await window.hubImportFetch(detail);   // a File: name and type filled in
  switch (detail.slot) {
    case "i2v":       setMode("i2v"); await setStartImage(file); break;
    case "flf_end":   setMode("flf2v"); await setEndImage(file); break;
    default:          return { ok: false, message: "Unknown slot " + detail.slot };
  }
  return { ok: true, message: "Start frame set - press Generate" };
};
```

* `window.hubImportFetch(detail)` is provided by the bridge: it fetches `detail.url` and returns a `File`
  (`detail.name`, `detail.mime` or the response type).
* Return `{ ok: true, message }` or `{ ok: false, message }` (or throw); the message is shown to the user.
* Put the file exactly where a drag-and-drop or file picker would put it, switch the UI to the matching
  mode/tab, and scroll it into view. Do not start generating - the user presses Generate.
* If the receiver is defined only after the app has initialised, that is fine: the bridge keeps the request
  for up to 60 s and delivers as soon as `window.hubImport` exists (it shows "Waiting for … to be ready").
* The receiver only exists inside the hub (the bridge is injected by the entrance). Standalone, nothing
  changes for the studio.

## Slots today

| Studio | slot | takes | lands in |
|---|---|---|---|
| Image Studio | `edit` | image | Edit & Combine, as a reference image |
| | `local` | image | Local edit, as the image to edit |
| | `extract` | image | Extract subject |
| Voice Studio | `clone` | audio | Voice clone, as the reference voice |
| | `sts` | audio | Speech to speech, as the source |
| | `dub` | video, audio | Dubbing, as the video (or audio) |
| Video Studio | `i2v` | image | Image to video, start frame |
| | `flf_start` / `flf_end` | image | First + last frame |
| Music Studio | `voice` | audio | Sing it in this voice, the voice reference |
| | `cover` | audio | Cover this recording |
| Forge Studio | `edit` | image | Studio app, Edit this image |
| | `img2img` | image | Classic UI, img2img source |

Kinds: `image`, `audio` (Voice clips and Music songs), `video`.
