# Muse mock-up

A clickable, self-contained mock-up of the Muse consumer app, built from `docs/muse-plan.md` (sections 1, 3, 5, 6, 7) and `docs/plan-infra-first.md` section 6. One file, inline CSS and JS, no network requests.

## Open it

Open `docs/mockups/index.html` in any browser (double-click, works offline). The toolbar above the app switches Phone / Desktop view, 繁體中文 / English, and jumps to demo states. "Skip to the app" loads the full sample day; "Start over" restarts the first-minute flow. On real phone widths (up to 520 px) the app is full-bleed. Query options: `?lang=en`, `?view=desktop`, `?demo=app|used|offline|error|choice|empty`, `&tab=today|ask|chores|helpers|activity|settings`.

## What is fake

Everything. All numbers, files, helpers, answers and the clock (fixed at 13:20 and ticking) are hard-coded. Nothing is sent, saved or connected; provider pages and approval sheets are drawn samples. A "Mock-up: sample data" label is always visible. After the first-minute flow the app shows a day-7 sample so every screen has content.

## Screens and states

- First minute: welcome (language, what Muse is and is not), connect first helper (one-tap approval sheet with success; or "AI on this device" download), first question or chore. 4 taps to a first answer.
- Tabs: Today (one gauge, next refill countdown, chores / waiting / spent rows, pause), Ask (answers with one quiet line and a Details disclosure, confirm cards, three-choice moment, hand-off sheet and filing the answer back, "add Gemini" suggestion), Chores (one-sentence add, example chips, estimate-before-start sheet, "ran on leftover allowance"), Helpers (cards with bar, refill countdown, good-for, privacy badge, reported vs estimated; ranked Add list; guided-key flow; Other allowances; subscriptions as hand-off only), Activity (summary, pause switch, filters, undo), Settings (daily cap, default privacy, permissions with revoke, editable memory, language, export, about).
- States: empty chores, empty activity, used up until 15:00, offline, helper error with next step, paused banner on every tab, strong allowance used up.
- Desktop: sidebar layout, menu-bar popover, "Endpoint for other apps" page.
- Light and dark (system setting), reduced motion respected.
