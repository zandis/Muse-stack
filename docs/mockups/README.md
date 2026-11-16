# Wardmuse mock-up

A clickable mock-up of the Wardmuse app on phone and desktop, in Traditional Chinese and English, built from `docs/spec/02-ux.md`. It is one self-contained file (`index.html`, about 220 KB): inline CSS and JavaScript, system fonts, inline SVG, no network requests, no storage, no clipboard access. Nothing is behind it.

## Open it

Double-click `docs/mockups/index.html`. It works offline in any current browser, including a phone browser (up to 520 px wide the phone frame fills the screen).

The strip above the app is not part of Wardmuse. It holds:

- **Phone / Desktop** (shown on windows 800 px or wider) and **繁體中文 / English**.
- **Demo state** (see below).
- **Skip to the app**, which loads the full sample day, and **Replay first run**. The page starts in the first-run walk-through; it can also be replayed from Settings → About.

Light and dark follow the system setting. Query options for linking to a screen: `?lang=en`, `?view=desktop`, `?demo=app` or any demo state name, `&nav=today|ask|chores|records|more|services|permissions|devices|wallet|apps|settings`.

## The demo-state switch

The switch reloads the sample day in a given state and keeps you on the screen you were looking at.

| State (`?demo=`) | What it shows |
|---|---|
| A normal day (`normal`) | Gauge at about 72%, two cards waiting, four services, six permissions, four devices |
| All used up (`used`) | Gauge at zero, "Refills at 16:00", the three choices on Today and for every new question in Ask |
| Paused (`paused`) | The "Everything is paused" bar on every screen (same as pressing Pause) |
| All seven cards (`cards`) | One card of each kind in section 5 under "Needs you" |
| Nothing connected (`empty`) | Empty states for Today, Services, Records, Chores, Permissions; Wallet hidden |
| Loading (`loading`) | "Updating…" over the last known content on Today, Services, Records |
| Something is wrong (`error`) | Service busy and key refused (Today, Services), record 1,204 does not match (Records), a chore that failed twice (Chores) |
| Offline (`offline`) | Offline bar; a question in Ask is queued |
| Always-on helper unreachable (`unreachable`) | "Last seen 14:02" with Retry and "Use this device for now"; the permission mark that says why it is not in force |
| Phone only (`nohome`) | The "Chores need a computer or an always-on helper" note (phone frame) and the three-step "Add an always-on helper" flow in Devices |
| Private records locked (`locked`) | "Unlock to see your private records" in Records |

Three more states are reached by using the app: lower the daily limit to NT$1 in Settings and pick "Spend about NT$1.3" to get the over-the-limit card; untick every app under Services → Your subscriptions, or stop the spending permission, to see the three choices shrink to the real ones; finish the first run to get a fresh account (one service, no wallet, the "connect Gemini" proposal and the Gemini guide from section 7.1).

## What it covers

- **Navigation (2).** Phone: five tabs with Pause in the top bar; More holds Services, Permissions, Devices, Wallet, Settings. Desktop: the sidebar in the specified order, "For apps", and a menu-bar button that opens the small panel (gauge, spending, first waiting card).
- **First run (3).** Welcome, connect OpenRouter on a sample approval page, try it, the answer with one grey line, the recovery key (24 placeholder words, Print, Save as file, "I'll do it later" with a reminder on Today, re-enter three words).
- **Screens (4).** Today, Ask, Chores, Records, Services, Permissions, Devices, Wallet with a Credits tab, For apps (desktop only), Settings, with the phone and desktop layouts described in each subsection.
- **Cards (5).** One shared component for save a file, draft a message, spend, grant a permission, one-off trial credit, send something private, install a skill. Approve and "Not this time" update Today, Records, spending, Permissions and Declined. "Always allow this" turns a card into a standing permission.
- **Key flows (7).** Guided key with one step at a time, the "Use the key you just copied?" prompt and the "This doesn't look right" fallback; the three choices; stopping a permission with the marks turning off one by one and a new line in Records; losing a device.
- **States (8).** Through the demo-state switch above.
- **Wording (1).** Main screens use only the plain words in the table; model names, tokens, US dollar amounts and fingerprints sit behind "Details". The three badges are On this device, Private, Standard. The one refill time is 16:00.

## What is simulated

Everything. The clock is fixed at Friday 13 November, 14:05. All names, files, amounts, keys, codes and services' pages are made up, and every answer is pre-written.

- Service pages, Oracle's page and the OpenRouter approval page are drawn samples inside the frame; nothing opens.
- The clipboard is never read. "Use the key you just copied?" appears when you press Copy on the sample page.
- Passkey, fingerprint and unlock prompts are a sheet with "Continue (demo)".
- Print, Save as file, Export, Copy and "Open in my ChatGPT" show a short note instead of acting.
- The QR code is a random pattern and cannot be scanned; the other device is played by a "(Demo) The other device scanned it" button.
- "Delete everything" only returns to the first-run screens. Nothing is stored, so a reload starts again.
- The sentence parser for chores and permissions is a few keyword rules (Monday, month, night; bills or mail trigger the "permission first" card).

## Not shown yet

From `docs/spec/02-ux.md`:

- **Section 6, notifications:** no push, system, tray-dot or LINE notifications, and no daily-summary setting.
- **Section 7.4, approving from the lock screen.**
- **Section 3, device lock:** the biometrics or PIN prompt the first time something private is stored; the optional vault PIN.
- **Section 5, card expiry:** cards say when they expire but never do; only one sample "Not approved" record stands for an expired card.
- **Section 4.2:** real attachments (the button attaches a pretend file); conversations cannot be started or deleted.
- **Section 4.3:** a chore's history is a short fixed list.
- **Section 4.4:** the export file and the way to check it.
- **Section 4.5:** on desktop the service page is drawn inside the app window rather than in the system browser with a separate side window.
- **Section 4.7:** the always-on helper's setup page as seen from its side; "Rebuild from backup" only confirms.
- **Section 4.10:** "make a new recovery key" shows the same sample words.
- **Section 9, measures:** no timing is collected.
- Japanese and Korean.

## Checking it

The mock-up was clicked through with Playwright in a 390×844 and a 1280×800 window, in both languages, light and dark: every screen, the first run, the guided key, cards, the three choices, pause, pairing, a lost device and every demo state, asserting no console errors, no requests other than the file, and no horizontal overflow. The script is not kept in the repository.
