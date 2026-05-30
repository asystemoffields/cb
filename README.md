# cb — Claude Browser

A Playwright-over-CDP harness designed for **Claude Code** to drive **any
Chromium-based browser** (Brave, Chrome, Edge) on **Windows, macOS, and Linux**. Optimized for the
way an LLM operator works: snap a page → reference elements by number → act,
with smart fallbacks for selectors and a single-CDP-session batch mode for
multi-step flows.

## Why this exists

LLM operators struggle with browsers because:

- **Selectors are brittle.** "Click the Submit button" is easy to say, hard to
  encode as CSS. So `cb act "click Submit"` tries six strategies in order
  (text, role, label, placeholder, fuzzy match against the last snap).
- **Visual context is hard to pass back.** `cb snap` produces an annotated
  screenshot with numbered overlay boxes on every interactive element, plus a
  registry. Subsequent commands accept `#7` as shorthand for "element 7 from
  the last snap."
- **One-shot CLIs are slow.** Each CDP attach costs ~300–500ms. The `batch`
  command runs a YAML/JSON script in one persistent connection.
- **React lies about its DOM.** Force-clicks, real input/change events, and
  digit-prefixed-ID fallbacks are baked in.

## Setup

Dependencies: `playwright`, `Pillow`, `PyYAML` (Python 3.10+). Connecting over
CDP does **not** require Playwright's bundled browsers, so `playwright install`
is unnecessary — you just need the `playwright` pip package and a real
Brave/Chrome/Edge install.

### Linux / macOS

```bash
# One-time: create an isolated venv for the harness and install deps.
# (On Debian/Ubuntu you may first need: sudo apt install python3-venv)
cd /path/to/cb
python3 -m venv .venv
./.venv/bin/pip install playwright Pillow PyYAML

# Then use the bash wrapper (auto-uses .venv if present). Put cb on PATH or
# call it by full path:
./cb launch                          # auto-detect Brave > Chrome > Edge
./cb launch chrome                   # force Chrome
CB_BROWSER=edge ./cb launch          # via env var
CB_BROWSER_PATH=/opt/something/chromium ./cb launch
```

Browser binaries are resolved against `$PATH` (e.g. `brave-browser`,
`google-chrome`, `chromium`) — or set `CB_BROWSER_PATH` to an absolute path.
Killing uses `pkill -x` on the exact process name.

### Windows

Already installed on this machine: Playwright 1.58, Pillow 12, PyYAML 6. Use the
`cb.bat` / `cb.ps1` wrapper:

```powershell
# From any directory, after putting the cb folder on PATH (or use full path):
cb launch                           # auto-detect Brave > Chrome > Edge
cb launch chrome                    # force Chrome
$env:CB_BROWSER = "edge"; cb launch # via env var
$env:CB_BROWSER_PATH = "C:\path\to\custom-chromium.exe"; cb launch
```

`launch` will:
1. Try to attach to whatever's running on CDP port 9333. If it works, exit.
2. Otherwise kill any existing instance of the chosen browser (CDP needs the
   `--remote-debugging-port` flag at startup; you can't add it to a running
   process), wait, relaunch detached with the flag, wait for CDP to come up.

Sessions persist — your normal Brave/Chrome/Edge profile loads, you stay
logged into Gmail / GitHub / whatever. The harness rides along.

### Environment overrides

| Var | Default | Purpose |
|---|---|---|
| `CB_CDP_PORT` | `9333` | DevTools port |
| `CB_BROWSER` | (auto) | `brave` / `chrome` / `edge` |
| `CB_BROWSER_PATH` | (auto) | Full path to a Chromium-based exe |
| `CB_USER_DATA_DIR` | (browser default) | Custom profile dir |
| `CB_TIMEOUT_MS` | `8000` | Default per-action timeout |
| `CB_SHOT_DIR` | `~` (home) | Where snap/screenshot files go |
| `CB_STATE_DIR` | `~/.cb` | Where state.json lives |

## The killer feature: snap → ref

```powershell
cb snap                  # → snap-20260506-143012-annotated.png + state.json
```

Output:

```
URL:    https://github.com/anthropics
Title:  Anthropic · GitHub
Shot:   ~\documents\snap-20260506-143012-annotated.png
Elements: 47 (use #N to reference)

  #  0 a        link        Skip to content
  #  1 input    text                  (search input)
  #  2 button   button      Sign in
  #  3 a        link        anthropics
  #  4 a        link        Repositories
  ...
```

Now any selector-taking command accepts `#N` or `7`:

```powershell
cb click 2               # click "Sign in"
cb fill 1 "claude-code"  # fill the search input
cb hover #4              # hover Repositories tab
```

The annotated PNG is what Claude reads to decide what to click. Box colors:
red = inputs, blue = buttons, green = links, purple = other interactive.

Re-snap after every state change — refs invalidate when the DOM changes.

## Command reference

### Connection

| Command | Purpose |
|---|---|
| `cb launch [brave\|chrome\|edge]` | Start browser w/ CDP, or attach if already up |
| `cb kill [brave\|chrome\|edge]` | Force-quit browser (rare) |
| `cb connect` / `cb title` | Verify connection: print URL + title of active page |

### Navigation

| Command | Purpose |
|---|---|
| `cb goto <url> [--wait load\|domcontentloaded\|networkidle]` | Navigate |
| `cb back`, `cb forward`, `cb reload` | History |

### Inspection (high-signal)

| Command | Purpose |
|---|---|
| `cb snap [--full]` | **Annotated screenshot + element registry. Use this first.** |
| `cb screenshot [name] [--full]` | Plain screenshot |
| `cb html [--trim N]` | Page HTML (trimmed; default 15 KB) |
| `cb text <sel\|#N>` | Text content of element |
| `cb links [filter]` | All `<a href>` links, optional substring filter |
| `cb forms` | Visible form controls w/ labels, types, current values |
| `cb ax [--all]` | Accessibility tree (compact: `interesting_only=True` by default) |
| `cb eval <js>` | Run arbitrary JS, return result |

### DOM actions

All of these accept either a CSS selector / Playwright anchor or a numeric
ref from the last snap (`7` or `#7`).

| Command | Purpose |
|---|---|
| `cb click <sel\|#N> [--force] [--timeout N]` | Click. Auto-scrolls into view first |
| `cb dblclick <sel\|#N>` | Double-click |
| `cb fill <sel\|#N> <value>` | React-safe fill (proper input/change events) |
| `cb clear <sel\|#N>` | Clear an input |
| `cb check <sel\|#N>` / `uncheck <sel\|#N>` | Force-toggle (works on MUI/custom checkboxes) |
| `cb select <sel\|#N> <option>` | Native `<select>`, value or label |
| `cb upload <sel\|#N> <path>` | File upload to `<input type="file">` |
| `cb hover <sel\|#N>` / `focus <sel\|#N>` / `blur <sel\|#N>` | Mouse/focus state |
| `cb press <sel\|#N> <key>` | Press a key on element (e.g. `Enter`, `Control+A`) |
| `cb keys <combo>` | Press a combo on the page (no element needed) |
| `cb type <text> [--delay N]` | Type into focused element (per-char delay optional) |
| `cb scroll <up\|down\|top\|bottom\|sel\|#N> [px]` | Scroll page or element into view |
| `cb coord_click <x> <y> [--button] [--clicks]` | Bypass DOM (canvas / PDF / OCR fallback) |
| `cb coord_move <x> <y>` | Move mouse to coords (hover canvas regions) |
| `cb drag <from> <to>` | Drag-and-drop |

### Smart actions (natural language)

| Command | Purpose |
|---|---|
| `cb act "<description>"` | Resolve target by multiple strategies, then act |
| `cb find "<description>"` | Show what `act` would target (no action) |
| `cb fill_form <json_or_path>` | Bulk fill: `{label: value}`. Auto-detects checkbox vs select vs file vs text |

`act` understands these verbs (defaulting to "click" if none): click / tap /
press / open / fill / type / enter / set / check / uncheck / toggle / select /
choose / pick / upload / attach.

Forms understood:

```bash
cb act "click Sign In"
cb act "fill Email with alex@example.com"
cb act "Email = alex@example.com"
cb act "check I agree to the terms"
cb act "select Country with United States"
cb act "upload Resume with C:\path\resume.pdf"
```

Resolution order:
1. If the target is a numeric ref (`#7`), use the snap registry.
2. If the target looks like a CSS selector or has a Playwright anchor prefix
   (`text=`, `role=`, `css=`, `xpath=`), use it directly.
3. `get_by_role(...)` for common roles.
4. `get_by_label`, `get_by_placeholder`, `get_by_text`.
5. Fuzzy string match against the last snap's element labels.
6. Fail with a list of close candidates.

### Waits (do these BEFORE acting on dynamic content)

| Command | Purpose |
|---|---|
| `cb wait <sel\|#N> [timeout_ms]` | Wait for element to be visible |
| `cb wait_hidden <sel\|#N> [timeout_ms]` | Wait for element to disappear |
| `cb wait_url <regex_or_substring> [timeout_ms]` | Wait for URL to match |
| `cb wait_text <text> [timeout_ms]` | Wait for visible text |
| `cb wait_idle [timeout_ms]` | Wait for network idle |
| `cb wait_js "<expr>" [timeout_ms]` | Wait for JS expression to be truthy |
| `cb sleep <ms>` | Last resort. Hard sleep |

### Tabs / frames

| Command | Purpose |
|---|---|
| `cb tabs` | List all tabs across all contexts as `[ctx:idx] title / url` |
| `cb tab <i>` | Switch to tab (`0:1` or just `1` for context 0) |
| `cb new_tab [url]` | Open a new tab |
| `cb close_tab [i]` | Close a tab (defaults to current) |
| `cb frames` | List iframes on current page |
| `cb frame <sel\|#N>` | Scope subsequent action commands to this iframe |
| `cb frame_reset` | Back to top frame |

### Sessions / cookies / storage

| Command | Purpose |
|---|---|
| `cb cookies [name]` | List cookies (optionally filter by name) |
| `cb cookies_save <file>` | Save all cookies to JSON |
| `cb cookies_load <file>` | Restore cookies (preserves login across browser restarts) |
| `cb storage [key]` | localStorage: dump everything, or read one key |

### Batch scripts

```yaml
# script.yaml — run with `cb batch script.yaml`
- goto: https://news.ycombinator.com
- wait_idle: 5000
- snap: []
- click: "text=login"
- fill: ["input[name='acct']", "myhandle"]
- fill: ["input[name='pw']", "mypassword"]
- act: "click login"
- wait_url: news
- snap: []
```

Args may be a string (shlex-split), a list (passed positionally), or a dict
(passed as inline JSON — useful for `fill_form`):

```yaml
- fill_form:
    First name: Alex
    Last name: Hill
    Country: United States
    I agree: true
```

`cb batch <script> --continue` keeps going past failures.

The whole script runs in a single CDP session, so it's much faster than
chaining one-shot commands.

## Recipes

### Log into something for the first time, save the session

```powershell
cb launch
cb goto https://app.example.com/login
# ... do whatever interactive auth (CAPTCHA, 2FA) the user needs to do ...
cb cookies_save ~\.cb\example-session.json
```

Next session:

```powershell
cb launch
cb cookies_load ~\.cb\example-session.json
cb goto https://app.example.com/dashboard
```

### Debug a click that "did nothing"

1. `cb snap` — annotated screenshot makes the actual layout obvious
2. `cb find "Submit"` — see what `act` would target and other candidates
3. `cb eval "document.activeElement.outerHTML"` — what's focused?
4. `cb eval "Array.from(document.querySelectorAll('[disabled]')).map(e => e.outerHTML)"` — anything disabled blocking?
5. `cb click 7 --force` — bypass actionability checks (overlays, animations)

### Workflows that span multiple pages

Always re-snap between pages. The numbered refs are cleared on every snap;
they refer to the page that was current when the snap was taken, not
forever-after. After `goto` or after clicking something that loaded a new
view, run `cb wait_idle` then `cb snap` again.

### Canvas / PDF / weird non-DOM targets

`cb coord_click x y` bypasses the DOM entirely. Get coords from a screenshot
+ ruler in your head, or from `cb eval` with a `getBoundingClientRect()` on
something nearby. Multi-monitor scaling can be off — verify with a screenshot.

### iframe content

```powershell
cb snap                       # find the iframe in the registry, e.g. #12
cb frame 12                   # subsequent commands scope to it
cb snap                       # registry now shows iframe contents
cb act "click Confirm"
cb frame_reset                # back to top
```

## Selector cheatsheet

| Form | Example | When to use |
|---|---|---|
| Numeric ref | `#7`, `7` | Right after `cb snap` |
| CSS | `button.submit`, `input[name="email"]`, `[data-testid="x"]` | Stable structural selectors |
| Text anchor | `text=Submit Application` | Click by visible text (Playwright extension) |
| Role | `role=button[name="Save"]` | Accessibility-first, robust to redesign |
| Aria | `[aria-label="Close"]` | Common on icon buttons |
| ID with digit prefix | `#abc123` works; `#1abc` auto-rewrites to `[id="1abc"]` | React-generated UUIDs |
| XPath | `xpath=//div[@class="..."]` | Last resort |

## Output conventions

- Single-shot commands print plain text or JSON to stdout. Errors go to stderr
  with `ERROR: …` and a non-zero exit.
- `snap` prints a compact summary (≤60 elements) and writes the full registry
  to `state.json`. Read `state.json` if you need to inspect anything beyond
  the summary.
- `forms`, `eval`, `cookies`, `ax`, `storage` print JSON.

## Limitations / not-shipped (yet)

- **No daemon mode.** Each command opens a fresh CDP attach (~300–500ms).
  `cb batch` is the workaround for chained workflows. A persistent server
  process would be next iteration.
- **No XHR/console capture.** Listeners are short-lived because each command
  reconnects. Will need the daemon for real network/console history.
- **No record-and-replay.** Manual scripting only.
- **No CAPTCHA solving / anti-bot evasion.** Out of scope. Stop and let the
  human handle it in the live browser.

## Files

```
cb/
├── cb.py                       # everything; single-file harness
├── cb.bat                      # Windows wrapper: `cb <args>`
├── cb.ps1                      # PowerShell wrapper
├── README.md                   # this
└── examples/
    ├── example_form.json       # minimal JSON batch
    └── login_flow.yaml         # YAML with fill_form
```
