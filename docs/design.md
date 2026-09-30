# AI Studio design language

The shell of AI Studio Hub and every studio inside it share one design language, so a studio looks the
same on its own and inside the hub. This page is the reference; `hub/web/styles.css` is the canonical
implementation of the tokens, and each studio implements the same tokens natively in its own stylesheet
(the hub only *syncs the theme* into a studio, it no longer restyles it).

## Tokens

| Token | Light | Dark |
|---|---|---|
| `bg` (page) | `#F4F6F9` | `#061027` |
| `panel` (cards, top bar) | `#FFFFFF` | `#0B1B3B` |
| `panel-2` (inputs, secondary surfaces) | `#F7F8FA` | `#10244D` |
| `panel-3` (hover, chips, code) | `#EEF1F5` | `#162D5C` |
| `line` (borders) | `#E5E5E4` | `rgba(255,255,255,.09)` |
| `line-2` (strong borders, scrollbars) | `#CFD5DE` | `rgba(255,255,255,.18)` |
| `text` | `#0B1F45` | `#F2F5FA` |
| `text-2` | `#2B3B5C` | `#C9D4E6` |
| `muted` | `#6B7A93` | `#8FA1BF` |
| `faint` | `#9AA7BB` | `#5F7194` |
| `accent` (primary actions, links, focus) | `#1C4991` (hover `#2C6BC4`) | `#3F6FD1` (hover `#5A88E6`) |
| `accent-ink` (text on accent) | `#FFFFFF` | `#FFFFFF` |
| `accent-soft` (selected / focus ring) | `rgba(28,73,145,.10)` | `rgba(63,111,209,.20)` |
| `busy` (progress, "working") | `#34C6A3` | `#34C6A3` |
| `ok` | `#11A311` | `#3ACB5A` |
| `warn` / `warn-bg` | `#C99400` / `#FFF4D6` | `#FFC23B` / `rgba(255,194,59,.12)` |
| `danger` / `danger-bg` | `#D6453D` / `#FBE5E3` | `#F07171` / `rgba(240,113,113,.14)` |
| `shadow` (cards, popovers) | `0 10px 30px rgba(7,25,58,.08)` | `0 10px 30px rgba(0,0,0,.35)` |
| `shadow-2` (small) | `0 2px 6px rgba(7,25,58,.06)` | `0 2px 6px rgba(0,0,0,.3)` |
| `ink` (brand navy, the shell's rail) | `#0B1F45` | `#0B1F45` |

Brand set: navy `#0B1F45`, blue `#1C4991`, teal `#34C6A3`, green `#11A311`, yellow `#FFC23B`, grey `#E5E5E4`.
No purple, no gradients, no glows. Colour is carried by the artwork; the UI stays calm.

### Studio signature colours

Each studio has one signature colour, used for its number, its mark in its own top bar, its active-tab
underline and its status dot. Primary buttons stay `accent` blue in every studio.

| Studio | Signature |
|---|---|
| 01 Image Studio | `#1C4991` |
| 02 Voice Studio | `#34C6A3` |
| 03 Video Studio | `#FFC23B` |
| 04 Music Studio | `#11A311` |
| 05 Forge Studio | `#D6453D` |

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
  text, `0 6px 18px rgba(28,73,145,.35)` shadow, hover lifts to the hover colour. Secondary = `panel`
  background, 1px `line`, `text`. Ghost = transparent, 1px `line`, `text-2`. Danger = `danger-bg`
  background, `danger` text. Disabled = 50% opacity.
* **Inputs, selects, textareas:** `panel-2` background, 1px `line`, radius **8px**, 34px tall,
  focus = `accent` border + 3px `accent-soft` ring. Range sliders: 4px track `line-2`, filled part
  `accent`, 14px white thumb with a 2px `accent` border.
* **Pills / badges:** radius 999, 12px, 600, `panel-3` background, 1px `line`; a 6px dot in `ok`, `busy`,
  `warn` or `danger` for state.
* **Tabs:** text `muted`, active `text` with a 2px underline in the studio's signature colour.
* **Segmented controls / chips:** `panel-3` background, selected chip = `panel` with `shadow-2` (light)
  or `accent-soft` (dark).
* **Progress:** 6px track `panel-3`, fill `busy` teal, radius 999.
* **Scrollbars:** thin, thumb `line-2`, track transparent.
* **Top bar:** 60px, `panel`, bottom 1px `line`. Left: the studio's mark (34px rounded square in the
  signature colour with a white icon), name (17px 700) and tagline (12.5px `muted`). Right: status pill,
  GPU readout (mono), actions, theme toggle. Inside the hub the shell shows the studio's name, so the
  brand block is hidden there (`html[data-hub-theme] .brand { display: none }`).
* **Empty states:** centred, a 64px rounded icon tile in `panel-3`, a 17px title, `muted` copy.
* **Motion:** 150–250ms ease; nothing bounces.

## Theme mechanics

* Light is the default. `html[data-theme="dark"]` switches to dark; with no attribute the studio may
  follow `prefers-color-scheme`. The studio's own toggle writes `data-theme` and remembers it.
* The hub's bridge sets `data-theme` (and `data-hub-theme`) on the studio's `<html>` and keeps it in
  step with the shell, so a studio only has to honour `data-theme`.
* `color-scheme` follows the theme so native controls and scrollbars match.
