# cb — Claude Browser

A browser harness built for **LLM agents** (Claude Code first, any shell-driving agent works).
It drives Chrome, Edge, Brave or Chromium over CDP on **Windows, macOS and Linux**, and is designed
around how a model actually works: read a compact text view of the page, act on a numbered element,
read what changed.

```
$ cb goto localhost:8765
loaded http://localhost:8765/
Page: CB Test Home
URL: http://localhost:8765/
main:
  # Welcome to the test site
  form "Sign up":
    [5] textbox "Email address" type=email (required)
    [8] checkbox "I agree to the terms" (unchecked)
    [11] select "Country" = "Choose…" options: Choose… | United States | Canada | Mexico
    [13] button "Sign up"

$ cb fill 5 alex@example.com
filled [5] textbox "Email address" = 'alex@example.com'

$ cb click "Sign up"
clicked [13] button "Sign up"
changes (1 added, 0 removed):
+   Submitted: alex@example.com
```

## Why it's fast and easy for a model

- **Every action reports its own effect.** After a click/fill/press, cb waits for the page to
  settle (navigation, in-flight XHR/fetch, DOM mutations) and prints only the outline lines that
  changed — or the full new page after a navigation. Dialogs, new tabs, downloads, console errors
  and failed API calls are reported inline. No "snap again to see what happened" round trips.
- **Text-first outline, not screenshots.** `cb snap` renders headings, text and every
  interactive element as `[ref] role "name" = "value" (states)` with landmarks (`nav:`, `form:`,
  `dialog (modal):`) for structure. Visually hidden junk is dropped; big pages are trimmed to a
  token budget (off-screen text first). Screenshots are there when you need eyes (`cb shot -a`).
- **Stable refs.** An element keeps its number across snapshots and DOM updates until the page
  navigates, and refs are never reused, so an old ref fails loudly instead of clicking the wrong thing.
- **Anything is a target.** `12`, `[12]`, `"Sign in"` (visible text / label / placeholder /
  accessible name), CSS, `xpath=`, `role=button[name="Save"]`, or `340,120` coordinates.
- **Smart actions.** `fill` detects text fields, `<select>`s, checkboxes, file inputs, custom
  dropdowns and contenteditable editors. `check` works on hidden-input/label and ARIA switches.
  `upload` handles hidden file inputs and buttons that open a file chooser.
- **Shadow DOM and iframes just work.** Open shadow roots are walked; iframes are inlined in the
  outline and their refs are used like any other.
- **Persistent daemon.** The first command starts a background daemon that keeps one CDP connection
  (plus console/network/dialog capture). Commands cost ~60–100 ms instead of ~1–1.5 s.
- **Batching.** `cb batch` runs many commands in one tool call and prints a single change report.
- **Actionable errors.** "click blocked: `<div class="cookie-banner">` is covering the element",
  "element is disabled", "ref [12] is not on the page anymore (it came from …)". Blocked and disabled
  clicks fail in ~1.5 s instead of sitting out a long timeout.
- **Safe by default.** cb launches its own browser instance with its own profile, so it never kills
  or takes over your everyday browser. Logins you make in it persist between sessions.
- **Parallel agents.** `CB_SESSION=name` gives each agent its own browser, daemon and state.

Measured on the bundled test site (headless Chromium): filling and submitting a form took **6.3 s
and 6 tool calls** with cb 1.x; with cb 2 it is **~1.1 s in 4 calls, or ~1.0 s in 1 `batch` call**,
and a plain `cb snap` is **~0.1 s** (was ~1.4 s).

## Install

Python 3.10+ and the `playwright` package. You do **not** need `playwright install` if Chrome, Edge,
Brave or Chromium is installed — cb drives the real browser.

```bash
git clone https://github.com/asystemoffields/cb && cd cb
python3 -m venv .venv
./.venv/bin/pip install playwright          # optional: pyyaml (YAML batch files)
./cb status                                 # put the folder on PATH, or call ./cb
```

Or install the command globally: `pip install .` (or `pipx install .`) provides `cb`.

Windows: use `cb.bat` (cmd) or `cb.ps1` (PowerShell); both prefer `.venv\Scripts\python.exe`.

No browser installed? `./.venv/bin/playwright install chromium` and cb will find it.

### Use it from Claude Code

Copy the skill so Claude knows the tool exists and how to drive it:

```bash
mkdir -p ~/.claude/skills && cp -r skills/browser ~/.claude/skills/
```

(`skills/browser/SKILL.md` is a one-page guide; `cb help` is the full cheat sheet.)

## The loop

```bash
cb goto news.ycombinator.com      # navigate; prints the outline
cb click "past"                   # act; prints the new page (it navigated)
cb fill 12 "query" --submit       # prints only what changed
cb read                           # page text as markdown, when you need to read content
cb shot -a                        # annotated screenshot when layout matters (path printed)
```

What an action prints:

| Situation | Output |
| --- | --- |
| Page changed in place | `changes (N added, M removed):` then `+`/`-` outline lines |
| Navigated | `-> URL` and the new page's outline |
| Nothing visible changed | `(no visible change)` — a strong hint the action didn't work |
| Side effects | `dialog (confirm) 'Sure?' -> accepted`, `new tab opened: …`, `downloaded: …`, `console error: …`, `request failed: POST /api/save -> 500` |

`--no-snap` skips the report; `CB_AUTOSNAP=off` disables it globally.

### Outline format

```
nav "Primary":                      container (nav, main, header, footer, aside, form, dialog, …)
  - [3] link "Docs"                 list item with an interactive element
# Pricing                           heading (# = h1, ## = h2 …)
Plans start at $10.                 text
[7] textbox "Email" type=email = "a@b.c" placeholder="you@x.com" (required, invalid)
[9] select "Country" = "Canada" options: Canada | Mexico | …(+190)
[11] checkbox "Remember me" (checked)
[12] button "Menu" (collapsed)
[14] clickable "Settings"           a div/span with a pointer cursor or click handler
[15] label "Upload resume" (for file)   label standing in for a hidden input
[20] iframe "Payment":              iframe; its content is indented below it
dialog "Confirm order" (modal):
Note: div#overlay covers most of the page and will intercept clicks
```

Options: `-i` interactive only, `-v` viewport only, `--in <target>` one subtree, `--forms` form
controls only, `--urls` link targets, `--shot` annotated screenshot too, `--max N` / `--all` budget,
`--diff` changes since the last snapshot.

## Commands

`cb help` prints the cheat sheet; `cb help <command>` prints details. `T` = any target.

| Group | Commands |
| --- | --- |
| Look | `snap`, `read [T] [--links]`, `find <text>`, `shot [T] [-a] [--full]`, `links [filter]`, `html [T]`, `ax [T]`, `eval <js> [--on T]` |
| Act | `click T [--double\|--right\|--force\|--js]`, `fill T <value> [--submit]`, `type <text> [--into T]`, `press <key>…`, `select T <option>`, `check T`, `uncheck T`, `upload T <file>…`, `hover T`, `focus T`, `clear T`, `scroll [down\|up\|top\|bottom\|T] [--in T]`, `drag T T`, `fill_form <json>`, `act "<sentence>"` |
| Navigate | `goto <url>`, `back`, `forward`, `reload` |
| Wait | `wait T`, `wait 2s`, `wait --text "…"`, `wait --gone T`, `wait --url /path`, `wait --idle`, `wait --js "expr"` |
| Tabs & frames | `tabs`, `tab <n\|text>`, `tab new [url]`, `tab close [n]`, `frames`, `frame T\|top` |
| Debug & data | `console [--errors]`, `network [filter] [--failed] [--body ID]`, `dialog accept [text]\|dismiss`, `downloads`, `cookies`, `cookies_save/cookies_load <file>`, `storage [key]`, `viewport W H`, `pdf [file]` |
| Session | `status`, `launch`, `close`, `kill`, `daemon status\|stop\|restart\|log` |
| Scripting | `batch <file\|->`, `batch "cmd" "cmd" …`, `help [cmd]` |

Notes:

- `goto` adds the scheme for you: `example.com` → https, `localhost:3000` → http, `./page.html` → file.
- `press` keys: `Enter`, `Tab`, `Escape`, `ArrowDown`, `Control+A`, `Mod+A` (Ctrl, or Cmd on macOS),
  sequences (`cb press Tab Tab Enter`), or on a target (`cb press 7 Enter`).
- Coordinates are viewport CSS pixels — exactly the pixels of a `cb shot` image.
- Dialogs are auto-accepted and reported. Run `cb dialog dismiss` (or `accept "text"` for prompts)
  *before* the action that opens one.
- `network --body ID` prints a captured XHR/fetch response — handy for scraping JSON APIs.

## Batch

One command per line (a leading `cb` is optional; `#` comments):

```bash
cb batch <<'EOF'
goto example.com/login
fill Email alex@example.com
fill Password hunter2
click "Sign in"
EOF
```

or inline: `cb batch "fill 4 alex" "fill 5 secret" "click 6"`. Each step prints one line; the
final page state is reported once. Stops at the first error (`--continue` to keep going). The 1.x
YAML/JSON format (`- goto: url`, `- fill_form: {…}`) still works.

## Browser, profile and sessions

- Any command auto-launches a browser if none is listening (`CB_AUTO_LAUNCH=0` to disable).
  Preference: Brave → Chrome → Edge → Chromium (including Playwright's download); override with
  `cb launch chrome`, `CB_BROWSER=edge`, or `CB_BROWSER_PATH=/path/to/browser`.
- cb uses its own profile (`~/.cb/profiles/<browser>`), so it runs alongside your normal browser.
  Log in once in the cb window and the session sticks. `cb launch --profile DIR` picks another
  profile. `--profile default` (your everyday profile) requires that browser to be fully closed, and
  Chrome 136+ refuses remote debugging on the default profile altogether.
- Headless by default on Linux without a display; headed elsewhere (`--headless` / `--headed`,
  `CB_HEADLESS=1/0`).
- An already-running browser with `--remote-debugging-port=9333` is attached to as-is.
- `CB_SESSION=agent2 cb …` (or `cb --session agent2 …`) runs a fully separate browser, profile,
  daemon and state — use one per parallel agent.

### Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `CB_SESSION` | `default` | Isolated browser + state per name |
| `CB_CDP_PORT` | `9333` (default session) | DevTools port |
| `CB_BROWSER` / `CB_BROWSER_PATH` | auto | Which browser to launch |
| `CB_BROWSER_ARGS` | | Extra browser flags for launches (also `cb launch -- <flags>`) |
| `CB_HEADLESS` | auto | `1` headless, `0` headed |
| `CB_USER_DATA_DIR` | `~/.cb/profiles/<browser>` | Profile directory |
| `CB_TIMEOUT_MS` | `5000` | Per-action timeout |
| `CB_SETTLE_MS` | `3000` | Max wait for the page to settle after an action (`0` = don't wait) |
| `CB_SNAP_MAX` | `12000` | Outline budget in characters |
| `CB_AUTOSNAP` | `diff` | `off` to stop printing changes after actions |
| `CB_DIALOG` | `accept` | Default answer to alert/confirm/prompt |
| `CB_DAEMON` | `1` | `0` runs every command in-process (slower; no console/network history) |
| `CB_DAEMON_IDLE` | `3600` | Daemon exits after this many idle seconds |
| `CB_STATE_DIR` | `~/.cb` | State, profiles, screenshots, downloads, logs |
| `CB_SHOT_DIR` / `CB_DOWNLOAD_DIR` | under `CB_STATE_DIR` | Where screenshots / downloads go |
| `CB_TAB_URL` | | Prefer the tab whose URL contains this |
| `CB_DEBUG` | | Print tracebacks for unexpected errors |

## Recipes

**A click "did nothing".** The output says `(no visible change)`. Check `cb snap` for a
`Note: … covers most of the page` line or a `(disabled)` state, `cb console --errors`, and
`cb network --failed`. `cb shot -a` shows the layout. `--force` skips actionability checks;
`--js` dispatches `element.click()` directly.

**Content that loads later.** Actions already wait for network and DOM to settle. For slower
things: `cb wait "Order confirmed"`, `cb wait --gone "Loading"`, `cb wait --url /done`.

**Huge pages.** `cb find "invoice"` lists matching elements with refs; `cb snap --in 40` outlines one
region; `cb snap -v` shows only the viewport; `cb read` gets the text.

**Infinite scroll.** `cb scroll bottom` — the new items appear in the change report.

**Scraping an API-backed page.** `cb network api` lists XHR/fetch calls; `cb network --body 17`
prints a JSON response.

**Canvas / maps / PDFs.** `cb shot` and click by pixel: `cb click 412,230`.

**Keep a login across machines.** `cb cookies_save ~/site.json`, then `cb cookies_load ~/site.json`.

## Upgrading from 1.x

- `cb launch` no longer kills your running browser; it starts a separate instance with its own
  profile (log in once). `cb kill brave` only kills the cb-launched browser; add `--all` for the
  old kill-everything behavior.
- `snap` prints a text outline (the annotated screenshot is `snap --shot` or `shot -a`). Refs are
  now stable numbers that never repeat; `#7` / `7` still work.
- Screenshots go to `~/.cb/shots` instead of your home folder.
- `act`, `find`, `fill_form`, `batch` (YAML/JSON), `wait_*`, `coord_click`, `keys`, `dblclick`,
  `new_tab`, `close_tab`, `frame_reset`, `connect`/`title` all still work.
- Fixed: `--flag value` options (e.g. `goto --wait load`) were silently ignored or crashed; `ax`
  broke on Playwright ≥ 1.57; refs could resolve to the wrong element (`nth-of-type` selectors);
  `tab N` didn't stick; `act "select … with …"` clicked instead of selecting.

## Development

```bash
./.venv/bin/pip install playwright pyyaml pytest
./.venv/bin/python -m pytest tests -q        # ~40 end-to-end tests against headless Chromium
```

`tests/site/` is the fixture site and `tests/server.py` serves it (`python tests/server.py 8765`).
See `CLAUDE.md` for the architecture. A GitHub Actions workflow is in
`contrib/github-actions-test.yml` (copy it to `.github/workflows/` to enable CI).

## Limitations

- Chromium-family browsers only (CDP). No Firefox/Safari.
- Closed shadow roots and cross-origin iframes nested more than 4 deep aren't outlined.
- No CAPTCHA solving or bot-detection evasion. When a site challenges you, hand the window to the
  human (`cb launch --headed`).
