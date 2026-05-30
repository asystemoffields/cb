"""
cb — Claude Browser. CDP-attached Playwright harness for browser tasks.

Designed to be operated by Claude Code: high-signal output, numeric refs from
snap, smart fallbacks on actions, batch scripts, multi-browser support
(Brave / Chrome / Edge — auto-detect, all Chromium-based).

Usage (see README for the full reference):
    cb launch [brave|chrome|edge]    # start browser w/ CDP, or attach if already up
    cb connect                       # verify connection (URL+title of active page)

    cb snap [--full]                 # screenshot + numbered overlay + element registry
    cb screenshot [name] [--full]
    cb title | html [--trim N] | text <sel> | links | forms | ax | eval <js>

    cb click <sel|#N>                # selector or numeric ref from last snap
    cb dblclick <sel|#N>
    cb fill <sel|#N> <value>         # React-safe
    cb clear <sel|#N>
    cb check <sel|#N> | uncheck <sel|#N>
    cb select <sel|#N> <option>      # native <select>
    cb upload <sel|#N> <path>
    cb hover <sel|#N> | focus <sel|#N> | blur <sel|#N>
    cb press <sel|#N> <key>          # key on element
    cb keys <combo>                  # global key combo (Tab, Enter, Ctrl+A, ...)
    cb type <text>                   # type into focused element
    cb scroll <direction|sel|#N> [px]
    cb coord_click <x> <y>           # bypass DOM (canvas, PDF, etc.)
    cb coord_move <x> <y>

    cb act "<description>"           # natural action: "click Submit", "fill Email with foo@bar"
    cb find "<description>"          # list candidate elements w/o acting
    cb fill_form <json_or_path>      # bulk fill: {"Email": "x@y", "I agree": true}

    cb wait <sel> [timeout_ms]       # wait for visible
    cb wait_hidden <sel> [timeout_ms]
    cb wait_url <regex> [timeout_ms]
    cb wait_text <text> [timeout_ms]
    cb wait_idle [timeout_ms]
    cb wait_js <expr> [timeout_ms]
    cb sleep <ms>

    cb back | forward | reload
    cb tabs | tab <i> | new_tab [url] | close_tab [i]
    cb frames | frame <sel|#N> | frame_reset

    cb cookies [name] | cookies_save <file> | cookies_load <file>
    cb storage [key]

    cb batch <file>                  # run a YAML/JSON action script
    cb help [command]

Setup:
    The browser must be running with --remote-debugging-port=9333.
    `cb launch` handles that (closes existing browser first).
    Override: CB_CDP_PORT=9334 cb ...
              CB_BROWSER=chrome cb launch
              CB_BROWSER_PATH="C:\\path\\to\\my-chromium.exe" cb launch

Selector notes:
    - Plain CSS works: button.submit, input[name='email'], [data-testid='foo']
    - Playwright text/role anchors: text=Submit, role=button[name='Save']
    - Numeric refs: 7 or #7 (after a snap) — looks up element from state.json
    - IDs starting with a digit auto-fall-back to [id="..."] form

React safety: fill uses Playwright .fill() (proper input/change events).
Force-clicks via check/uncheck for MUI/custom checkbox components.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

# Force UTF-8 stdout (Windows console + emoji/accents in page content)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Frame,
    async_playwright,
)


# ─── Config ──────────────────────────────────────────────────────────────────

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

CDP_PORT = int(os.environ.get("CB_CDP_PORT", "9333"))
SCREENSHOT_DIR = Path(os.environ.get("CB_SHOT_DIR", os.path.expanduser("~")))
STATE_DIR = Path(os.environ.get("CB_STATE_DIR", os.path.expanduser("~/.cb")))
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"
ACTIVE_FRAME_FILE = STATE_DIR / "active_frame.json"

DEFAULT_TIMEOUT_MS = int(os.environ.get("CB_TIMEOUT_MS", "8000"))

# Candidate browser locations per OS. Windows/macOS entries are absolute paths;
# Linux entries are mostly bare command names resolved against $PATH (see
# _resolve_browser_candidate). Set CB_BROWSER_PATH to override entirely.
if IS_WINDOWS:
    BROWSER_PATHS = {
        "brave": [
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        ],
        "chrome": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ],
        "edge": [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ],
    }
elif IS_MAC:
    BROWSER_PATHS = {
        "brave": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
        "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "edge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
    }
else:  # Linux / other POSIX
    BROWSER_PATHS = {
        "brave": ["brave-browser", "brave", "/opt/brave.com/brave/brave"],
        "chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
        "edge": ["microsoft-edge", "microsoft-edge-stable"],
    }

# Pattern used to kill a running browser. On Windows it's a Stop-Process -Name
# (no .exe); on POSIX it's a `pkill -f <pattern>` substring against the cmdline.
PROCESS_NAMES = {"brave": "brave", "chrome": "chrome", "edge": "msedge"}


def _resolve_browser_candidate(path: str) -> str | None:
    """Resolve a candidate to an absolute executable, or None.

    Absolute/relative paths (containing a path separator) are checked for
    existence; bare command names are looked up on $PATH via shutil.which."""
    if (os.sep in path) or ("/" in path):
        return path if Path(path).exists() else None
    return shutil.which(path)


def find_browser(preferred: str | None = None) -> tuple[str, str]:
    """Return (kind, path). Honors CB_BROWSER_PATH and CB_BROWSER env vars."""
    env_path = os.environ.get("CB_BROWSER_PATH")
    if env_path:
        resolved = env_path if Path(env_path).exists() else shutil.which(env_path)
        if resolved:
            name = Path(resolved).stem.lower()
            kind = ("edge" if "edge" in name
                    else "chrome" if ("chrome" in name or "chromium" in name)
                    else "brave" if "brave" in name else "chrome")
            return kind, resolved
    pref = preferred or os.environ.get("CB_BROWSER")
    order = [pref] if pref else []
    for kind in ("brave", "chrome", "edge"):
        if kind not in order:
            order.append(kind)
    for kind in order:
        if not kind:
            continue
        for path in BROWSER_PATHS.get(kind, []):
            resolved = _resolve_browser_candidate(path)
            if resolved:
                return kind, resolved
    raise CBError("No supported browser found. Set CB_BROWSER_PATH or install Brave/Chrome/Edge.")


# POSIX process-name variants per browser. Names are matched with `pkill -x`
# against the kernel `comm` (truncated to 15 chars), so e.g. the launcher
# wrapper `brave-browser` and the real `brave` binary are both covered.
_POSIX_PROC_ALIASES = {
    "brave": ["brave", "brave-browser"],
    "chrome": ["chrome", "google-chrome", "chromium", "chromium-browse"],
    "msedge": ["msedge", "microsoft-edge"],
}


def _kill_browser(proc_name: str) -> None:
    """Force-kill all processes for a browser, cross-platform."""
    if IS_WINDOWS:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"Stop-Process -Name {proc_name} -Force -ErrorAction SilentlyContinue"],
            check=False, capture_output=True,
        )
        return
    # POSIX: match exact process names (the executable basename). `-x` avoids
    # `-f`'s pitfall of matching any command line that merely *contains* the
    # word — including cb's own `cb kill brave` process, which would make cb
    # signal itself. Killing the main process tears down its renderer children.
    names = _POSIX_PROC_ALIASES.get(proc_name, [proc_name])

    def _alive() -> bool:
        return any(subprocess.run(["pgrep", "-x", n], capture_output=True).returncode == 0
                   for n in names)

    for n in names:                       # graceful first
        subprocess.run(["pkill", "-x", n], check=False, capture_output=True)
    for _ in range(20):                   # wait up to ~2s for clean exit
        if not _alive():
            return
        time.sleep(0.1)
    for n in names:                       # force anything that lingers
        subprocess.run(["pkill", "-9", "-x", n], check=False, capture_output=True)


# ─── State ───────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_active_frame() -> str | None:
    if not ACTIVE_FRAME_FILE.exists():
        return None
    try:
        return json.loads(ACTIVE_FRAME_FILE.read_text(encoding="utf-8")).get("selector")
    except Exception:
        return None


def save_active_frame(selector: str | None) -> None:
    if selector is None:
        if ACTIVE_FRAME_FILE.exists():
            ACTIVE_FRAME_FILE.unlink()
    else:
        ACTIVE_FRAME_FILE.write_text(json.dumps({"selector": selector}), encoding="utf-8")


def resolve_ref(ref: str) -> dict | None:
    """Return element dict from last snap if ref is a numeric index (`7`, `#7`, `@7`)."""
    s = ref.lstrip("#@")
    if not s.isdigit():
        return None
    idx = int(s)
    state = load_state()
    for el in state.get("elements", []):
        if el.get("idx") == idx:
            return el
    return None


# ─── Errors ──────────────────────────────────────────────────────────────────

class CBError(Exception):
    pass


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ─── Connection ──────────────────────────────────────────────────────────────

async def connect_cdp():
    p = await async_playwright().start()
    try:
        browser = await p.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
        return p, browser
    except Exception as e:
        await p.stop()
        raise CBError(
            f"Cannot connect to browser on CDP port {CDP_PORT}.\n"
            f"  Run: cb launch    (or launch your browser with --remote-debugging-port={CDP_PORT})\n"
            f"  Underlying error: {e}"
        )


async def get_active_page(browser: Browser) -> Page:
    """Return the foreground/most-recent visible page across all contexts.

    Strategy: look at all pages in all contexts, prefer one that's not about:blank
    or DevTools, and prefer the last in iteration order (Playwright surfaces them
    in approximate creation order).
    """
    contexts = browser.contexts
    if not contexts:
        ctx = await browser.new_context()
        return await ctx.new_page()

    candidates: list[Page] = []
    for ctx in contexts:
        for page in ctx.pages:
            url = page.url or ""
            if url.startswith("devtools://"):
                continue
            candidates.append(page)

    if not candidates:
        return await contexts[0].new_page()
    # Honor CB_TAB_URL env override (substring match against page.url)
    target = os.environ.get("CB_TAB_URL", "").strip()
    if target:
        for p in candidates:
            if target in (p.url or ""):
                return p
    # Prefer non-blank pages
    non_blank = [p for p in candidates if not (p.url == "about:blank" or p.url == "")]
    return (non_blank or candidates)[-1]


async def get_action_root(page: Page) -> Page | Frame:
    """Honor a saved active frame selector for action commands."""
    sel = load_active_frame()
    if not sel:
        return page
    try:
        handle = await page.wait_for_selector(sel, timeout=2000)
        if not handle:
            return page
        frame = await handle.content_frame()
        return frame or page
    except Exception:
        return page


_ACTIVE_BROWSER: Browser | None = None
_ACTIVE_PAGE: Page | None = None


@asynccontextmanager
async def session():
    """Per-command CDP session, OR reuse the active shared session if present."""
    global _ACTIVE_BROWSER, _ACTIVE_PAGE
    if _ACTIVE_BROWSER is not None and _ACTIVE_PAGE is not None:
        # Re-resolve the active page each time — a new tab may have become foreground
        try:
            _ACTIVE_PAGE = await get_active_page(_ACTIVE_BROWSER)
        except Exception:
            pass
        yield _ACTIVE_BROWSER, _ACTIVE_PAGE
        return
    p, browser = await connect_cdp()
    try:
        page = await get_active_page(browser)
        yield browser, page
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        await p.stop()


@asynccontextmanager
async def browser_session():
    """Like session(), but yields just the Browser (for tab/cookie commands).

    Reuses the active shared session if one is open."""
    global _ACTIVE_BROWSER
    if _ACTIVE_BROWSER is not None:
        yield _ACTIVE_BROWSER
        return
    p, browser = await connect_cdp()
    try:
        yield browser
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        await p.stop()


@asynccontextmanager
async def shared_session():
    """Open a CDP connection that subsequent session() calls reuse."""
    global _ACTIVE_BROWSER, _ACTIVE_PAGE
    p, browser = await connect_cdp()
    try:
        page = await get_active_page(browser)
        _ACTIVE_BROWSER = browser
        _ACTIVE_PAGE = page
        yield browser, page
    finally:
        _ACTIVE_BROWSER = None
        _ACTIVE_PAGE = None
        try:
            await browser.close()
        except Exception:
            pass
        await p.stop()


# ─── Selector handling ───────────────────────────────────────────────────────

def normalize_selector(sel: str) -> str:
    """Repair common selector mistakes Claude might emit."""
    if sel.startswith("#") and len(sel) > 1 and sel[1].isdigit():
        return f'[id="{sel[1:]}"]'
    return sel


def resolve_selector(arg: str) -> tuple[str, dict | None]:
    """Resolve a numeric ref OR a raw selector. Returns (selector, ref_dict_or_None)."""
    ref = resolve_ref(arg)
    if ref:
        return ref["selector"], ref
    return normalize_selector(arg), None


# ─── Argument parsing for command flags ──────────────────────────────────────

def split_flags(args: list[str]) -> tuple[list[str], dict[str, Any]]:
    """Pop --key / --key=val / --flag args from the list. Returns (positional, opts)."""
    pos: list[str] = []
    opts: dict[str, Any] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            body = a[2:]
            if "=" in body:
                k, v = body.split("=", 1)
                opts[k] = v
            else:
                # flag, or "--key value"
                if i + 1 < len(args) and not args[i + 1].startswith("--"):
                    # heuristic: only consume next arg if it doesn't look positional;
                    # for our usage simpler to treat it as boolean unless explicit form
                    opts[body] = True
                else:
                    opts[body] = True
            i += 1
            continue
        pos.append(a)
        i += 1
    return pos, opts


def opt_int(opts: dict, key: str, default: int) -> int:
    v = opts.get(key)
    if v is None or v is True:
        return default
    try:
        return int(v)
    except Exception:
        return default


# ─── Browser launcher ────────────────────────────────────────────────────────

async def cmd_launch(args: list[str], opts: dict) -> None:
    """Launch a browser with CDP enabled, after killing any existing instance."""
    pos, _ = split_flags(args)
    pref = pos[0] if pos else None
    kind, path = find_browser(pref)
    proc_name = PROCESS_NAMES[kind]

    # Try to attach first — maybe it's already running with CDP
    try:
        p, browser = await connect_cdp()
        await browser.close()
        await p.stop()
        print(f"Already attached on port {CDP_PORT}. Use 'cb kill' to relaunch.")
        return
    except CBError:
        pass

    # Kill any existing browser of this kind (otherwise the CDP flag is ignored)
    _kill_browser(proc_name)

    await asyncio.sleep(1.5)

    # Launch detached so the browser outlives this cb process
    user_data = os.environ.get("CB_USER_DATA_DIR")
    cmd = [path, f"--remote-debugging-port={CDP_PORT}"]
    if user_data:
        cmd.append(f"--user-data-dir={user_data}")
    if IS_WINDOWS:
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(cmd, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
    else:
        subprocess.Popen(cmd, start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    # Wait for CDP to come up
    for _ in range(30):
        await asyncio.sleep(0.5)
        try:
            p, browser = await connect_cdp()
            await browser.close()
            await p.stop()
            print(f"Launched {kind} ({path}) on CDP port {CDP_PORT}.")
            return
        except CBError:
            continue
    die(f"Launched {kind} but CDP port {CDP_PORT} never came up.")


async def cmd_kill(args: list[str], opts: dict) -> None:
    pos, _ = split_flags(args)
    pref = pos[0] if pos else None
    if pref:
        names = [PROCESS_NAMES[pref]]
    else:
        names = list(set(PROCESS_NAMES.values()))
    for n in names:
        _kill_browser(n)
    print(f"Killed: {', '.join(names)}")


# ─── Inspection ──────────────────────────────────────────────────────────────

async def cmd_connect(args, opts):
    async with session() as (_, page):
        print(f"URL:   {page.url}")
        print(f"Title: {await page.title()}")
        active_frame = load_active_frame()
        if active_frame:
            print(f"Frame: {active_frame}")


async def cmd_title(args, opts):
    await cmd_connect(args, opts)


async def cmd_screenshot(args, opts):
    pos, opts = split_flags(args)
    fname = pos[0] if pos else f"shot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    if not fname.endswith(".png"):
        fname += ".png"
    full = bool(opts.get("full", False))
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / fname
    async with session() as (_, page):
        await page.screenshot(path=str(path), full_page=full)
    print(str(path))


async def cmd_html(args, opts):
    pos, opts = split_flags(args)
    trim = opt_int(opts, "trim", 15000)
    async with session() as (_, page):
        root = await get_action_root(page)
        if isinstance(root, Frame):
            html = await root.content()
        else:
            html = await root.content()
    if trim and len(html) > trim:
        html = html[:trim] + f"\n... [truncated, {len(html)-trim} more chars]"
    print(html)


async def cmd_text(args, opts):
    pos, _ = split_flags(args)
    if not pos:
        die("Usage: cb text <selector|#N>")
    sel, _ = resolve_selector(pos[0])
    async with session() as (_, page):
        root = await get_action_root(page)
        loc = root.locator(sel).first
        try:
            txt = await loc.text_content(timeout=5000)
        except Exception:
            try:
                txt = await loc.inner_text(timeout=5000)
            except Exception:
                txt = None
    if txt is None:
        die(f"Element not found: {sel}")
    print(txt)


async def cmd_links(args, opts):
    pos, opts = split_flags(args)
    flt = pos[0].lower() if pos else None
    async with session() as (_, page):
        root = await get_action_root(page)
        links = await root.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]')).map(e => ({
                text: (e.textContent || '').trim().substring(0, 100),
                href: e.href,
            })).filter(l => l.href && !l.href.startsWith('javascript:'))
        """)
    for i, link in enumerate(links):
        if flt and flt not in link["text"].lower() and flt not in link["href"].lower():
            continue
        print(f"[{i}] {link['text']}")
        print(f"    {link['href']}")


async def cmd_eval(args, opts):
    if not args:
        die("Usage: cb eval <javascript>")
    js = " ".join(args)
    async with session() as (_, page):
        root = await get_action_root(page)
        result = await root.evaluate(js)
    if isinstance(result, (dict, list)):
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result)


async def cmd_forms(args, opts):
    """Visible form controls with selector hints (improved over old `form`)."""
    async with session() as (_, page):
        root = await get_action_root(page)
        fields = await root.evaluate(_FORMS_JS)
    print(json.dumps(fields, indent=2, ensure_ascii=False))


_FORMS_JS = r"""
() => {
    const sel = 'input, textarea, select, button';
    return Array.from(document.querySelectorAll(sel)).filter(el => {
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.visibility !== 'hidden'
            && style.display !== 'none'
            && rect.width > 0 && rect.height > 0
            && el.type !== 'hidden';
    }).map((el, i) => {
        const labelFromId = el.id
            ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText?.trim()
            : '';
        const parentLabel = el.closest('label')?.innerText?.trim() || '';
        const labelledBy = el.getAttribute('aria-labelledby');
        const ariaLabelled = labelledBy
            ? labelledBy.split(/\s+/).map(id => document.getElementById(id)?.innerText?.trim()).filter(Boolean).join(' ')
            : '';
        const label = el.getAttribute('aria-label')
            || ariaLabelled
            || labelFromId
            || parentLabel
            || el.placeholder
            || el.name
            || el.id
            || (el.innerText || '').trim()
            || '';
        const dt = el.getAttribute('data-testid');
        const selector = el.id
            ? (el.id.match(/^\d/) ? `[id="${el.id}"]` : `#${CSS.escape(el.id)}`)
            : el.name
                ? `${el.tagName.toLowerCase()}[name="${el.name}"]`
                : dt
                    ? `[data-testid="${dt}"]`
                    : `${el.tagName.toLowerCase()}:nth-of-type(${i + 1})`;
        const options = el.tagName === 'SELECT'
            ? Array.from(el.options).map(o => ({ value: o.value, label: o.label || o.text })).slice(0, 24)
            : undefined;
        return {
            tag: el.tagName.toLowerCase(),
            type: el.type || '',
            label: label.replace(/\s+/g, ' ').slice(0, 140),
            selector,
            value: (el.value || '').slice(0, 140),
            checked: el.checked === true || undefined,
            required: !!el.required || el.getAttribute('aria-required') === 'true',
            disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
            options
        };
    });
}
"""


_SNAP_JS = r"""
() => {
    const isInteractive = el => {
        if (el.matches('input:not([type=hidden]), textarea, select, button, a[href]')) return true;
        const role = el.getAttribute('role');
        if (role && ['button','link','textbox','checkbox','radio','combobox','tab','menuitem','switch','slider','option','listbox','menubar','menu'].includes(role)) return true;
        if (el.getAttribute('contenteditable') === 'true') return true;
        if (el.getAttribute('tabindex') && el.getAttribute('tabindex') !== '-1') return true;
        const onclick = el.onclick || el.getAttribute('onclick');
        if (onclick) return true;
        return false;
    };
    const visible = el => {
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return rect.width >= 4 && rect.height >= 4
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth
            && style.visibility !== 'hidden'
            && style.display !== 'none'
            && parseFloat(style.opacity || '1') > 0.05;
    };
    const elements = Array.from(document.querySelectorAll('*')).filter(el => isInteractive(el) && visible(el));
    // Suppress nested interactive: if a child interactive sits fully inside a parent interactive, drop the parent
    const skip = new Set();
    for (const el of elements) {
        let p = el.parentElement;
        while (p) {
            if (elements.includes(p)) {
                // drop parent in favor of child only if parent has *only* this kind of interactive content
                if (p.tagName === 'A' || p.tagName === 'BUTTON') break;  // keep parent
                skip.add(p);
                break;
            }
            p = p.parentElement;
        }
    }
    const final = elements.filter(el => !skip.has(el)).slice(0, 250);
    return final.map((el, i) => {
        const rect = el.getBoundingClientRect();
        const labelFromId = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText?.trim() : '';
        const parentLabel = el.closest('label')?.innerText?.trim() || '';
        const labelledBy = el.getAttribute('aria-labelledby');
        const ariaLabelled = labelledBy
            ? labelledBy.split(/\s+/).map(id => document.getElementById(id)?.innerText?.trim()).filter(Boolean).join(' ')
            : '';
        const ariaLabel = el.getAttribute('aria-label') || '';
        const placeholder = el.getAttribute('placeholder') || el.placeholder || '';
        const text = (el.innerText || el.value || '').trim().slice(0, 100);
        const label = (ariaLabel || ariaLabelled || labelFromId || parentLabel || placeholder || text || el.name || el.id || '').replace(/\s+/g, ' ').slice(0, 120);
        const dt = el.getAttribute('data-testid');
        const dq = el.getAttribute('data-qa');
        const dc = el.getAttribute('data-cy');
        const selector = el.id
            ? (el.id.match(/^\d/) ? `[id="${el.id}"]` : `#${CSS.escape(el.id)}`)
            : el.name
                ? `${el.tagName.toLowerCase()}[name="${el.name}"]`
                : dt ? `[data-testid="${dt}"]`
                : dq ? `[data-qa="${dq}"]`
                : dc ? `[data-cy="${dc}"]`
                : ariaLabel ? `[aria-label="${ariaLabel.replace(/"/g, '\\"')}"]`
                : (el.tagName === 'A' || el.tagName === 'BUTTON') && text ? `text=${text.slice(0, 60)}`
                : `${el.tagName.toLowerCase()}:nth-of-type(${Array.from(el.parentNode.children).filter(c => c.tagName === el.tagName).indexOf(el) + 1})`;
        return {
            idx: i,
            tag: el.tagName.toLowerCase(),
            type: el.type || el.getAttribute('role') || '',
            label,
            selector,
            value: (el.value || '').toString().slice(0, 80),
            checked: el.checked === true ? true : undefined,
            disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true' || undefined,
            href: el.tagName === 'A' ? (el.href || '').slice(0, 200) : undefined,
            bbox: { x: Math.round(rect.left), y: Math.round(rect.top), w: Math.round(rect.width), h: Math.round(rect.height) }
        };
    });
}
"""


async def cmd_snap(args, opts):
    """Annotated screenshot + element registry. Updates state.json with refs."""
    pos, opts = split_flags(args)
    full = bool(opts.get("full", False))
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = SCREENSHOT_DIR / f"snap-{timestamp}.png"
    annot_path = SCREENSHOT_DIR / f"snap-{timestamp}-annotated.png"

    async with session() as (_, page):
        root = await get_action_root(page)
        # Element collection happens before screenshot so layout matches
        elements = await root.evaluate(_SNAP_JS)
        dpr = await page.evaluate("window.devicePixelRatio || 1")
        await page.screenshot(path=str(raw_path), full_page=full)
        url = page.url
        title = await page.title()

    # Annotate
    annotated_ok = False
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(raw_path).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        try:
            font = ImageFont.truetype("arial.ttf", 13)
            font_small = ImageFont.truetype("arial.ttf", 11)
        except Exception:
            font = ImageFont.load_default()
            font_small = font

        # Distinct colors for tag types
        color_for = {
            "input": (220, 30, 30, 230),
            "textarea": (220, 30, 30, 230),
            "select": (220, 30, 30, 230),
            "button": (30, 120, 220, 230),
            "a": (30, 160, 60, 230),
        }
        default_color = (160, 30, 200, 230)

        for el in elements:
            # CSS pixels → image pixels (Windows DPI scaling)
            x = int(round(el["bbox"]["x"] * dpr))
            y = int(round(el["bbox"]["y"] * dpr))
            w = int(round(el["bbox"]["w"] * dpr))
            h = int(round(el["bbox"]["h"] * dpr))
            if w <= 0 or h <= 0:
                continue
            if y + h < 0 or y > img.height or x + w < 0 or x > img.width:
                continue
            color = color_for.get(el["tag"], default_color)
            draw.rectangle([x, y, x + w - 1, y + h - 1], outline=color, width=2)
            label = str(el["idx"])
            try:
                tw = int(draw.textlength(label, font=font))
            except Exception:
                tw = 8 * len(label)
            th = int(round(16 * dpr))
            # Tag in upper-left of box (or above if there's room)
            tag_y = y - th if y - th >= 0 else y
            draw.rectangle([x, tag_y, x + tw + 8, tag_y + th], fill=color)
            try:
                font_use = ImageFont.truetype("arial.ttf", max(11, int(round(13 * dpr))))
            except Exception:
                font_use = font
            draw.text((x + 3, tag_y + 1), label, fill=(255, 255, 255, 255), font=font_use)
        composite = Image.alpha_composite(img, overlay)
        composite.convert("RGB").save(annot_path, "PNG")
        annotated_ok = True
    except ImportError:
        pass
    except Exception as e:
        print(f"WARN: annotation failed: {e}", file=sys.stderr)

    state = {
        "url": url,
        "title": title,
        "timestamp": timestamp,
        "screenshot": str(raw_path),
        "annotated": str(annot_path) if annotated_ok else None,
        "device_pixel_ratio": dpr,
        "elements": elements,  # bbox is in CSS pixels; multiply by dpr for image coords
    }
    save_state(state)

    print(f"URL:    {url}")
    print(f"Title:  {title}")
    print(f"Shot:   {annot_path if annotated_ok else raw_path}")
    print(f"Elements: {len(elements)} (use #N to reference)")
    print()
    for el in elements[:60]:
        marker = ""
        if el.get("checked"):
            marker = "[x] "
        elif el["type"] in ("checkbox", "radio"):
            marker = "[ ] "
        if el.get("disabled"):
            marker = "(disabled) " + marker
        type_short = (el["type"] or el["tag"])[:11]
        print(f"  #{el['idx']:>3} {el['tag']:<8} {type_short:<11} {marker}{el['label'][:70]}")
    if len(elements) > 60:
        print(f"  ... and {len(elements) - 60} more (see {STATE_FILE})")


async def cmd_ax(args, opts):
    """Accessibility tree (Chrome's, via Playwright)."""
    pos, opts = split_flags(args)
    interesting_only = not opts.get("all", False)
    async with session() as (_, page):
        snapshot = await page.accessibility.snapshot(interesting_only=interesting_only)
    print(json.dumps(snapshot, indent=2, ensure_ascii=False))


# ─── DOM actions ─────────────────────────────────────────────────────────────

async def _safe_click(loc, *, timeout: int, force: bool = False) -> None:
    try:
        await loc.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    await loc.click(timeout=timeout, force=force)


async def cmd_click(args, opts):
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb click <selector|#N>")
    sel, _ = resolve_selector(pos[0])
    timeout = opt_int(opts, "timeout", DEFAULT_TIMEOUT_MS)
    force = bool(opts.get("force", False))
    async with session() as (_, page):
        root = await get_action_root(page)
        loc = root.locator(sel).first
        await _safe_click(loc, timeout=timeout, force=force)
        print(f"Clicked: {sel}")
        # If URL changed within a tick, surface that
        await asyncio.sleep(0.15)
        print(f"URL: {page.url}")


async def cmd_dblclick(args, opts):
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb dblclick <selector|#N>")
    sel, _ = resolve_selector(pos[0])
    timeout = opt_int(opts, "timeout", DEFAULT_TIMEOUT_MS)
    async with session() as (_, page):
        root = await get_action_root(page)
        loc = root.locator(sel).first
        try:
            await loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        await loc.dblclick(timeout=timeout)
    print(f"Double-clicked: {sel}")


async def cmd_fill(args, opts):
    pos, opts = split_flags(args)
    if len(pos) < 2:
        die("Usage: cb fill <selector|#N> <value>")
    sel, _ = resolve_selector(pos[0])
    value = " ".join(pos[1:])
    timeout = opt_int(opts, "timeout", DEFAULT_TIMEOUT_MS)
    async with session() as (_, page):
        root = await get_action_root(page)
        loc = root.locator(sel).first
        try:
            await loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        await loc.fill(value, timeout=timeout)
    print(f"Filled {sel}: {value}")


async def cmd_clear(args, opts):
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb clear <selector|#N>")
    sel, _ = resolve_selector(pos[0])
    async with session() as (_, page):
        root = await get_action_root(page)
        await root.locator(sel).first.fill("", timeout=DEFAULT_TIMEOUT_MS)
    print(f"Cleared {sel}")


async def cmd_check(args, opts):
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb check <selector|#N>")
    sel, _ = resolve_selector(pos[0])
    async with session() as (_, page):
        root = await get_action_root(page)
        loc = root.locator(sel).first
        await loc.check(force=True, timeout=DEFAULT_TIMEOUT_MS)
        try:
            state = await loc.evaluate("el => el.checked ?? el.getAttribute('aria-checked')")
        except Exception:
            state = "?"
    print(f"Checked {sel} (state: {state})")


async def cmd_uncheck(args, opts):
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb uncheck <selector|#N>")
    sel, _ = resolve_selector(pos[0])
    async with session() as (_, page):
        root = await get_action_root(page)
        loc = root.locator(sel).first
        await loc.uncheck(force=True, timeout=DEFAULT_TIMEOUT_MS)
    print(f"Unchecked {sel}")


async def cmd_select(args, opts):
    pos, opts = split_flags(args)
    if len(pos) < 2:
        die("Usage: cb select <selector|#N> <value_or_label>")
    sel, _ = resolve_selector(pos[0])
    value = " ".join(pos[1:])
    async with session() as (_, page):
        root = await get_action_root(page)
        loc = root.locator(sel).first
        try:
            await loc.select_option(value=value, timeout=DEFAULT_TIMEOUT_MS)
            print(f"Selected {sel} by value: {value}")
        except Exception:
            await loc.select_option(label=value, timeout=DEFAULT_TIMEOUT_MS)
            print(f"Selected {sel} by label: {value}")


async def cmd_upload(args, opts):
    pos, opts = split_flags(args)
    if len(pos) < 2:
        die("Usage: cb upload <selector|#N> <path>")
    sel, _ = resolve_selector(pos[0])
    path = pos[1]
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if not os.path.exists(path):
        die(f"File not found: {path}")
    async with session() as (_, page):
        root = await get_action_root(page)
        await root.locator(sel).first.set_input_files(path)
    print(f"Uploaded {path} to {sel}")


async def cmd_hover(args, opts):
    pos, _ = split_flags(args)
    if not pos:
        die("Usage: cb hover <selector|#N>")
    sel, _ = resolve_selector(pos[0])
    async with session() as (_, page):
        root = await get_action_root(page)
        await root.locator(sel).first.hover(timeout=DEFAULT_TIMEOUT_MS)
    print(f"Hovered {sel}")


async def cmd_focus(args, opts):
    pos, _ = split_flags(args)
    if not pos:
        die("Usage: cb focus <selector|#N>")
    sel, _ = resolve_selector(pos[0])
    async with session() as (_, page):
        root = await get_action_root(page)
        await root.locator(sel).first.focus(timeout=DEFAULT_TIMEOUT_MS)
    print(f"Focused {sel}")


async def cmd_blur(args, opts):
    pos, _ = split_flags(args)
    if not pos:
        die("Usage: cb blur <selector|#N>")
    sel, _ = resolve_selector(pos[0])
    async with session() as (_, page):
        root = await get_action_root(page)
        await root.locator(sel).first.evaluate("el => el.blur()")
    print(f"Blurred {sel}")


async def cmd_press(args, opts):
    """Press a key while focused on a specific element."""
    pos, _ = split_flags(args)
    if len(pos) < 2:
        die("Usage: cb press <selector|#N> <key>  (e.g., 'Enter', 'Control+A', 'Tab')")
    sel, _ = resolve_selector(pos[0])
    key = pos[1]
    async with session() as (_, page):
        root = await get_action_root(page)
        await root.locator(sel).first.press(key, timeout=DEFAULT_TIMEOUT_MS)
    print(f"Pressed {key} on {sel}")


async def cmd_keys(args, opts):
    """Press a key combo on the currently focused element / page."""
    pos, _ = split_flags(args)
    if not pos:
        die("Usage: cb keys <combo>  (e.g., 'Tab', 'Enter', 'Control+A', 'Shift+Tab')")
    combo = pos[0]
    async with session() as (_, page):
        await page.keyboard.press(combo)
    print(f"Pressed {combo}")


async def cmd_type(args, opts):
    """Type text into the currently focused element."""
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb type <text>  [--delay 30]")
    text = " ".join(pos)
    delay = opt_int(opts, "delay", 0)
    async with session() as (_, page):
        await page.keyboard.type(text, delay=delay)
    print(f"Typed: {text}")


async def cmd_scroll(args, opts):
    """Scroll: cb scroll up|down|top|bottom|<sel|#N> [px]"""
    pos, _ = split_flags(args)
    if not pos:
        die("Usage: cb scroll up|down|top|bottom|<selector|#N> [px]")
    target = pos[0]
    px = int(pos[1]) if len(pos) > 1 and pos[1].lstrip("-").isdigit() else 600
    async with session() as (_, page):
        root = await get_action_root(page)
        if target == "up":
            await page.mouse.wheel(0, -px)
        elif target == "down":
            await page.mouse.wheel(0, px)
        elif target == "top":
            await page.evaluate("window.scrollTo(0, 0)")
        elif target == "bottom":
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        else:
            sel, _ = resolve_selector(target)
            await root.locator(sel).first.scroll_into_view_if_needed(timeout=DEFAULT_TIMEOUT_MS)
    print(f"Scrolled: {target}")


async def cmd_coord_click(args, opts):
    pos, opts = split_flags(args)
    if len(pos) < 2:
        die("Usage: cb coord_click <x> <y> [--button left|right|middle] [--clicks N]")
    x, y = int(pos[0]), int(pos[1])
    button = opts.get("button", "left")
    clicks = opt_int(opts, "clicks", 1)
    async with session() as (_, page):
        await page.mouse.click(x, y, button=button, click_count=clicks)
    print(f"Clicked at ({x},{y}) [{button}, x{clicks}]")


async def cmd_coord_move(args, opts):
    pos, _ = split_flags(args)
    if len(pos) < 2:
        die("Usage: cb coord_move <x> <y>")
    x, y = int(pos[0]), int(pos[1])
    async with session() as (_, page):
        await page.mouse.move(x, y)
    print(f"Moved to ({x},{y})")


async def cmd_drag(args, opts):
    pos, _ = split_flags(args)
    if len(pos) < 2:
        die("Usage: cb drag <from-sel|#N> <to-sel|#N>")
    from_sel, _ = resolve_selector(pos[0])
    to_sel, _ = resolve_selector(pos[1])
    async with session() as (_, page):
        root = await get_action_root(page)
        await root.locator(from_sel).first.drag_to(root.locator(to_sel).first, timeout=DEFAULT_TIMEOUT_MS)
    print(f"Dragged {from_sel} -> {to_sel}")


# ─── Smart actions ───────────────────────────────────────────────────────────

ACT_VERBS = {
    "click": "click",
    "tap": "click",
    "press": "click",
    "select": "click",
    "open": "click",
    "fill": "fill",
    "type": "fill",
    "enter": "fill",
    "set": "fill",
    "check": "check",
    "uncheck": "uncheck",
    "toggle": "check",
    "choose": "select",
    "pick": "select",
    "upload": "upload",
    "attach": "upload",
}


def _parse_act(desc: str) -> tuple[str, str, str | None]:
    """Parse "<verb> <target> [with <value>]" into (verb, target, value).

    Examples:
        "click Submit"                  -> ("click", "Submit", None)
        "fill Email with foo@bar"       -> ("fill", "Email", "foo@bar")
        "Email = foo@bar"               -> ("fill", "Email", "foo@bar")
        "check I agree to terms"        -> ("check", "I agree to terms", None)
    """
    # Equals form
    if "=" in desc and " with " not in desc.lower():
        # split on first = not inside quotes
        parts = desc.split("=", 1)
        return ("fill", parts[0].strip(), parts[1].strip())
    tokens = desc.strip().split()
    if not tokens:
        raise CBError("Empty description")
    verb_lc = tokens[0].lower()
    if verb_lc in ACT_VERBS:
        verb = ACT_VERBS[verb_lc]
        rest = " ".join(tokens[1:])
    else:
        verb = "click"
        rest = desc.strip()
    value = None
    m = re.search(r"\s+with\s+", rest, re.IGNORECASE)
    if m and verb in ("fill", "upload", "select"):
        target = rest[: m.start()].strip()
        value = rest[m.end() :].strip()
        # strip surrounding quotes
        if value and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
    else:
        target = rest
    if target and target[0] in "\"'" and target[-1] == target[0]:
        target = target[1:-1]
    return verb, target, value


async def _resolve_target(root, target: str) -> tuple[Any, str, list[dict]]:
    """Try multiple strategies to locate an element by description.

    Returns (locator, description_of_strategy, candidates).
    Candidates are returned for diagnostics when nothing matched well.
    """
    # 1) Numeric ref
    ref = resolve_ref(target)
    if ref:
        return root.locator(ref["selector"]).first, f"snap-ref #{ref['idx']}", []

    # 2) Looks like a CSS selector or playwright anchor
    looks_selector = (
        any(c in target for c in "[].#") and " " not in target
    ) or target.startswith(("text=", "role=", "css=", "xpath="))
    if looks_selector:
        loc = root.locator(normalize_selector(target)).first
        return loc, f"selector {target}", []

    # 3) Playwright role-based — try common roles
    candidates: list[dict] = []
    for role in ("button", "link", "checkbox", "radio", "textbox", "combobox", "tab", "menuitem", "switch"):
        try:
            loc = root.get_by_role(role, name=target, exact=False).first
            if await loc.count() > 0:
                return loc, f"role={role}[name~={target!r}]", []
        except Exception:
            pass

    # 4) get_by_label
    try:
        loc = root.get_by_label(target, exact=False).first
        if await loc.count() > 0:
            return loc, f"label~={target!r}", []
    except Exception:
        pass

    # 5) get_by_placeholder
    try:
        loc = root.get_by_placeholder(target, exact=False).first
        if await loc.count() > 0:
            return loc, f"placeholder~={target!r}", []
    except Exception:
        pass

    # 6) get_by_text
    try:
        loc = root.get_by_text(target, exact=False).first
        if await loc.count() > 0:
            return loc, f"text~={target!r}", []
    except Exception:
        pass

    # 7) Fuzzy match against current snap registry
    state = load_state()
    elements = state.get("elements", []) if isinstance(state, dict) else []
    target_lc = target.lower()
    scored = []
    for el in elements:
        label = (el.get("label") or "").lower()
        if not label:
            continue
        score = 0
        if target_lc == label:
            score = 100
        elif target_lc in label:
            score = 70 + (10 if label.startswith(target_lc) else 0)
        else:
            # token overlap
            tt = set(target_lc.split())
            lt = set(label.split())
            if tt & lt:
                score = 40 * len(tt & lt) / max(1, len(tt))
        if score > 0:
            scored.append((score, el))
    scored.sort(key=lambda x: -x[0])
    candidates = [el for _, el in scored[:8]]
    if scored and scored[0][0] >= 70:
        el = scored[0][1]
        return root.locator(el["selector"]).first, f"fuzzy snap-match #{el['idx']} ({el['label']!r})", candidates

    raise CBError(
        f"Could not find element matching {target!r}. "
        f"Try `cb snap` first, then `cb find {target!r}` to see candidates."
    )


async def cmd_act(args, opts):
    if not args:
        die('Usage: cb act "<description>"  e.g. "click Submit", "fill Email with x@y"')
    desc = " ".join(args)
    try:
        verb, target, value = _parse_act(desc)
    except CBError as e:
        die(str(e))
    if verb in ("fill", "upload", "select") and value is None:
        die(f"'{verb}' requires a value: try '{verb} <field> with <value>' or '<field> = <value>'.")
    async with session() as (_, page):
        root = await get_action_root(page)
        try:
            loc, strategy, _ = await _resolve_target(root, target)
        except CBError as e:
            die(str(e))
        try:
            await loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        if verb == "click":
            await loc.click(timeout=DEFAULT_TIMEOUT_MS)
        elif verb == "fill":
            await loc.fill(value, timeout=DEFAULT_TIMEOUT_MS)
        elif verb == "check":
            await loc.check(force=True, timeout=DEFAULT_TIMEOUT_MS)
        elif verb == "uncheck":
            await loc.uncheck(force=True, timeout=DEFAULT_TIMEOUT_MS)
        elif verb == "select":
            try:
                await loc.select_option(value=value, timeout=DEFAULT_TIMEOUT_MS)
            except Exception:
                await loc.select_option(label=value, timeout=DEFAULT_TIMEOUT_MS)
        elif verb == "upload":
            if not os.path.isabs(value):
                value = os.path.abspath(value)
            await loc.set_input_files(value)
    print(f"OK: {verb} {target!r}" + (f" with {value!r}" if value else "") + f"  [via {strategy}]")


async def cmd_find(args, opts):
    if not args:
        die('Usage: cb find "<description>"')
    target = " ".join(args)
    async with session() as (_, page):
        root = await get_action_root(page)
        try:
            loc, strategy, candidates = await _resolve_target(root, target)
        except CBError as e:
            die(str(e))
    print(f"Best match via: {strategy}")
    if candidates:
        print("\nFuzzy candidates from last snap:")
        for el in candidates:
            print(f"  #{el['idx']:>3} {el['tag']:<8} {el['label']!r}  -> {el['selector']}")


async def cmd_fill_form(args, opts):
    """Bulk form fill from JSON. Keys are matched to labels; values fill / check / select."""
    if not args:
        die("Usage: cb fill_form <json_or_path>")
    raw = args[0]
    if Path(raw).exists():
        data = json.loads(Path(raw).read_text(encoding="utf-8"))
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        die("fill_form expects a JSON object {label: value}")

    results = []
    async with session() as (_, page):
        root = await get_action_root(page)
        # Fetch field registry
        fields = await root.evaluate(_FORMS_JS)
        # Build label index
        idx = []
        for f in fields:
            label = (f.get("label") or "").strip().lower()
            if label:
                idx.append((label, f))
        for key, val in data.items():
            tk = key.strip().lower()
            field = None
            # exact label match
            for label, f in idx:
                if label == tk:
                    field = f
                    break
            if not field:
                # contains
                for label, f in idx:
                    if tk in label:
                        field = f
                        break
            if not field:
                results.append({"key": key, "ok": False, "reason": "no matching label"})
                continue
            sel = field["selector"]
            try:
                loc = root.locator(sel).first
                if field["tag"] == "select":
                    sval = str(val)
                    try:
                        await loc.select_option(value=sval, timeout=DEFAULT_TIMEOUT_MS)
                    except Exception:
                        await loc.select_option(label=sval, timeout=DEFAULT_TIMEOUT_MS)
                elif field["type"] in ("checkbox", "radio"):
                    if bool(val):
                        await loc.check(force=True, timeout=DEFAULT_TIMEOUT_MS)
                    else:
                        await loc.uncheck(force=True, timeout=DEFAULT_TIMEOUT_MS)
                elif field["type"] == "file":
                    p = str(val)
                    if not os.path.isabs(p):
                        p = os.path.abspath(p)
                    await loc.set_input_files(p)
                else:
                    await loc.fill(str(val), timeout=DEFAULT_TIMEOUT_MS)
                results.append({"key": key, "ok": True, "selector": sel})
            except Exception as e:
                results.append({"key": key, "ok": False, "reason": str(e)[:200]})
    ok = sum(1 for r in results if r["ok"])
    print(f"Filled {ok}/{len(results)} fields.")
    for r in results:
        marker = "OK " if r["ok"] else "ERR"
        if r["ok"]:
            print(f"  [{marker}] {r['key']!r} -> {r['selector']}")
        else:
            print(f"  [{marker}] {r['key']!r}: {r['reason']}")


# ─── Navigation ──────────────────────────────────────────────────────────────

async def cmd_goto(args, opts):
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb goto <url> [--wait load|domcontentloaded|networkidle]")
    url = pos[0]
    wait = opts.get("wait", "domcontentloaded")
    timeout = opt_int(opts, "timeout", 30000)
    async with session() as (_, page):
        await page.goto(url, wait_until=wait, timeout=timeout)
        print(f"URL:   {page.url}")
        print(f"Title: {await page.title()}")


async def cmd_back(args, opts):
    async with session() as (_, page):
        await page.go_back(wait_until="domcontentloaded")
        print(f"URL: {page.url}")


async def cmd_forward(args, opts):
    async with session() as (_, page):
        await page.go_forward(wait_until="domcontentloaded")
        print(f"URL: {page.url}")


async def cmd_reload(args, opts):
    async with session() as (_, page):
        await page.reload(wait_until="domcontentloaded")
        print(f"URL: {page.url}")


# ─── Waits ───────────────────────────────────────────────────────────────────

async def cmd_wait(args, opts):
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb wait <selector|#N> [timeout_ms]")
    sel, _ = resolve_selector(pos[0])
    timeout = int(pos[1]) if len(pos) > 1 else opt_int(opts, "timeout", 15000)
    async with session() as (_, page):
        root = await get_action_root(page)
        await root.wait_for_selector(sel, timeout=timeout, state="visible")
    print(f"Visible: {sel}")


async def cmd_wait_hidden(args, opts):
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb wait_hidden <selector|#N> [timeout_ms]")
    sel, _ = resolve_selector(pos[0])
    timeout = int(pos[1]) if len(pos) > 1 else opt_int(opts, "timeout", 15000)
    async with session() as (_, page):
        root = await get_action_root(page)
        await root.wait_for_selector(sel, timeout=timeout, state="hidden")
    print(f"Hidden: {sel}")


async def cmd_wait_url(args, opts):
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb wait_url <regex_or_substring> [timeout_ms]")
    pat = pos[0]
    timeout = int(pos[1]) if len(pos) > 1 else opt_int(opts, "timeout", 15000)
    async with session() as (_, page):
        try:
            rx = re.compile(pat)
            await page.wait_for_url(rx, timeout=timeout)
        except re.error:
            await page.wait_for_url(lambda u: pat in u, timeout=timeout)
    print(f"URL match: {page.url}")


async def cmd_wait_text(args, opts):
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb wait_text <text> [timeout_ms]")
    text = pos[0]
    timeout = int(pos[1]) if len(pos) > 1 else opt_int(opts, "timeout", 15000)
    async with session() as (_, page):
        root = await get_action_root(page)
        await root.get_by_text(text, exact=False).first.wait_for(timeout=timeout, state="visible")
    print(f"Found text: {text!r}")


async def cmd_wait_idle(args, opts):
    pos, opts = split_flags(args)
    timeout = int(pos[0]) if pos else opt_int(opts, "timeout", 15000)
    async with session() as (_, page):
        await page.wait_for_load_state("networkidle", timeout=timeout)
    print("Network idle")


async def cmd_wait_js(args, opts):
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb wait_js <expr> [timeout_ms]   e.g. 'document.readyState === \"complete\"'")
    expr = pos[0]
    timeout = int(pos[1]) if len(pos) > 1 else opt_int(opts, "timeout", 15000)
    async with session() as (_, page):
        await page.wait_for_function(expr, timeout=timeout)
    print(f"Truthy: {expr}")


async def cmd_sleep(args, opts):
    pos, _ = split_flags(args)
    ms = int(pos[0]) if pos else 1000
    await asyncio.sleep(ms / 1000)
    print(f"Slept {ms}ms")


# ─── Tabs ────────────────────────────────────────────────────────────────────

async def cmd_tabs(args, opts):
    async with browser_session() as browser:
        for ci, ctx in enumerate(browser.contexts):
            for pi, page in enumerate(ctx.pages):
                title = await page.title()
                print(f"[{ci}:{pi}] {title}")
                print(f"        {page.url}")


async def cmd_tab(args, opts):
    pos, _ = split_flags(args)
    if not pos:
        die("Usage: cb tab <index>  (e.g. '0:1' or '1')")
    idx = pos[0]
    async with browser_session() as browser:
        if ":" in idx:
            ci, pi = map(int, idx.split(":"))
            page = browser.contexts[ci].pages[pi]
        else:
            page = browser.contexts[0].pages[int(idx)]
        await page.bring_to_front()
        print(f"Switched: {await page.title()}")
        print(f"URL: {page.url}")


async def cmd_new_tab(args, opts):
    pos, _ = split_flags(args)
    url = pos[0] if pos else "about:blank"
    async with browser_session() as browser:
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()
        if url != "about:blank":
            await page.goto(url, wait_until="domcontentloaded")
        await page.bring_to_front()
        print(f"Opened tab: {page.url}")


async def cmd_close_tab(args, opts):
    pos, _ = split_flags(args)
    async with browser_session() as browser:
        if pos:
            idx = pos[0]
            if ":" in idx:
                ci, pi = map(int, idx.split(":"))
                page = browser.contexts[ci].pages[pi]
            else:
                page = browser.contexts[0].pages[int(idx)]
        else:
            page = await get_active_page(browser)
        url = page.url
        await page.close()
        print(f"Closed: {url}")


# ─── Frames ──────────────────────────────────────────────────────────────────

async def cmd_frames(args, opts):
    async with session() as (_, page):
        frames = page.frames
        for i, f in enumerate(frames):
            print(f"[{i}] name={f.name!r}  url={f.url}")


async def cmd_frame(args, opts):
    pos, _ = split_flags(args)
    if not pos:
        die("Usage: cb frame <selector|#N>  (action commands will scope to this frame)")
    sel, _ = resolve_selector(pos[0])
    save_active_frame(sel)
    print(f"Active frame set: {sel}")


async def cmd_frame_reset(args, opts):
    save_active_frame(None)
    print("Active frame cleared (back to top frame)")


# ─── Sessions / cookies ──────────────────────────────────────────────────────

async def cmd_cookies(args, opts):
    pos, _ = split_flags(args)
    async with browser_session() as browser:
        all_cookies = []
        for ctx in browser.contexts:
            all_cookies.extend(await ctx.cookies())
        if pos:
            name = pos[0]
            all_cookies = [c for c in all_cookies if c.get("name") == name]
        print(json.dumps(all_cookies, indent=2, ensure_ascii=False))


async def cmd_cookies_save(args, opts):
    pos, _ = split_flags(args)
    if not pos:
        die("Usage: cb cookies_save <file>")
    path = pos[0]
    async with browser_session() as browser:
        all_cookies = []
        for ctx in browser.contexts:
            all_cookies.extend(await ctx.cookies())
        Path(path).write_text(json.dumps(all_cookies, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Saved {len(all_cookies)} cookies to {path}")


async def cmd_cookies_load(args, opts):
    pos, _ = split_flags(args)
    if not pos:
        die("Usage: cb cookies_load <file>")
    path = pos[0]
    cookies = json.loads(Path(path).read_text(encoding="utf-8"))
    async with browser_session() as browser:
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        await ctx.add_cookies(cookies)
        print(f"Loaded {len(cookies)} cookies from {path}")


async def cmd_storage(args, opts):
    pos, _ = split_flags(args)
    async with session() as (_, page):
        if pos:
            key = pos[0]
            v = await page.evaluate(f"localStorage.getItem({json.dumps(key)})")
            print(v)
        else:
            data = await page.evaluate("Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)]))")
            print(json.dumps(data, indent=2, ensure_ascii=False))


# ─── Batch script runner ─────────────────────────────────────────────────────

async def cmd_batch(args, opts):
    """Run a YAML or JSON script of actions in one CDP session.

    Script format (YAML or JSON):

      # array of single-key dicts; key is command name, value is args (string or list)
      - goto: https://example.com
      - wait_idle: 5000
      - snap: {}
      - fill_form:
          Email: foo@bar.com
          Name: Alex
      - click: "#submit-button"
      - wait_url: dashboard
    """
    pos, opts = split_flags(args)
    if not pos:
        die("Usage: cb batch <script.yaml|script.json> [--continue]")
    path = pos[0]
    keep_going = bool(opts.get("continue", False))
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith(".json"):
        script = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError:
            die("YAML scripts require PyYAML: pip install pyyaml. Use .json instead.")
        script = yaml.safe_load(text)
    if not isinstance(script, list):
        die("Batch script must be a list of {command: args} entries.")

    async with shared_session():
        for i, step in enumerate(script):
            if not isinstance(step, dict) or len(step) != 1:
                die(f"Step {i}: each entry must be a single-key dict. Got: {step!r}")
            (name, raw_args), = step.items()
            handler = COMMANDS.get(name)
            if not handler:
                die(f"Step {i}: unknown command {name!r}. Allowed: {', '.join(sorted(COMMANDS))}")
            # Coerce args
            if raw_args is None:
                raw_args = []
            elif isinstance(raw_args, str):
                raw_args = shlex.split(raw_args, posix=False)
            elif isinstance(raw_args, dict):
                # Form-style: pass the dict as inline JSON to commands like fill_form
                raw_args = [json.dumps(raw_args, ensure_ascii=False)]
            elif not isinstance(raw_args, list):
                raw_args = [str(raw_args)]
            else:
                raw_args = [str(a) for a in raw_args]
            print(f"[{i}] {name} {' '.join(raw_args)}")
            try:
                await handler(raw_args, {})
            except Exception as e:
                if keep_going:
                    print(f"  WARN step {i} ({name}) failed: {e}", file=sys.stderr)
                    continue
                die(f"Step {i} ({name}) failed: {e}")
    print("Batch complete.")


# ─── Help ────────────────────────────────────────────────────────────────────

async def cmd_help(args, opts):
    pos, _ = split_flags(args)
    if not pos:
        print(__doc__)
        return
    cmd = pos[0]
    fn = COMMANDS.get(cmd)
    if not fn:
        die(f"No such command: {cmd}")
    doc = (fn.__doc__ or "(no description)").strip()
    print(f"cb {cmd}\n  {doc}")


# ─── Command registry ────────────────────────────────────────────────────────

COMMANDS = {
    # connection
    "launch": cmd_launch,
    "kill": cmd_kill,
    "connect": cmd_connect,
    "title": cmd_title,
    # nav
    "goto": cmd_goto,
    "back": cmd_back,
    "forward": cmd_forward,
    "reload": cmd_reload,
    # inspection
    "screenshot": cmd_screenshot,
    "snap": cmd_snap,
    "html": cmd_html,
    "text": cmd_text,
    "links": cmd_links,
    "forms": cmd_forms,
    "ax": cmd_ax,
    "eval": cmd_eval,
    # actions
    "click": cmd_click,
    "dblclick": cmd_dblclick,
    "fill": cmd_fill,
    "clear": cmd_clear,
    "check": cmd_check,
    "uncheck": cmd_uncheck,
    "select": cmd_select,
    "upload": cmd_upload,
    "hover": cmd_hover,
    "focus": cmd_focus,
    "blur": cmd_blur,
    "press": cmd_press,
    "keys": cmd_keys,
    "type": cmd_type,
    "scroll": cmd_scroll,
    "coord_click": cmd_coord_click,
    "coord_move": cmd_coord_move,
    "drag": cmd_drag,
    # smart
    "act": cmd_act,
    "find": cmd_find,
    "fill_form": cmd_fill_form,
    # waits
    "wait": cmd_wait,
    "wait_hidden": cmd_wait_hidden,
    "wait_url": cmd_wait_url,
    "wait_text": cmd_wait_text,
    "wait_idle": cmd_wait_idle,
    "wait_js": cmd_wait_js,
    "sleep": cmd_sleep,
    # tabs
    "tabs": cmd_tabs,
    "tab": cmd_tab,
    "new_tab": cmd_new_tab,
    "close_tab": cmd_close_tab,
    # frames
    "frames": cmd_frames,
    "frame": cmd_frame,
    "frame_reset": cmd_frame_reset,
    # sessions
    "cookies": cmd_cookies,
    "cookies_save": cmd_cookies_save,
    "cookies_load": cmd_cookies_load,
    "storage": cmd_storage,
    # batch
    "batch": cmd_batch,
    "help": cmd_help,
}


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print(f"\nCommands: {', '.join(sorted(COMMANDS.keys()))}")
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd in ("-h", "--help", "help"):
        rest = sys.argv[2:]
        await cmd_help(rest, {})
        return
    if cmd not in COMMANDS:
        # suggest close matches
        from difflib import get_close_matches
        close = get_close_matches(cmd, COMMANDS.keys(), n=3)
        msg = f"Unknown command: {cmd}"
        if close:
            msg += f"\nDid you mean: {', '.join(close)}?"
        die(msg)
    args = sys.argv[2:]
    try:
        await COMMANDS[cmd](args, {})
    except CBError as e:
        die(str(e))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
