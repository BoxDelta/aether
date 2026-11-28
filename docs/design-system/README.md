# Aether

Aether's interface: a calm, dark, organised place to talk to Aether and see exactly what it did. This system is taken from the approved UI mockups ([Aether UI designs](https://claude.ai/artifact/WUWchjEM4tgGMiLowzjq8R)) and is the source for Phase 3 Steps 3–12 (`docs/08_Phase_3_Build_Order.md`). Build it with Tailwind CSS and shadcn/ui (ADR-034): map these tokens into the Tailwind theme and shadcn's CSS variables; don't invent new colours or sizes per screen.

## Principles

- **Clean before clever.** One accent (`accent`), lots of `surface-0`, hairline `border`s. No gradients, glows or drop shadows. Animation is limited to state changes, and respects "reduce motion".
- **Organised.** Every screen is the same skeleton: sidebar (`sidebar-width`), a 56px header with the page title (`page-title`), then content. Lists on the left, detail on the right.
- **Honest.** Show only what really happened (ADR-007). Failed and cancelled work is shown plainly, never hidden. No model "thinking" shown as activity.
- **Machine data looks like machine data.** IDs, event types, model names, token counts and times use the `mono` / `mono-meta` styles.
- **Phone first.** Every screen works at 390px with one hand; tap targets are at least `touch-target`.

## Colour

Dark is the only theme for v1.

- **Surfaces** step up from `surface-0` (page) → `surface-1` (sidebar) → `surface-2` (cards, inputs) → `surface-3` (your chat bubbles). `surface-selected` marks the active item.
- **Text:** `text` for content, `text-secondary` for list items, `text-muted` for labels and helpers, `text-dim` only for small uppercase labels and reply metadata.
- **Accent** (`accent`) is for the one main action in a view (Send, Save, Approve once), focus rings and live dots. Text on it is `accent-ink`. Accent-tinted rows (memory proposals, topic links) use `accent-soft` with `accent-border` and `accent-text`.
- **Status colours mean one thing each:** `ok` = healthy/succeeded, `warn` = needs you (approvals, waiting, High risk, Sensitive), `danger` = failed or destructive. They differ in lightness as well as hue. Never use them decoratively.
- **Privacy badges:** Public = `surface-3`/`text-secondary`, Personal = `accent-soft`/`accent-text`, Sensitive = `warn-soft`/`warn-text` ("Sensitive · local only").

## Type

Inter for the interface, JetBrains Mono for machine data (both from Google Fonts, self-hosted in the build: no external CDNs, per the CSP rule in doc 08).

- `page-title` for headers, `card-title` for card headings, `chat` for chat messages, `body` for everything else, `small` for secondary rows, `label` (uppercase, +0.08em tracking) for section labels, `display` only for big numbers on Status.

## Layout and spacing

- Desktop: sidebar 232px + content. Page side padding `space-7`. The chat column and composer are centred at `chat-width`.
- Phone: no sidebar; a bottom tab bar (`phone-tabbar-height`) with five tabs: Chat, Memory, Activity, Status, Approvals. Topics open from links in the chat; Agents from Status.
- Cards: `surface-2`, `border`, `radius-lg`, padding `space-5`. Gaps between cards `space-4`.
- Controls: `control-height` on desktop, `touch-target` on the phone; `radius-md`.

## Navigation

Sidebar order: **Chat** (with **Topics** as its only sub-tab, shown only while Chat or Topics is open), Memory, Activity, Status, Agents, Approvals (with a `warn` count badge). A blue dot on Chat means a topic has something new. The bottom of the sidebar has the status card (services, today's tokens split cloud/local, GPU, cloud spend) and the user row.

## Component patterns (build with shadcn/ui)

- **Button:** primary = `accent` fill + `accent-ink` text; secondary = transparent with `border-strong`; ghost = text only; destructive = `danger` text with `danger-border` outline. One primary per view.
- **Input / textarea / select:** `surface-2`, `border-strong`, `radius-md`; every field has a real `<label>` (visually hidden is fine).
- **Nav item:** 40px tall, icon + label, `radius-md`; active = `surface-selected` + white text; sub-tab = 34px, indented under its parent with a `border` guide line.
- **Meta line:** under each Aether reply, one `mono-meta` line in `text-dim`: "Local · 2.4 s · 3 memories", with an info button for the routing reason.
- **Topic link chip:** pill, `accent-soft`/`accent-border`/`accent-text`: "New topic · Backup check job ↗", plus a text button "Use an existing one" or "Wrong place? Move".
- **Memory proposal row:** one line, `border`, Save (soft accent) and a dismiss ✕.
- **Approval card:** warm card (#15130F, border #4A3A1E), High badge, what/who/tool/why as a definition list, optional note, "Approve once" (primary) and "Deny" (destructive), expiry time top-right.
- **Event row:** grid of time (mono), type (mono, coloured by outcome), actor, summary, outcome word.
- **Status dot + label, stat tile, thin progress bar** (6px, `border` track, `accent`/`ok` fill).
- **Badges:** counts are pills 20px tall; risk and privacy badges use `radius-sm`.

## Accessibility

Text meets 4.5:1 on its surface (3:1 at 24px+). Focus is always visible (2px `accent` ring). Icon-only buttons have `aria-label`. Real buttons, links and inputs, never clickable divs.

## Not in this system yet

No logo (the sidebar uses a simple hexagon mark drawn inline; replace when there is one). No component bundle: Claude Code builds the components in `web/` with shadcn/ui from the patterns above.
