# AI Trader — visual world

**Mode:** Persuade (landing). The app at `/app` is Operate and keeps its own look.

**Concept:** the device is the hero. The product's whole magic is one card arriving
on a phone and one tap answering it, so the page puts that object at the center and
lets everything else support it.

## Canvas
Near-black, slightly cool. The use scene is early morning or late evening on a
phone — dark is chosen from that scene, not from category habit.

| Token | Value | Use |
|---|---|---|
| `--ink` | `#08090B` | page |
| `--surface` | `#101317` | raised panels |
| `--surface-2` | `#171B21` | the card face |
| `--line` | `#242A33` | hairlines |
| `--text` | `#F2F4F7` | primary |
| `--text-2` | `#A7AFBB` | secondary |
| `--text-3` | `#7D8695` | meta only |
| `--accent` | `#C8F24C` | the single accent: actions, approval, emphasis |
| `--accent-ink` | `#0A0F02` | text on accent |
| `--down` | `#FF6B5E` | semantic only: a price moving down |

One accent. `--down` is data semantics, not a second brand color.

## Type
- **Archivo** (variable) — display and UI. Display tracking `-0.03em`, never past `-0.04em`.
- **JetBrains Mono** — tickers, prices, scores, counts. Measurement only, never as costume.

Display ceiling 5.5rem. Body measure 62–70ch.

## Elevation
Declare once. The phone and the floating notification use shadow with a real offset
and blur. Flat panels use a 1px hairline and no shadow. Never both on one element.

## Motion
One authored moment: the notification arrives, then the card resolves from blur.
Exponential ease-out from an already-visible default. Everything else is state
feedback only. Fully honored under `prefers-reduced-motion`.
