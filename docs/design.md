# AI Studio design language

The shell of AI Studio Hub and every studio inside it share one design language, so a studio looks the
same on its own and inside the hub. This page is the reference; `hub/web/styles.css` is the canonical
implementation of the tokens, and each studio implements the same tokens natively in its own stylesheet
(the hub only *syncs the theme* into a studio, it never restyles it).

The look is **palette v7**, shared with Forge classic and Forge Studio: warm neutral surfaces, hairline
borders, a **coral** accent (#E05A4E light / #FF7B6C dark) for the primary action and for "you are here"
states (active nav, focus ring, the studio that holds the GPU), and **teal** (#1C8C7E / #3CC2B0) for
secondary markers - section labels, on-switches, meters. Each studio keeps its small signature colour.

## Tokens

| Token | Light | Dark |
|---|---|---|
| `bg` (page) | `#F3F3F4` | `#0B0B0D` |
| `panel` (cards, top bar) | `#FFFFFF` | `#141416` |
| `panel-2` (inputs, secondary surfaces) | `#F7F7F8` | `#1B1B1E` |
| `panel-3` (hover, chips, code) | `#EEEEF0` | `#232327` |
| `line` (borders) | `#E4E4E7` | `rgba(255,255,255,.085)` |
| `line-2` (strong borders, scrollbars) | `#CFCFD4` | `rgba(255,255,255,.16)` |
| `text` | `#111113` | `#ECECEE` |
| `text-2` | `#3A3A40` | `#C4C4C9` |
| `muted` | `#71717A` | `#8A8A93` |
| `faint` | `#A1A1AA` | `#5C5C66` |
| `accent` (primary actions, links, focus) | `#17171A` (hover `#2A2A2E`) | `#F4F4F5` (hover `#FFFFFF`) |
| `accent-ink` (text on accent) | `#FFFFFF` | `#0B0B0D` |
| `accent-soft` (selected / focus ring) | `rgba(17,17,19,.08)` | `rgba(255,255,255,.10)` |
| `busy` (progress, "working") | `#4CC38A` | `#4CC38A` |
| `ok` | `#2EA043` | `#4ADE80` |
| `warn` / `warn-bg` | `#B7791F` / `#FBF1DC` | `#FBBF24` / `rgba(251,191,36,.12)` |
| `danger` / `danger-bg` | `#D93F3F` / `#FDE8E9` | `#F87171` / `rgba(248,113,113,.14)` |
| `shadow` (cards, popovers) | `0 10px 30px rgba(0,0,0,.08)` | `0 12px 32px rgba(0,0,0,.45)` |
| `shadow-2` (small) | `0 2px 6px rgba(0,0,0,.06)` | `0 2px 6px rgba(0,0,0,.35)` |
| `ink` (the shell's rail, brand black) | `#0B0B0D` | `#060607` |

The primary button gets no coloured shadow: `0 4px 14px rgba(0,0,0,.18)` in light, none in dark.
No blue, no purple, no gradients, no glows.

### Studio signature colours

Each studio has one signature colour, used only for its number, its mark in its own top bar, its
active-tab underline and its status dot. Primary buttons stay `accent` in every studio.

| Studio | Signature |
|---|---|
| 01 Image Studio | `#F4C15D` amber |
| 02 Voice Studio | `#6FCF97` mint |
| 03 Video Studio | `#F28B82` coral |
| 04 Music Studio | `#A78BFA` violet |
| 05 Forge Studio | `#E05A4E` red |

## Type

* **UI:** Inter (`"Inter", "Segoe UI Variable", "Segoe UI", system-ui, -apple-system, sans-serif`), base
  14.5px / 1.4, `-webkit-font-smoothing: antialiased`. Load it from Google Fonts with the system fallback
  (Forge bundles InterVariable and stays offline).
* **Headings:** 700, letter-spacing `.01em`; page/section titles 16–18px, top-bar name 17px.
* **Eyebrow labels** (section headers such as PROMPT, MODEL): 11.5px, uppercase, letter-spacing `.14em`,
  700, `muted`.
* **Display accent** (a hero or an empty-state title only): Fraunces 300, optional.
* **Mono:** `"Cascadia Code", Consolas, "JetBrains Mono", monospace` for seeds, sizes, logs.

## Shape and components

* **Cards / panels:** `panel` background, 1px `line` border, radius **14px**, `shadow-2`; popovers and
  dialogs radius 18px with `shadow`.
* **Buttons:** radius **9px**, 34px tall, 600 weight, 13.5px. Primary = `accent` background, `accent-ink`
  text (black-on-light, white-on-dark), hover to the hover colour. Secondary = `panel` background, 1px
  `line`, `text`. Ghost = transparent, 1px `line`, `text-2`. Danger = `danger-bg` background, `danger`
  text. Disabled = 50% opacity.
* **Inputs, selects, textareas:** `panel-2` background, 1px `line`, radius **8px**, 34px tall,
  focus = `line-2` border + 3px `accent-soft` ring. Range sliders: 4px track `line-2`, filled part
  `text`, 14px thumb in `panel` with a 2px `text` border.
* **Pills / badges:** radius 999, 12px, 600, `panel-3` background, 1px `line`; a 6px dot in `ok`, `busy`,
  `warn` or `danger` for state.
* **Tabs:** text `muted`, active `text` with a 2px underline in the studio's signature colour (the shell
  uses `text`).
* **Segmented controls / chips:** `panel-3` background, selected chip = `panel` with `shadow-2` (light)
  or `accent-soft` (dark).
* **Progress:** 6px track `panel-3`, fill `busy`, radius 999.
* **Scrollbars:** thin, thumb `line-2`, track transparent.
* **Top bar:** 60px, `panel`, bottom 1px `line`. Left: the studio's mark (34px rounded square in the
  signature colour with a black icon), name (17px 700) and tagline (12.5px `muted`). Right: status pill,
  GPU readout (mono), actions, theme toggle. Inside the hub the shell shows the studio's name, so the
  brand block is hidden there (`html[data-hub-theme] .brand { display: none }`).
* **Empty states:** centred, a 64px rounded icon tile in `panel-3`, a 17px title, `muted` copy.
* **Motion:** 150–250ms ease; nothing bounces.

## Theme mechanics

* Light is the default. `html[data-theme="dark"]` switches to dark; with no attribute the studio may
  follow `prefers-color-scheme`. The studio's own toggle writes `data-theme` and remembers it.
* The hub's bridge sets `data-theme` (and `data-hub-theme`, plus a `dark` class) on the studio's `<html>`
  and keeps it in step with the shell, so a studio only has to honour `data-theme`.
* `color-scheme` follows the theme so native controls and scrollbars match.
