# cb — development notes

Single-module harness: `cb.py` (Python) with the page-side library embedded as `_PAGE_JS_TEMPLATE`.

## Architecture

- **Thin client → daemon.** `main()` forwards argv to a per-session daemon over 127.0.0.1 TCP
  (token in `<state>/daemon.json`). The client path must stay cheap: heavy stdlib modules are
  `_LazyModule` proxies and Playwright is imported only via `pw()`. The daemon restarts itself
  when `cb.py` changes (`code_version()`). `CB_DAEMON=0` runs commands in-process (`run_local`).
- **Runtime** holds the CDP connection, the active page, and event capture (console, network,
  dialogs, downloads, popups). `observe()` runs after every mutating command: settle → report
  events → snapshot → print diff (or full outline after navigation). In `batch` it is deferred.
- **Page library** (`window.__cb`, injected via `frame.evaluate`, CSP-proof): `snapshot`
  (composed-tree walker → outline lines, stamps `data-cb-ref` attributes; refs come from a
  session-wide counter so they are never reused), `find` (scores elements for text targets),
  `describe`/`stamp`, `selectOptions`, `scroll`, `settle` (MutationObserver quiet period),
  `overlay` (annotated screenshots), `blocker` (overlay detection).
- **Targets** resolve in `resolve()`: ref → coordinates → explicit selector engine → CSS-looking →
  text (`find_everywhere` across iframes, then Playwright's label/placeholder/text getters).
  Refs carry their iframe chain (`f`), turned into locators by `scope_for_chain`.
- **Commands** register with `@command(...)`; aliases may carry preset flags
  (`("dblclick", ["--double"])`). The hand-written `OVERVIEW` is the `cb help` text; a test checks
  it names every command.
- State per session lives in `<state>/state.json` (refs map, last outline, active tab hint,
  frame scope, dialog policy, next ref).

## Testing

```bash
./.venv/bin/python -m pytest tests -q          # e2e against headless Chromium (~70 s)
./.venv/bin/python -m pyflakes cb.py tests/*.py
```

Tests run the CLI as subprocesses with an isolated `CB_STATE_DIR` and port, against
`tests/site/` served by `tests/server.py`. Add a fixture page there for new behaviors. For manual
poking: `python tests/server.py 8765 &` then `CB_STATE_DIR=/tmp/cbdev ./cb goto localhost:8765`.

## Conventions

- Output is for a model: short, stable formats, one fact per line, errors that say what to do.
- Every user-facing failure is a `CBError` (exit 1) or `UsageError` (exit 2); wrap Playwright
  exceptions with `clean_error()`.
- Keep Windows working: no POSIX-only calls outside `IS_WINDOWS` guards; paths via `ctx.path()`.
