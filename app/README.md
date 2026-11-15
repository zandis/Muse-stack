# Muse app

The real app's front end, and the desktop shell around it. This build is work packages **B1** (app shell), **B3** (Today and the tray panel) and **B7** (Records and Permissions) from `docs/spec/04-execution-plan.md`, with the navigation skeleton for the rest. The specification is `docs/spec/02-ux.md`; the look is the approved mock-up, `docs/mockups/index.html`.

## Run it against the fake home

You need Node 22, pnpm 10, and a Python that can run this repository (`pip install -e .` in a virtualenv). For the Records and Permissions screens the fake home also needs the signature and permissions libraries in that virtualenv (`pip install -e '.[records,permissions]'` from the repository root); without it the home answers `GET /v1/mandates` with 503 and the screen says it cannot show permissions. The end-to-end tests expect it.

```bash
cd app
pnpm install --frozen-lockfile
MUSE_PYTHON=/path/to/venv/bin/python pnpm dev      # fake home + Vite on http://127.0.0.1:5173
MUSE_FAKE_STATE=cards pnpm dev                     # normal | used | paused | cards | empty | error | mismatch
```

`pnpm dev` starts `python -m muse.home --fake` on a free port and the Vite dev server with its proxy pointed at it. `MUSE_PYTHON` defaults to `python3`.

Against a real home started with `muse home serve --tcp 4181`:

```bash
MUSE_HOME_PORT=4181 MUSE_HOME_TOKEN="$(cat ~/.muse/app.token)" pnpm exec vite
```

| Script | What it does |
|---|---|
| `pnpm dev` | Fake home plus the Vite dev server |
| `pnpm build` | Production build into `dist/` |
| `pnpm preview` | Serves `dist/` with the same proxy (`MUSE_HOME_PORT`, `MUSE_HOME_TOKEN`) |
| `pnpm typecheck` | `tsc --noEmit`, strict |
| `pnpm lint` | ESLint |
| `pnpm test` | Unit tests (Vitest, jsdom) |
| `pnpm e2e` | End-to-end tests (Playwright, Chromium): builds, then starts a fake home and `vite preview` per state |
| `pnpm tauri dev` / `pnpm tauri build` | The desktop app (needs the Tauri system libraries, below) |

## How it is put together

- **Vite, React, TypeScript (strict).** Three runtime dependencies: `react`, `react-dom`, `@tauri-apps/api`. No UI kit, no router library, no state library, no analytics, no remote fonts, no CDN.
- **State: React context plus a reducer** (`src/state/reducer.ts`, `store.tsx`). The app's state is one small object changed by a dozen actions; a reducer keeps every change in one pure, tested function, and context is already in React, so a store library would add a dependency without removing any code.
- **Routing:** `src/router.tsx`, about 80 lines over the History API. One path per screen: `/today /ask /chores /records /more /services /permissions /devices /wallet /apps /settings`, and `/tray`.
- **Design tokens:** `src/styles.css` is ported from the mock-up's `<style>` block: the same colour variables (light and dark), radii, shadows, font stack and component classes (`.card`, `.cardx`, `.btn`, `.gauge`, `.tabbar`, `.sidebar`, …). Changed on purpose: font sizes are in `rem` so text follows the system text size; the layout switches on the real window width (800 px) instead of a demo frame. Icons are the mock-up's paths as React elements.
- **Strings:** `src/i18n/en.ts` and `zh-TW.ts`, typed so a missing key does not compile. Components hold no text: `tests/source-rules.test.ts` parses every `.tsx` file and fails on a bare user-facing string. Language follows the system (`navigator.languages`), with a switch in Settings. Dates, times, numbers and money go through `Intl` (`src/i18n/format.ts`); times are in the person's time zone.

### The home client and its two transports

`src/home/` is written by hand from `docs/spec/interfaces/home-api.v1.yaml` and `card.schema.json`: `types.ts` (status snapshot, cards, jobs, apps, errors), `client.ts` (one typed client), and two transports behind one interface.

| Transport | Used | How the token is kept out of the page |
|---|---|---|
| `http` (`http.ts`) | Browser: development and tests | The page sends same-origin `/v1/...` requests. The Vite server (`home-proxy.ts`) forwards them to the home on `127.0.0.1` and adds `Authorization: Bearer …` from the `MUSE_HOME_TOKEN` environment variable. The proxy refuses requests that come from another site. |
| `tauri` (`tauri.ts`) | Desktop app | The page calls `invoke('home_request', {method, path, body})`. Rust talks to the home's Unix socket and adds the token it reads from a private file. The page gets `{status, body}`. |

The home sends no CORS headers, so a browser page cannot call it directly; that is why the proxy exists. It is a development tool, not a way to ship the app on the web.

Errors become the UX §8 pattern (what happened, what Muse will do, one button): the home cannot be reached (network failure, `503 unavailable`, or a gateway error), this device is refused (`401`/`403`), or anything else. The server's own message text is never shown; the app uses its translated sentences.

### Today

- **Gauge.** Worked out in `src/state/gauge.ts`, where the rule is written out. In short: a service counts if it is connected, in the cloud, and has an allowance that refills daily; what it has left is its tightest daily limit; services are weighted by how many different jobs they can do (the number of router aliases they serve); the gauge is the weighted average. It says "about" when any figure used is an estimate. Next refill is the earliest refill among services that are not full.
- **Spending.** The home reports US dollars only. The app shows NT$ converted at a fixed, stated rate (`TWD_PER_USD = 32` in `format.ts`), marks converted spending "about", and shows the US dollar figures and the rate behind Details.
- **Needs you.** `GET /v1/cards?status=pending`, newest first, drawn by the one `Card` component for all seven kinds. Approve / Not this time call `POST /v1/cards/{id}/answer`. The card leaves at once; if the answer fails it comes back with an explanation; if it was answered elsewhere or ran out of time, the app says so. "Always allow this" (save a file, message) asks once more in a dialog and sends `always: true`.
- **Running / Done today.** From `GET /v1/jobs`.
- **Tip.** The first entry of `suggestions` in the status snapshot; one a day, dismissible, remembered in `localStorage`.
- **States.** Nothing connected, all used up, paused, home unreachable (with "Last seen"), loading ("Updating…" over the last known content), error.
- **Polling.** `src/state/poller.ts`: every 5 seconds, doubling after each failure up to 60 seconds (and honouring `retry_after`), nothing while the window is hidden, at once when it is shown again. No websockets.

### Security

- Content-Security-Policy: `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; …`. No inline script, no `eval`. Set as a `<meta>` tag at build time and as a header by `vite preview`; inside Tauri it comes from `tauri.conf.json` (which also allows Tauri's own message channel).
- All text from the home is rendered as text. Tests fail on `dangerouslySetInnerHTML`, `innerHTML` and similar.
- Every string from the home passes `redact()` (`src/lib/redact.ts`), which hides anything shaped like a key or token.
- Outside links open only through `src/lib/links.ts` (https, three listed hosts); the Rust side checks the same list again.
- No secret is in the bundle: an end-to-end test scans `dist/` for the fake token and for `sk-…`.
- What is stored on the device: the language choice, when the home was last seen, and which tip was shown today.

### Records (`/records`, UX 4.4)

- **Data.** `GET /v1/receipts` (pages of 100, "Show older records" for the next), `?mandate=<permission id>` when opened from a permission (`/records?permission=…`). Each record is the signed record of `record-format.md`; the app writes the sentence itself from its facts (`src/screens/trust-words.ts`): who (`actor`, and the permission's holder), what (the action name, mapped to a plain verb; an unknown one says so), under which permission (the permission's own sentence from `GET /v1/mandates`), what it cost (`cost` in minor units, shown in the person's currency at the stated rate with the US-dollar figure behind Details), and where the content went (`egress`, with the service's privacy badge from `GET /v1/services`). Action names, ids and signatures appear only behind Details. What the home wrote (`summary`) is shown in the record; a redacted record says the text was removed on purpose and still checks out.
- **Proof.** Every `GET /v1/receipts` makes the home check the whole chain (it keeps the answer until the file changes). The top card says "All N records check out" or "Record N does not match the ones around it" with why (from `verify.kind`); each record says whether it is sealed, not sealed yet, the one that does not match, or cannot be confirmed because an earlier one does not. "Check my records" asks again. Record numbers shown are `seq + 1`.
- **Filters.** Chips (left my devices, cost money, changed something, by a helper or program), Who, When. They filter what is loaded. There is no "By chore" chip: records do not name the chore that made them.
- **Export.** `POST /v1/receipts/export` (new; owner only): the home seals, builds the complete export of `record-format.md` §8, checks it, and the app saves it as a file. Option to leave out what each record says. Refused, with a plain sentence, when the records do not check out.
- **Not done:** Undo (the home has no undo call), the "Vault locked" state (no vault yet), and a device filter (the home's records are one device's chain).
- **Layout.** Phone: lines grouped by day, a record opens in a sheet. Desktop: a table with the same columns and a pane for the selected record.

### Permissions (`/permissions`, UX 4.6)

- **Data.** `GET /v1/mandates?lang=` (each permission with its facts: actions, resources, limits, privacy, and the compiler's per-place state), `GET /v1/mandates/declined?lang=`, the waiting `grant_permission` cards from the store, and `GET /v1/receipts?limit=200` for "Used N times" and when it was stopped.
- **The sentence** is written by the app ("Your helper may spend up to NT$30 a day on 3 services, until 30 Nov."); when a permission allows something the app has no words for, the home's own sentence is shown. The home's lines are behind "In the home's own words".
- **Marks.** One per place the compiler made something (helper tasks, using your allowances, records; spending limit, mail and calendar, health are grey "not built yet", never green). A mark that is not green says why.
- **Stop.** Stop → "Stop this permission?" → Stop (two taps). The dialog then shows "Permission withdrawn" and each place turning off one at a time (450 ms apart; at once with reduced motion) from the home's answer to `POST /v1/mandates/{name}/stop`. A place that could not be turned off is shown as failed, with what Muse does by itself and Try again. A place that was never built is never called "turned off". Focus returns to Stop on Cancel, and to the stopped permission (now under "Stopped or ended") after a stop.
- **Adding a permission.** Not in this build. UX 4.6 describes "Add" (type what to allow, approve with a passkey), but the home has no call that turns a sentence into a draft and no passkey check yet (`person_confirmed` is the owner role). Permissions arise from cards (section 5) and from For apps; the screen says so.
- **Layout.** Phone: cards in one column. Desktop: cards on the left, the selected permission's history on the right.

## The desktop shell (Tauri v2)

```
src-tauri/
  tauri.conf.json          two windows (main, and the hidden tray panel at /tray), the content policy
  capabilities/default.json what the two windows may ask Rust to do
  build.rs                 lists the commands so each needs a permission
  src/lib.rs               commands, the tray icon and menu, the panel's placement
  crates/home-link/        HTTP/1.1 over the home's Unix socket, the token file, the link allow-list
```

- `home_request` → `muse-home-link`: a small hand-written HTTP/1.1 client over `UnixStream` (`MUSE_HOME/home.sock`; `127.0.0.1:4181` on Windows). Its only dependencies are `serde` and `serde_json`. It refuses any method or path the API does not define, so nothing can be smuggled into the request.
- **The token** is read from `MUSE_HOME/app.token`, which must be a plain file with mode 0600 (not a link, not readable by others). To create it: `muse home token add desktop-app --show-token > ~/.muse/app.token && chmod 600 ~/.muse/app.token`. Pairing from inside the app, and the OS keystore, come with later packages (B2, B10, C2).
- **Tray:** an icon with a menu (Open Muse, Small panel, Quit). A left click opens the panel beside the icon; on Linux, where tray icons send no click events, the menu opens instead. The panel hides when it loses focus.
- For development against the fake home: `MUSE_HOME_TCP=<port>` and `MUSE_HOME_TOKEN_FILE=<file>`.

System libraries on Ubuntu 24.04: `libwebkit2gtk-4.1-dev libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev build-essential`.

### What was built and run here, and what was not

Done on Ubuntu 24.04 (x86-64) with WebKitGTK 2.52:

- `cargo test --workspace` in `src-tauri/` (22 tests: 18 in `muse-home-link`, 4 in the shell).
- `pnpm tauri build --debug --no-bundle` produced the binary.
- The binary was run on a virtual display (Xvfb) against the fake home, both over TCP and over the home's real Unix socket with the token from a 0600 file. Today rendered with live data; clicking Pause changed the home's state (checked by asking the home afterwards).

Not done:

- **The tray icon and its panel were never seen.** A virtual display has no tray. The code compiles and follows the Tauri v2 documentation; its placement arithmetic is unit-tested; nobody has clicked it.
- No installer was built (`--no-bundle`); nothing is signed. No macOS or Windows build. The Windows path (TCP on 127.0.0.1) is written and its request code is tested, but it has not run on Windows.
- "Open in my ChatGPT" was not exercised inside the desktop app.

## Tests

```bash
pnpm typecheck && pnpm lint && pnpm test && pnpm build
MUSE_PYTHON=/path/to/venv/bin/python pnpm e2e
(cd src-tauri && cargo test --workspace)
```

- **Unit (Vitest):** the client and both transports, the reducer, the gauge and spending, polling, the tip rule, formatting, both dictionaries (every key, same placeholders, only the plain words of UX §1), the card for each kind, Today in every state, navigation, Records and Permissions (`tests/trust.test.tsx`: the sentences, money, proof per record, filters, export, Stop one by one and a failed place, focus, both languages, with a fixed clock), and rules about the source (no bare strings, no HTML from strings, one `fetch`, no secrets).
- **End to end (Playwright):** Today in every fake-home state × phone (390×844) and desktop (1280×800) × zh-TW and English × light and dark, each with axe-core (no serious or critical violation), no console errors, no request to anything but the app, no sideways scrolling. Then loading, a home that goes away and comes back, approve / decline / always (checked on the fake home itself), rollback, pause and resume, every route, keyboard and focus, the language switch, the tray panel, time zones, text at 200%, reduced motion, and the security checks. `e2e/trust.spec.ts`: Records and Permissions in both layouts and languages (light and dark) with axe; filters, Check my records, export (the downloaded file is checked), the `mismatch` state, empty/error/paused, Stop with a place that fails once and Try again, the link from a permission to its records, keyboard and focus.
- **CI** (`.github/workflows/ci.yml`, job `app`): install from the lock file, types, lint, unit tests, build, and the Rust bridge tests. The end-to-end tests are not in CI yet: they need this repository's Python environment and a browser download on the runner. Run them locally.
- Screenshots: `MUSE_SHOTS_DIR=/some/folder pnpm e2e screenshots`.

Chromium for Playwright comes from `PLAYWRIGHT_BROWSERS_PATH` if set; otherwise run `pnpm exec playwright install chromium` once. `@playwright/test` is pinned to the version whose browser build is installed.

## Supply chain

Every dependency is pinned to an exact version in `package.json` and `src-tauri/Cargo.toml`; `pnpm-lock.yaml` and `src-tauri/Cargo.lock` are committed. No package may run an install script (`onlyBuiltDependencies: []` in `pnpm-workspace.yaml`).

## What is real and what is a placeholder

| Screen | State |
|---|---|
| Today | Real, all states. Spending in the person's currency from the home's `money` (fixed rate setting). "Connect your first service" leads to the first run |
| Ask (`/ask`) | Real: conversations, privacy badge, attach an allowed folder (the files skill, with sources as file › paragraph and the quoted words), the grey line with Details, "Open in my …", "Make this a chore", the three choices (wait, hand off, pay, and the over-limit second step), Continue for a cut-short answer, offline queue, private-with-no-private-service. Answers are Markdown rendered by `src/lib/markdown.tsx` (no HTML from strings, links through the opener, images never loaded). No streaming |
| Chores (`/chores`) | Real: sentences, add by one sentence with "what I understood" or its questions, run now, pause, edit, delete, history, spare-allowance label, no-home note. The sentences the home writes are in the chore's own language |
| Services (`/services`) | Real: connected services, test, disconnect, the add list, guided key (one step at a time, the service's page through the opener, key pasted into a password field and cleared at once), one tap, other allowances, subscriptions, restart note |
| Settings (`/settings`) | Real: daily limit in the person's currency, default privacy, language, currency and its fixed rate, memory (honest: nothing is remembered; "Forget everything" erases kept conversations), About with the supply-chain check. Recovery key and export say they come later (`features.recovery: false`) |
| First run (`/welcome`) | Real: welcome, one-tap OpenRouter with its two facts, try it, the answer with its grey line. The recovery-key step is hidden until the vault is wired in. Opens by itself from Today on a home with nothing connected whose `settings.first_run` is `pending`; finishing or "Skip for now" is written to the home (`PATCH /v1/settings {first_run}`), so it opens once. After a skip Today shows its empty state |
| For apps (`/apps`, desktop) | Real: the address with Copy, the programs, add a program from a preset; its key is copied to the clipboard on a tap and never shown |
| Records (`/records`) | Real: sentences grouped by day, filters, the record with its proof, Check my records, Export, by permission. No Undo, no vault lock |
| Permissions (`/permissions`) | Real: sentences, marks, limits, end dates, use counts linked to Records, Stop with each place turning off, waiting cards, stopped/ended, declined. No Add (see above) |
| Devices | Placeholder |
| Wallet | Placeholder with the test-network header, hidden from the menus |
| Tray panel | Real in the browser; the Tauri tray window is written, not seen |

Links outside the app: both builds open only https pages whose host is in `src/lib/link-hosts.json`, the catalogue's own sign-up, key and one-tap pages plus the three subscription sites. The file is written from `catalogue/catalogue.v1.json` by `scripts/export_link_hosts.py` (also run by `scripts/publish_catalogue.py`, and checked by its `--check`). The desktop shell compiles it into Rust (`src-tauri/crates/home-link`) and checks every link there, so the web view cannot add a host; a Rust test derives the list from the catalogue again and compares.
