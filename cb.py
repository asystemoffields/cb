#!/usr/bin/env python3
"""cb — Claude Browser. A Chromium harness built for LLM operators.

Run `cb help` for the command list, `cb help <command>` for details.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

VERSION = "2.0.0"
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

try:  # Windows consoles default to a legacy code page; page text is UTF-8.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ─── Errors ──────────────────────────────────────────────────────────────────

class CBError(Exception):
    """A user-facing failure: printed as `ERROR: <msg>` with exit code 1."""


class UsageError(CBError):
    """Bad arguments: printed with the command's usage, exit code 2."""


# ─── Paths & config (cheap; used by the thin client too) ─────────────────────

def _env(name: str, default: str | None = None) -> str | None:
    ctx = _current_ctx()
    if ctx is not None and name in ctx.env:
        return ctx.env[name]
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def home_dir():
    from pathlib import Path
    return Path(os.environ.get("CB_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".cb"))


def session_name() -> str:
    return os.environ.get("CB_SESSION") or "default"


def session_dir():
    d = home_dir()
    name = session_name()
    if name != "default":
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise CBError(f"Invalid session name {name!r} (use letters, digits, _ . -)")
        d = d / "sessions" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _write_json(path, data) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader has the file open for a moment
            if attempt == 4:
                raise
            time.sleep(0.05)


def code_version() -> str:
    """Changes whenever cb.py changes, so a stale daemon gets replaced."""
    try:
        st = os.stat(__file__)
        return f"{VERSION}-{st.st_mtime_ns}-{st.st_size}"
    except OSError:
        return VERSION


# ─── Thin client: forwards argv to the session daemon ────────────────────────
#
# The daemon holds one CDP connection (plus console/network/dialog listeners)
# for the whole session, so each command costs a local socket round trip
# instead of a Python + Playwright startup and CDP attach.

LOCAL_COMMANDS = {"help", "version", "daemon"}


def _daemon_connect(info: dict, timeout: float = 1.0):
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", int(info["port"])), timeout=timeout)
        s.settimeout(None)
        return s
    except (OSError, KeyError, ValueError, TypeError):
        return None


def _daemon_request(sock, info: dict, payload: dict) -> int:
    payload = dict(payload, token=info.get("token"))
    sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    f = sock.makefile("r", encoding="utf-8", newline="\n")
    code = 1
    for line in f:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            if "o" in msg:
                sys.stdout.write(msg["o"])
                sys.stdout.flush()
            elif "e" in msg:
                sys.stderr.write(msg["e"])
                sys.stderr.flush()
        except BrokenPipeError:  # e.g. `cb snap | head`: stop quietly
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            sock.close()
            return 0
        if "x" in msg:
            code = int(msg["x"])
            break
    try:
        sock.close()
    except OSError:
        pass
    return code


def _daemon_stop(info: dict) -> bool:
    s = _daemon_connect(info)
    if not s:
        return False
    try:
        _daemon_request(s, info, {"argv": ["__shutdown__"]})
    except OSError:
        pass
    return True


def _spawn_daemon(sd) -> None:
    import subprocess
    log = open(sd / "daemon.log", "ab")
    cmd = [sys.executable, os.path.abspath(__file__), "__daemon__"]
    kw: dict = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, env=os.environ.copy())
    if IS_WINDOWS:
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)
    log.close()


def _ensure_daemon(sd):
    """Return (socket, info) for a live daemon of the current code version."""
    info_path = sd / "daemon.json"
    info = _read_json(info_path)
    if info:
        if info.get("version") != code_version():
            _daemon_stop(info)
            time.sleep(0.2)
        else:
            s = _daemon_connect(info)
            if s:
                return s, info
    lock = sd / "daemon.spawn"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        owner = True
    except FileExistsError:
        owner = time.time() - os.path.getmtime(lock) > 15  # stale lock
        if owner:
            lock.touch()
    try:
        if owner:
            try:
                info_path.unlink()
            except FileNotFoundError:
                pass
            _spawn_daemon(sd)
        deadline = time.time() + 20
        while time.time() < deadline:
            info = _read_json(info_path)
            if info.get("version") == code_version():
                s = _daemon_connect(info)
                if s:
                    return s, info
            time.sleep(0.03)
    finally:
        if owner:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
    raise CBError(f"cb daemon did not start; see {sd / 'daemon.log'} (or set CB_DAEMON=0)")


def _wants_stdin(argv: list[str]) -> bool:
    if sys.stdin is None or sys.stdin.isatty():
        return False
    if not argv:
        return False
    cmd = ALIASES.get(argv[0], argv[0]) if "ALIASES" in globals() else argv[0]
    return "-" in argv[1:] or (cmd in ("batch",) and len(argv) == 1)


def client_main(argv: list[str]) -> int:
    sd = session_dir()
    stdin = sys.stdin.read() if _wants_stdin(argv) else None
    sock, info = _ensure_daemon(sd)
    env = {k: v for k, v in os.environ.items() if k.startswith("CB_")}
    try:
        return _daemon_request(sock, info, {"argv": argv, "cwd": os.getcwd(), "env": env, "stdin": stdin})
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        raise CBError(f"lost connection to cb daemon: {e}")


# ─── Per-invocation context ──────────────────────────────────────────────────

_CTX_VAR = None


def _current_ctx():
    if _CTX_VAR is None:
        return None
    return _CTX_VAR.get(None)


class Ctx:
    """Output sink + cwd/env for one command invocation (local or via daemon)."""

    def __init__(self, write, cwd: str | None = None, env: dict | None = None, stdin: str | None = None):
        self._write = write
        self.cwd = cwd or os.getcwd()
        self.env = env or {}
        self.stdin = stdin
        self.batch = False          # inside `cb batch`: defer auto-snapshots to the end
        self.pending_observe = None  # "before" state captured by the first deferred action
        self.echoed: set = set()     # refs whose new value a batch step already reported
        self.no_snap = False
        self.exit_daemon = False

    def out(self, s: str = "") -> None:
        self._write("o", s + "\n")

    def err(self, s: str) -> None:
        self._write("e", s + "\n")

    def note(self, s: str) -> None:
        self._write("e", f"cb: {s}\n")

    def path(self, p: str):
        from pathlib import Path
        q = Path(os.path.expanduser(p))
        return q if q.is_absolute() else Path(self.cwd) / q


# ─── Heavy section: everything below runs in the daemon (or with CB_DAEMON=0) ─

class _LazyModule:
    """Defers an import until first use, so the thin client never pays for it."""

    def __init__(self, name: str):
        self._name = name
        self._mod = None

    def __getattr__(self, attr):
        if self._mod is None:
            import importlib
            self._mod = importlib.import_module(self._name)
        return getattr(self._mod, attr)


asyncio = _LazyModule("asyncio")
difflib = _LazyModule("difflib")
shlex = _LazyModule("shlex")
shutil = _LazyModule("shutil")
subprocess = _LazyModule("subprocess")

import contextvars  # noqa: E402
import zlib  # noqa: E402
from collections import deque  # noqa: E402
from pathlib import Path  # noqa: E402

_CTX_VAR = contextvars.ContextVar("cb_ctx")

# Page-side library, injected once per document as window.__cb. It builds the outline,
# stamps stable data-cb-ref attributes on elements, scores text matches, scrolls, and
# draws the annotation overlay.
_PAGE_JS_TEMPLATE = r"""
(() => {
const V = '__CB_VERSION__';
if (window.__cb && window.__cb.v === V) return;
const REF = 'data-cb-ref';
const SKIP = new Set(['script', 'style', 'noscript', 'template', 'head', 'meta', 'link', 'base', 'title',
  'param', 'source', 'track', 'datalist', 'option', 'optgroup', 'br', 'wbr', 'map', 'area', 'math']);
const INTER = new Set(['button', 'link', 'textbox', 'searchbox', 'checkbox', 'radio', 'combobox', 'listbox',
  'option', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'tab', 'switch', 'slider', 'spinbutton', 'treeitem',
  'select', 'file', 'input']);
const CHECKABLE = new Set(['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio']);
const VALUED = new Set(['textbox', 'searchbox', 'combobox', 'slider', 'spinbutton', 'select', 'listbox', 'file', 'input']);
const LANDMARK = {navigation: 'nav', main: 'main', banner: 'header', contentinfo: 'footer', complementary: 'aside',
  form: 'form', search: 'search', dialog: 'dialog', alertdialog: 'dialog', alert: 'alert', status: 'status',
  region: 'region', menu: 'menu', menubar: 'menubar', tablist: 'tablist', tree: 'tree', toolbar: 'toolbar',
  group: 'group', grid: 'grid', table: 'table'};
const NAMED_ONLY = new Set(['region', 'group', 'table', 'grid', 'status', 'toolbar']);
const TEXT_INPUTS = new Set(['', 'text', 'search', 'email', 'url', 'tel', 'password', 'number', 'date',
  'datetime-local', 'month', 'week', 'time', 'color']);

const norm = s => (s || '').replace(/[\s ​]+/g, ' ').trim();
const cut = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
const q = s => '"' + s.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';

function parentOf(el) {
  if (el.assignedSlot) return el.assignedSlot;
  if (el.parentElement) return el.parentElement;
  const root = el.getRootNode && el.getRootNode();
  return root && root.host ? root.host : null;
}

function deepActive() {
  let a = document.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  return a;
}

function kidsOf(el) {
  if (el.shadowRoot) return el.shadowRoot.childNodes;
  if (el.localName === 'slot') {
    const a = el.assignedNodes({flatten: true});
    return a.length ? a : el.childNodes;
  }
  return el.childNodes;
}

// Text of a subtree across open shadow roots (used when innerText misses shadow content).
function composedText(el, max) {
  let out = '';
  const visit = n => {
    if (out.length > max) return;
    if (n.nodeType === 3) { out += n.data; return; }
    if (n.nodeType !== 1) return;
    if (SKIP.has(n.localName) || n.localName === 'svg') return;
    for (const k of kidsOf(n)) visit(k);
    if (n.localName === 'img' && n.alt) out += ' ' + n.alt;
  };
  visit(el);
  return out;
}

function textOf(el, max = 200) {
  let t = el.innerText;
  if (el.shadowRoot || !t || !t.trim()) {
    const c = composedText(el, max);
    if (norm(c).length > norm(t || '').length) t = c;
  }
  return norm(t || '');
}

function textExcluding(root, skip) {
  let out = '';
  const visit = n => {
    if (n === skip) return;
    if (n.nodeType === 3) { out += n.data; return; }
    if (n.nodeType !== 1 || SKIP.has(n.localName)) return;
    for (const k of kidsOf(n)) visit(k);
  };
  visit(root);
  return norm(out);
}

function inSectioning(el) {
  for (let p = parentOf(el); p; p = parentOf(p)) {
    if (['article', 'aside', 'main', 'nav', 'section'].includes(p.localName)) return true;
  }
  return false;
}

function implicitRole(el, tag) {
  switch (tag) {
    case 'a': return el.hasAttribute('href') ? 'link' : '';
    case 'button': return 'button';
    case 'select': return (el.multiple || el.size > 1) ? 'listbox' : 'select';
    case 'textarea': return 'textbox';
    case 'input': {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'hidden') return '';
      if (t === 'button' || t === 'submit' || t === 'reset' || t === 'image') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'range') return 'slider';
      if (t === 'number') return 'spinbutton';
      if (t === 'file') return 'file';
      if (t === 'search') return el.list ? 'combobox' : 'searchbox';
      if (TEXT_INPUTS.has(t)) return el.list ? 'combobox' : 'textbox';
      return 'input';
    }
    case 'summary': return 'button';
    case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6': return 'heading';
    case 'nav': return 'navigation';
    case 'main': return 'main';
    case 'aside': return 'complementary';
    case 'form': return 'form';
    case 'search': return 'search';
    case 'dialog': return 'dialog';
    case 'header': return inSectioning(el) ? '' : 'banner';
    case 'footer': return inSectioning(el) ? '' : 'contentinfo';
    case 'section': return 'region';
    case 'fieldset': return 'group';
    case 'img': return 'img';
    case 'li': return 'listitem';
    case 'tr': return 'row';
    case 'table': return 'table';
    case 'td': return 'cell';
    case 'th': return 'columnheader';
  }
  return '';
}

function roleOf(el, tag) {
  const r = (el.getAttribute('role') || '').trim().split(/\s+/)[0].toLowerCase();
  if (r && r !== 'presentation' && r !== 'none' && r !== 'generic') return r;
  if (el.isContentEditable && !(el.parentElement && el.parentElement.isContentEditable)) return 'textbox';
  return implicitRole(el, tag);
}

function byIds(el, ids) {
  const root = el.getRootNode();
  return ids.split(/\s+/).map(id => {
    const t = (root.getElementById && root.getElementById(id)) || document.getElementById(id);
    return t ? textOf(t, 200) : '';
  }).join(' ');
}

function nameOf(el, tag, role) {
  tag = tag || el.localName;
  const lb = el.getAttribute('aria-labelledby');
  if (lb) { const s = norm(byIds(el, lb)); if (s) return s; }
  const al = norm(el.getAttribute('aria-label'));
  if (al) return al;
  if (tag === 'input' || tag === 'textarea' || tag === 'select') {
    const t = (el.type || '').toLowerCase();
    if (t === 'button' || t === 'submit' || t === 'reset') return norm(el.value) || (t === 'submit' ? 'Submit' : t === 'reset' ? 'Reset' : '');
    if (t === 'image') return norm(el.alt || el.value) || 'Submit';
    if (el.labels && el.labels.length) {
      const s = norm(Array.from(el.labels).map(l => textExcluding(l, el)).join(' '));
      if (s) return s;
    }
    const wrap = el.closest && el.closest('label');
    if (wrap) { const s = textExcluding(wrap, el); if (s) return s; }
    return norm(el.placeholder || el.title || el.getAttribute('aria-placeholder') || '');
  }
  if (tag === 'img') return norm(el.alt || el.title);
  if (tag === 'iframe' || tag === 'frame') return norm(el.title || el.name);
  let t = textOf(el, 300);
  if (t) return t;
  if (el.title) return norm(el.title);
  const icon = el.querySelector && el.querySelector('img[alt]:not([alt=""]), [aria-label], svg title, [title]');
  if (icon) return norm(icon.getAttribute('alt') || icon.getAttribute('aria-label') || icon.getAttribute('title') || icon.textContent);
  return '';
}

function shortUrl(href) {
  try {
    const u = new URL(href, location.href);
    if (u.protocol === 'javascript:') return 'javascript:';
    if (u.origin === location.origin) return cut(u.pathname + u.search + u.hash, 90);
    return cut(u.host + (u.pathname === '/' ? '' : u.pathname) + u.search, 90);
  } catch (e) { return cut(String(href), 90); }
}

function valueOf(el, tag, role) {
  if (tag === 'select') return Array.from(el.selectedOptions).map(o => norm(o.label || o.text)).join(', ');
  if (tag === 'textarea') return el.value;
  if (tag === 'input') {
    const t = (el.type || '').toLowerCase();
    if (['checkbox', 'radio', 'button', 'submit', 'reset', 'image'].includes(t)) return null;
    if (t === 'password') return el.value ? '•'.repeat(Math.min(el.value.length, 8)) : '';
    if (t === 'file') return Array.from(el.files || []).map(f => f.name).join(', ');
    return el.value;
  }
  if (role === 'textbox' && el.isContentEditable) return el.innerText;
  if (VALUED.has(role)) {
    const vt = el.getAttribute('aria-valuetext') || el.getAttribute('aria-valuenow');
    if (vt) return vt;
    if (role === 'combobox') return textOf(el, 100);
  }
  return null;
}

function statesOf(el, tag, role, active) {
  const s = [];
  const aria = n => el.getAttribute('aria-' + n);
  if (CHECKABLE.has(role)) {
    let c = tag === 'input' ? (el.indeterminate ? 'mixed' : el.checked) : aria('checked');
    s.push(c === true || c === 'true' ? 'checked' : c === 'mixed' ? 'mixed' : 'unchecked');
  }
  if (el.disabled || aria('disabled') === 'true') s.push('disabled');
  const ex = aria('expanded');
  if (ex === 'true') s.push('expanded'); else if (ex === 'false') s.push('collapsed');
  if (tag === 'summary' && el.parentElement && el.parentElement.localName === 'details') s.push(el.parentElement.open ? 'expanded' : 'collapsed');
  if (aria('selected') === 'true') s.push('selected');
  if (aria('pressed') === 'true') s.push('pressed');
  const cur = aria('current');
  if (cur && cur !== 'false') s.push('current');
  if (el.required || aria('required') === 'true') s.push('required');
  if (((tag === 'input' || tag === 'textarea') && el.readOnly) || aria('readonly') === 'true') s.push('readonly');
  let invalid = aria('invalid') === 'true';
  try { invalid = invalid || el.matches(':user-invalid'); } catch (e) {}
  if (invalid) s.push('invalid');
  return s;
}

function isInteractive(el, tag, role, cs, rect, ctx) {
  if (INTER.has(role)) return !(role === 'link' && tag === 'a' && !el.hasAttribute('href') && !el.hasAttribute('role'));
  if (tag === 'label' || tag === 'html' || tag === 'body' || LANDMARK[role]) return false;
  if (ctx.inInter) return false;
  const area = rect.width * rect.height;
  const small = area > 0 && area < innerWidth * innerHeight * 0.4;
  if (el.hasAttribute('onclick')) return small;
  const ti = el.getAttribute('tabindex');
  if (ti !== null && +ti >= 0 && small && el.childElementCount < 12) return true;
  if (cs.cursor === 'pointer' && small) {
    const p = parentOf(el);
    if (!p || getComputedStyle(p).cursor !== 'pointer') return true;
  }
  return false;
}

function landmarkName(el, role) {
  const lb = el.getAttribute('aria-labelledby');
  if (lb) { const s = norm(byIds(el, lb)); if (s) return s; }
  const al = norm(el.getAttribute('aria-label'));
  if (al) return al;
  if (role === 'group' && el.localName === 'fieldset') {
    const lg = el.querySelector('legend');
    if (lg) return textOf(lg, 80);
  }
  if (role === 'dialog' || role === 'alertdialog') {
    const h = el.querySelector('h1,h2,h3,h4,h5,h6,[role=heading]');
    if (h) return textOf(h, 80);
  }
  if (role === 'table' || role === 'grid') {
    const c = el.querySelector('caption');
    if (c) return textOf(c, 80);
  }
  return '';
}

function isBlock(display) {
  return !(display === 'inline' || display === 'contents' || display.startsWith('inline-') || display === 'ruby');
}

const state = {next: 1};

function refFor(el, seen) {
  let r = el.getAttribute(REF);
  if (r && !seen.has(r)) { seen.add(r); return +r; }
  r = String(state.next++);
  el.setAttribute(REF, r);
  seen.add(r);
  return +r;
}

// Walk the composed (flattened) tree and produce outline lines.
// mode: 'snap' (default) | 'read' (readable text, no refs) | 'collect' (interactive candidates only)
function walkPage(opts, scope) {
  opts = opts || {};
  const mode = opts.mode || 'snap';
  const read = mode === 'read';
  const collect = mode === 'collect';
  const textMax = opts.textMax || (read ? 2000 : 240);
  const vw = innerWidth, vh = innerHeight, sx = scrollX, sy = scrollY;
  const lines = [], refs = {}, cands = [], seen = new Set();
  const active = deepActive();
  const maxNodes = opts.maxNodes || 60000;
  if (opts.next) state.next = Math.max(state.next, opts.next);
  let count = 0, truncated = false;
  let buf = '', bufDepth = 0, bufVis = false;
  let bullet = null;

  function push(line) {
    if (bullet !== null && line.d >= bullet) { line.t = '- ' + line.t; bullet = null; }
    lines.push(line);
  }
  function addText(s, depth, vis) {
    if (!s) return;
    if (!buf) bufDepth = depth;
    buf += s;
    bufVis = bufVis || vis;
  }
  function flush() {
    if (!buf) return;
    let s = norm(buf);
    buf = '';
    const vis = bufVis;
    bufVis = false;
    if (!s || collect) return;
    if (!read && !/[\p{L}\p{N}]/u.test(s)) return;
    push({d: bufDepth, k: 't', t: cut(s, textMax), v: vis});
  }

  function emitInteractive(el, tag, role, depth, inView, prefix, ctx) {
    const ref = refFor(el, seen);
    let name = nameOf(el, tag, role);
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : '';
    let label = role || 'clickable';
    let extra = '';
    if (tag === 'label' && el.control) {
      label = 'label';
      const cr = roleOf(el.control, el.control.localName);
      const st = statesOf(el.control, el.control.localName, cr, active);
      extra = ` (for ${cr}${st.length ? ', ' + st.join(', ') : ''})`;
    }
    if (collect) {
      const alt = [el.getAttribute('placeholder'), el.getAttribute('title'), el.getAttribute('name'),
        el.id, el.getAttribute('data-testid')].filter(Boolean).map(norm);
      cands.push({ref, role: label, name, alt, tag, vis: inView,
        disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true')});
      return ref;
    }
    let line = `[${ref}] ${label}`;
    if (name) line += ' ' + q(cut(name, 100));
    else if ((tag === 'input' || tag === 'select' || tag === 'textarea') && el.getAttribute('name')) line += ` name=${q(cut(el.getAttribute('name'), 40))}`;
    if ((role === 'textbox' || role === 'searchbox') && type && type !== 'text' && type !== 'search') line += ` type=${type}`;
    const ph = el.getAttribute && el.getAttribute('placeholder');
    if (ph && norm(ph) !== name && VALUED.has(role)) line += ` placeholder=${q(cut(norm(ph), 60))}`;
    const val = valueOf(el, tag, role);
    if (val !== null && val !== undefined && norm(String(val)) && norm(String(val)) !== name) line += ` = ${q(cut(norm(String(val)), 100))}`;
    if (tag === 'select') {
      const o = Array.from(el.options).map(x => norm(x.label || x.text)).filter(Boolean);
      if (o.length) {
        let shown = [], len = 0;
        for (const x of o) { if (len + x.length > 160 || shown.length >= 12) break; shown.push(x); len += x.length + 3; }
        line += ` options: ${shown.join(' | ')}${o.length > shown.length ? ` | …(+${o.length - shown.length})` : ''}`;
      }
    }
    if (tag === 'input' && type === 'range') line += ` min=${el.min || 0} max=${el.max || 100}`;
    const st = statesOf(el, tag, role, active);
    if (st.length) line += ` (${st.join(', ')})`;
    line += extra;
    if (role === 'link') {
      const href = el.getAttribute('href');
      if (href && (opts.urls || !name)) line += ` -> ${shortUrl(el.href || href)}`;
    }
    push({d: depth, k: 'i', t: (prefix || '') + line, r: ref, v: inView});
    refs[ref] = {role: label, name: cut(name, 100), tag};
    return ref;
  }

  function walkKids(el, depth, ctx) {
    for (const n of kidsOf(el)) {
      if (truncated) return;
      if (n.nodeType === 3) { if (!ctx.noText) addText(n.data, depth, ctx.vis); }
      else if (n.nodeType === 1) walk(n, depth, ctx);
    }
  }

  function walk(el, depth, ctx) {
    if (++count > maxNodes) { truncated = true; return; }
    const tag = el.localName;
    if (SKIP.has(tag)) return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse' || cs.contentVisibility === 'hidden') return;
    const isCtl = tag === 'input' || tag === 'select' || tag === 'textarea';
    if (!isCtl && parseFloat(cs.opacity) === 0) return;
    const contents = cs.display === 'contents';
    const rect = el.getBoundingClientRect();
    if (!contents && !isCtl && (rect.width < 2 || rect.height < 2) &&
        (cs.overflowX !== 'visible' || cs.overflowY !== 'visible' || cs.clipPath !== 'none' || cs.clip !== 'auto')) return;
    if (!contents && (rect.right + sx < 0 || rect.bottom + sy < 0)) return;
    const inView = contents ? ctx.vis : (rect.bottom > 0 && rect.top < vh && rect.right > 0 && rect.left < vw);
    if (opts.viewport && !contents && !inView && !(rect.width === 0 && rect.height === 0)) return;
    const role = roleOf(el, tag);
    if (!read && (tag === 'legend' || tag === 'caption')) return;  // already the group/table name

    if (tag === 'iframe' || tag === 'frame') {
      if (read || collect || rect.width < 10 || rect.height < 10) return;
      flush();
      const ref = refFor(el, seen);
      const nm = norm(el.title || el.name || el.getAttribute('aria-label') || '') || (el.src ? shortUrl(el.src) : '');
      push({d: depth, k: 'f', t: `[${ref}] iframe${nm ? ' ' + q(cut(nm, 80)) : ''}:`, r: ref, v: inView});
      refs[ref] = {role: 'iframe', name: nm, tag};
      return;
    }

    let inter = isInteractive(el, tag, role, cs, rect, ctx);
    // A label whose control is hidden (custom checkbox / file button) acts as the control.
    if (!inter && tag === 'label' && el.control && !ctx.inInter) {
      const c = el.control;
      const ccs = getComputedStyle(c);
      if (ccs.display === 'none' || ccs.visibility === 'hidden' || !c.getClientRects().length) inter = true;
    }
    if (inter) {
      if (read) {
        if (role === 'link' && !ctx.noText) {
          if (opts.links && el.href) {
            addText(` [${norm(el.innerText) || nameOf(el, tag, role)}](${el.href}) `, depth, inView);
            return;
          }
          walkKids(el, depth, ctx);
          return;
        }
        if (['button', 'textbox', 'searchbox', 'checkbox', 'radio', 'switch', 'slider', 'spinbutton', 'select',
             'listbox', 'combobox', 'file', 'input'].includes(role) || tag === 'label') return;
        const block = isBlock(cs.display) && !ctx.inline;
        if (block) flush();
        walkKids(el, depth, {...ctx, vis: inView});
        if (block) flush();
        return;
      }
      flush();
      emitInteractive(el, tag, role, depth, inView, '', ctx);
      walkKids(el, depth + 1, {...ctx, noText: true, inInter: true, vis: inView});
      return;
    }

    if (tag === 'svg' || tag === 'canvas' || tag === 'video' || tag === 'audio' || tag === 'img' || role === 'img') {
      if (ctx.noText || collect) return;
      if (tag === 'canvas' || tag === 'video' || tag === 'audio') {
        if (read || rect.width < 40 || rect.height < 20) return;
        flush();
        const nm = norm(el.getAttribute('aria-label') || el.title || '');
        push({d: depth, k: 'm', t: `${tag}${nm ? ' ' + q(cut(nm, 60)) : ''} ${Math.round(rect.width)}x${Math.round(rect.height)}`, v: inView});
        return;
      }
      if (tag === 'svg' && role !== 'img') return;
      const alt = norm(el.getAttribute('alt') || el.getAttribute('aria-label') || el.getAttribute('title') || '');
      if (!alt || rect.width < 16 || rect.height < 16) return;
      if (read) { if (opts.images) addText(` ![${alt}] `, depth, inView); return; }
      flush();
      push({d: depth, k: 'm', t: `img ${q(cut(alt, 80))}`, v: inView});
      return;
    }

    if (role === 'heading' && !ctx.noText) {
      flush();
      const level = Math.min(6, Math.max(1, +(el.getAttribute('aria-level') || (/^h\d$/.test(tag) ? tag[1] : 2))));
      const text = cut(textOf(el, 300), 200);
      const hashes = '#'.repeat(level);
      if (!text) { walkKids(el, depth, {...ctx, noText: true, vis: inView}); return; }
      if (read) { push({d: depth, k: 'h', t: `${hashes} ${text}`, v: inView}); return; }
      if (!collect) {
        const kids = el.querySelectorAll('a[href],button,[role=link],[role=button]');
        if (kids.length === 1 && norm(nameOf(kids[0])) === text) {
          const k = kids[0];
          emitInteractive(k, k.localName, roleOf(k, k.localName), depth, inView, hashes + ' ', ctx);
          return;
        }
        push({d: depth, k: 'h', t: `${hashes} ${text}`, v: inView});
      }
      walkKids(el, depth + 1, {...ctx, noText: true, vis: inView});
      return;
    }

    const lm = LANDMARK[role];
    if (lm && !ctx.noText && !read && !collect) {
      const nm = landmarkName(el, role);
      if (!(NAMED_ONLY.has(role) && !nm) || role === 'status') {
        flush();
        let t = lm + (nm ? ' ' + q(cut(nm, 60)) : '');
        if (lm === 'dialog') {
          let modal = el.getAttribute('aria-modal') === 'true';
          try { modal = modal || el.matches(':modal'); } catch (e) {}
          if (modal) t += ' (modal)';
        }
        const at = lines.length;
        push({d: depth, k: 's', t: t + ':', v: inView});
        walkKids(el, depth + 1, {...ctx, vis: inView, inLi: false, inline: false});
        flush();
        if (lines.length === at + 1) lines.pop();
        return;
      }
    }

    if (tag === 'li' || role === 'listitem') {
      flush();
      bullet = depth;
      walkKids(el, depth, {...ctx, vis: inView, inLi: true, inline: false});
      flush();
      bullet = null;
      return;
    }
    if ((tag === 'ul' || tag === 'ol') && ctx.inLi) {
      flush();
      walkKids(el, depth + 1, {...ctx, vis: inView, inLi: false});
      flush();
      return;
    }
    if (tag === 'tr' || role === 'row') {
      flush();
      walkKids(el, depth, {...ctx, vis: inView, inRow: true, inline: false});
      flush();
      return;
    }
    if (ctx.inRow && (tag === 'td' || tag === 'th' || role === 'cell' || role === 'gridcell' || role === 'columnheader')) {
      if (buf && !ctx.noText) addText(' | ', depth, inView);
      walkKids(el, depth, {...ctx, vis: inView, inRow: false, inline: true});
      return;
    }
    // Labels attached to a rendered control: the control's line carries the text.
    if (tag === 'label' && el.control && !ctx.noText) {
      walkKids(el, depth, {...ctx, vis: inView, noText: true});
      return;
    }

    const block = !ctx.inline && isBlock(cs.display);
    if (block) flush();
    else if (buf && !ctx.noText && (cs.display.startsWith('inline-') )) addText(' ', depth, inView);
    walkKids(el, depth, {...ctx, vis: inView});
    if (block) flush();
    else if (buf && !ctx.noText && cs.display.startsWith('inline-')) addText(' ', depth, inView);
  }

  let root = scope;
  if (!root && read && !opts.all) {
    const m = document.querySelector('main, [role=main]');
    const arts = document.querySelectorAll('article');
    if (m && norm(m.innerText).length > 200) root = m;
    else if (arts.length === 1 && norm(arts[0].innerText).length > 200) root = arts[0];
  }
  root = root || document.body || document.documentElement;
  const ctx = {vis: true};
  if (root) {
    if (scope && scope.nodeType === 1) walk(root, 0, ctx); else walkKids(root, 0, ctx);
  }
  flush();
  const de = document.documentElement;
  return {
    lines, refs, cands, next: state.next, truncated,
    blocker: (!scope && mode === 'snap') ? blocker() : null,
    url: location.href, title: document.title,
    vw, vh, sx: Math.round(sx), sy: Math.round(sy),
    sw: de ? de.scrollWidth : 0, sh: Math.max(de ? de.scrollHeight : 0, document.body ? document.body.scrollHeight : 0),
  };
}

// A large fixed/absolute element that receives clicks over most of the viewport (modal backdrop,
// cookie wall, loading veil). App shells are excluded: they contain most of the page.
function blocker() {
  const pts = [[0.5, 0.5], [0.2, 0.2], [0.8, 0.2], [0.2, 0.8], [0.8, 0.8]];
  const hits = new Map();
  const vpArea = innerWidth * innerHeight;
  for (const [fx, fy] of pts) {
    const e = document.elementFromPoint(innerWidth * fx, innerHeight * fy);
    if (!e || e === document.body || e === document.documentElement) continue;
    const cs = getComputedStyle(e);
    if (cs.position !== 'fixed' && cs.position !== 'absolute') continue;
    const r = e.getBoundingClientRect();
    if (r.width * r.height < vpArea * 0.6) continue;
    hits.set(e, (hits.get(e) || 0) + 1);
  }
  const total = document.body ? document.body.getElementsByTagName('*').length : 0;
  for (const [e, n] of hits) {
    if (n < 4 || e.getElementsByTagName('*').length > total * 0.5) continue;
    let d = e.localName + (e.id ? '#' + e.id : '') +
      (typeof e.className === 'string' && e.className.trim() ? '.' + e.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
    const t = textOf(e, 80);
    return d + (t ? ' ' + q(cut(t, 60)) : '');
  }
  return null;
}

const ROLE_PREF = {
  click: null,
  fill: ['textbox', 'searchbox', 'combobox', 'spinbutton', 'select', 'listbox', 'slider', 'input'],
  check: ['checkbox', 'radio', 'switch', 'menuitemcheckbox', 'menuitemradio', 'label'],
  select: ['select', 'listbox', 'combobox', 'option', 'radio', 'menuitem', 'tab'],
  upload: ['file', 'button', 'label', 'link'],
};

function scoreText(qn, s) {
  if (!s) return 0;
  const n = norm(s).toLowerCase();
  if (!n) return 0;
  if (n === qn) return 100;
  if (n.startsWith(qn)) return 88 - Math.min(18, (n.length - qn.length) / 4);
  const re = new RegExp('(^|[^\\p{L}\\p{N}])' + qn.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '($|[^\\p{L}\\p{N}])', 'u');
  if (re.test(n)) return 78 - Math.min(18, (n.length - qn.length) / 6);
  if (n.includes(qn)) return 62 - Math.min(12, (n.length - qn.length) / 8);
  if (n.length >= 3 && qn.includes(n)) return 55 * n.length / qn.length;
  const qt = qn.split(' ').filter(Boolean), nt = new Set(n.split(' '));
  const hit = qt.filter(t => nt.has(t)).length;
  return hit ? 45 * hit / qt.length : 0;
}

function find(query, opts) {
  opts = opts || {};
  const res = walkPage({mode: 'collect', next: opts.next}, null);
  const qn = norm(query).toLowerCase();
  const pref = ROLE_PREF[opts.intent] || null;
  const scored = [];
  for (const c of res.cands) {
    let s = scoreText(qn, c.name);
    for (const a of c.alt) s = Math.max(s, scoreText(qn, a) - 8);
    if (!s) continue;
    if (pref) s += pref.includes(c.role) ? 12 : -25;
    if (c.vis) s += 3;
    if (c.disabled) s -= 6;
    scored.push({...c, score: Math.round(s)});
  }
  scored.sort((a, b) => b.score - a.score);
  const top = scored.slice(0, opts.limit || 8).map(({alt, ...c}) => c);
  return {matches: top, next: state.next};
}

function describe(el) {
  if (!el || el.nodeType !== 1) return null;
  const tag = el.localName;
  const role = roleOf(el, tag) || tag;
  const seen = new Set();
  const ref = refFor(el, seen);
  const name = nameOf(el, tag, role);
  const val = valueOf(el, tag, role);
  const st = statesOf(el, tag, role, deepActive());
  return {ref, role, name: cut(name, 100), tag, value: val === null ? null : cut(norm(String(val)), 100), states: st,
    type: (el.getAttribute('type') || '').toLowerCase(), editable: !!el.isContentEditable, next: state.next,
    multiple: !!el.multiple,
    ctl: (tag === 'label' && el.control) ? {role: roleOf(el.control, el.control.localName), tag: el.control.localName,
      type: (el.control.getAttribute('type') || '').toLowerCase()} : null};
}

function stamp(el, next) {
  if (next) state.next = Math.max(state.next, next);
  return describe(el);
}

function selectOptions(el, wanted) {
  const opts = Array.from(el.options);
  const out = [];
  for (const w of wanted) {
    const wn = norm(w).toLowerCase();
    let hit = opts.find(o => o.value === w)
      || opts.find(o => norm(o.label || o.text).toLowerCase() === wn)
      || opts.find(o => norm(o.label || o.text).toLowerCase().startsWith(wn))
      || opts.find(o => norm(o.label || o.text).toLowerCase().includes(wn))
      || opts.find(o => o.value.toLowerCase() === wn);
    if (!hit) return {error: w, options: opts.map(o => norm(o.label || o.text) || o.value).slice(0, 40)};
    out.push(hit.value);
  }
  return {values: out};
}

function scrollable(el) {
  const cs = getComputedStyle(el);
  return (/(auto|scroll|overlay)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 4) ||
         (/(auto|scroll|overlay)/.test(cs.overflowX) && el.scrollWidth > el.clientWidth + 4);
}

function scrollBox(el) {
  const se = document.scrollingElement || document.documentElement;
  if (el) {
    for (let p = el; p; p = parentOf(p)) if (p !== document.body && p !== document.documentElement && scrollable(p)) return p;
    return se;
  }
  if (se.scrollHeight > innerHeight + 4 || se.scrollWidth > innerWidth + 4) return se;
  let best = null, area = 0;
  for (const e of document.querySelectorAll('*')) {
    if (e.clientHeight < 50 || !scrollable(e)) continue;
    const r = e.getBoundingClientRect();
    const a = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
    if (a > area) { area = a; best = e; }
  }
  return best || se;
}

function scroll(dir, px, el) {
  const box = scrollBox(el);
  const page = box === (document.scrollingElement || document.documentElement);
  const h = page ? innerHeight : box.clientHeight, w = page ? innerWidth : box.clientWidth;
  const dy = {down: 1, up: -1}[dir] || 0, dx = {right: 1, left: -1}[dir] || 0;
  const before = [box.scrollLeft, box.scrollTop];
  if (dir === 'top') box.scrollTo({top: 0, behavior: 'instant'});
  else if (dir === 'bottom') box.scrollTo({top: box.scrollHeight, behavior: 'instant'});
  else box.scrollBy({left: dx * (px || Math.round(w * 0.8)), top: dy * (px || Math.round(h * 0.8)), behavior: 'instant'});
  return {page, y: Math.round(box.scrollTop), x: Math.round(box.scrollLeft), max: Math.round(box.scrollHeight - h),
    maxX: Math.round(box.scrollWidth - w), h, moved: before[0] !== box.scrollLeft || before[1] !== box.scrollTop,
    box: page ? 'page' : (box.id ? '#' + box.id : box.localName + (box.className && typeof box.className === 'string' ? '.' + box.className.trim().split(/\s+/)[0] : ''))};
}

function settle(quiet, max) {
  return new Promise(resolve => {
    let timer = null;
    const done = () => { mo.disconnect(); clearTimeout(timer); clearTimeout(cap); resolve(true); };
    const mo = new MutationObserver(muts => {
      if (muts.every(m => m.type === 'attributes' && m.attributeName === REF)) return;
      clearTimeout(timer);
      timer = setTimeout(done, quiet);
    });
    mo.observe(document, {childList: true, subtree: true, characterData: true});
    timer = setTimeout(done, quiet);
    const cap = setTimeout(done, max);
  });
}

function deepAll(sel, root, out) {
  root = root || document;
  out = out || [];
  out.push(...root.querySelectorAll(sel));
  for (const e of root.querySelectorAll('*')) if (e.shadowRoot) deepAll(sel, e.shadowRoot, out);
  return out;
}

const COLORS = {link: '#1a7f37', button: '#0969da', textbox: '#cf222e', searchbox: '#cf222e', select: '#cf222e',
  combobox: '#cf222e', checkbox: '#bf3989', radio: '#bf3989', switch: '#bf3989', file: '#cf222e', iframe: '#57606a'};

function overlay(on, onlyRefs, full) {
  document.querySelectorAll('[data-cb-overlay]').forEach(e => e.remove());
  if (!on) return 0;
  const allow = onlyRefs ? new Set(onlyRefs.map(String)) : null;
  const box = document.createElement('div');
  box.setAttribute('data-cb-overlay', '1');
  box.style.cssText = 'all:initial;position:absolute;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
  let n = 0;
  for (const el of deepAll('[' + REF + ']')) {
    const r = el.getAttribute(REF);
    if (allow && !allow.has(r)) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) continue;
    if (!full && (rect.bottom < 0 || rect.top > innerHeight || rect.right < 0 || rect.left > innerWidth)) continue;
    const role = roleOf(el, el.localName);
    const c = COLORS[role] || '#8250df';
    const x = rect.left + scrollX, y = rect.top + scrollY;
    const b = document.createElement('div');
    b.style.cssText = `all:initial;position:absolute;left:${x}px;top:${y}px;width:${rect.width}px;height:${rect.height}px;` +
      `outline:2px solid ${c};outline-offset:-1px;box-sizing:border-box;`;
    const t = document.createElement('div');
    t.textContent = r;
    const ty = rect.top >= 15 ? y - 15 : y;
    t.style.cssText = `all:initial;position:absolute;left:${x}px;top:${ty}px;background:${c};color:#fff;` +
      'font:bold 11px/15px monospace;padding:0 3px;border-radius:2px;white-space:nowrap;';
    box.append(b, t);
    n++;
  }
  document.documentElement.appendChild(box);
  return n;
}

function info() {
  const de = document.documentElement;
  return {url: location.href, title: document.title, vw: innerWidth, vh: innerHeight,
    sy: Math.round(scrollY), sh: de ? de.scrollHeight : 0, ready: document.readyState};
}

window.__cb = {v: V, snapshot: walkPage, find, describe, stamp, selectOptions, scroll, settle, overlay, info,
  nameOf, roleOf};
})();
"""

PAGE_JS_VERSION = f"{zlib.crc32(_PAGE_JS_TEMPLATE.encode('utf-8')):08x}"
PAGE_JS = _PAGE_JS_TEMPLATE.replace("__CB_VERSION__", PAGE_JS_VERSION)

_pw_mod = None


def pw():
    """Import Playwright lazily (it's the slow part of startup)."""
    global _pw_mod
    if _pw_mod is None:
        try:
            import playwright.async_api as m
        except ImportError:
            raise CBError("Playwright is not installed. Run: pip install playwright   "
                          "(no `playwright install` needed if you have Chrome/Brave/Edge)")
        _pw_mod = m
    return _pw_mod


# ─── Browser discovery & launch ──────────────────────────────────────────────

DEFAULT_PORT = 9333

if IS_WINDOWS:
    _pf = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
           os.environ.get("LOCALAPPDATA", "")]
    BROWSER_PATHS = {
        "brave": [os.path.join(p, r"BraveSoftware\Brave-Browser\Application\brave.exe") for p in _pf if p],
        "chrome": [os.path.join(p, r"Google\Chrome\Application\chrome.exe") for p in _pf if p],
        "edge": [os.path.join(p, r"Microsoft\Edge\Application\msedge.exe") for p in _pf if p],
        "chromium": [os.path.join(p, r"Chromium\Application\chrome.exe") for p in _pf if p],
    }
elif IS_MAC:
    BROWSER_PATHS = {
        "brave": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
        "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "edge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        "chromium": ["/Applications/Chromium.app/Contents/MacOS/Chromium"],
    }
else:
    BROWSER_PATHS = {
        "brave": ["brave-browser", "brave", "/opt/brave.com/brave/brave"],
        "chrome": ["google-chrome", "google-chrome-stable", "/opt/google/chrome/chrome"],
        "edge": ["microsoft-edge", "microsoft-edge-stable"],
        "chromium": ["chromium", "chromium-browser", "/snap/bin/chromium"],
    }
BROWSER_ORDER = ["brave", "chrome", "edge", "chromium"]
PROCESS_NAMES = {"brave": "brave", "chrome": "chrome", "edge": "msedge", "chromium": "chromium"}
_POSIX_PROC_ALIASES = {
    "brave": ["brave", "brave-browser"],
    "chrome": ["chrome", "google-chrome"],
    "msedge": ["msedge", "microsoft-edge"],
    "chromium": ["chromium", "chromium-browse"],
}


def _playwright_chromium() -> list[str]:
    """Chromium builds downloaded by `playwright install`, if any."""
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")]
    if IS_WINDOWS:
        roots.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"))
    elif IS_MAC:
        roots.append(os.path.expanduser("~/Library/Caches/ms-playwright"))
    else:
        roots.append(os.path.expanduser("~/.cache/ms-playwright"))
    rel = {"win": ["chrome-win/chrome.exe", "chrome-win64/chrome.exe"],
           "mac": ["chrome-mac/Chromium.app/Contents/MacOS/Chromium",
                   "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
                   "chrome-mac-x64/Chromium.app/Contents/MacOS/Chromium"],
           "linux": ["chrome-linux/chrome", "chrome-linux64/chrome"]}["win" if IS_WINDOWS else "mac" if IS_MAC else "linux"]
    out = []
    for root in filter(None, roots):
        rp = Path(root)
        if not rp.is_dir():
            continue
        for d in sorted(rp.glob("chromium-*"), reverse=True):
            for r in rel:
                if (d / r).exists():
                    out.append(str(d / r))
    return out


def _resolve_exe(path: str) -> str | None:
    if os.sep in path or "/" in path:
        return path if Path(path).exists() else None
    return shutil.which(path)


def _kind_from_path(path: str) -> str:
    n = Path(path).name.lower() + " " + str(Path(path).parent).lower()
    for k, needle in (("edge", "edge"), ("brave", "brave"), ("chrome", "google"), ("chromium", "chromium")):
        if needle in n:
            return k
    return "chrome" if "chrome" in n else "chromium"


def find_browser(preferred: str | None = None) -> tuple[str, str]:
    """Return (kind, executable). Honors CB_BROWSER_PATH and CB_BROWSER."""
    env_path = _env("CB_BROWSER_PATH")
    if env_path:
        resolved = _resolve_exe(env_path)
        if not resolved:
            raise CBError(f"CB_BROWSER_PATH={env_path!r} does not exist")
        return _kind_from_path(resolved), resolved
    pref = (preferred or _env("CB_BROWSER") or "").lower() or None
    if pref and pref not in BROWSER_PATHS:
        raise UsageError(f"Unknown browser {pref!r}; choose from {', '.join(BROWSER_ORDER)}")
    for kind in ([pref] if pref else BROWSER_ORDER):
        cands = list(BROWSER_PATHS.get(kind, [])) + (_playwright_chromium() if kind == "chromium" else [])
        for c in cands:
            r = _resolve_exe(c)
            if r:
                return kind, r
    if pref:
        raise CBError(f"{pref} not found. Install it, or set CB_BROWSER_PATH to the executable.")
    raise CBError("No Chromium-based browser found (Brave/Chrome/Edge/Chromium). "
                  "Install one, run `playwright install chromium`, or set CB_BROWSER_PATH.")


def _http_json(port: int, path: str = "/json/version", timeout: float = 0.6):
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def browser_info() -> dict:
    return _read_json(session_dir() / "browser.json")


def cdp_port() -> int:
    p = _env("CB_CDP_PORT")
    if p:
        return int(p)
    info = browser_info()
    if info.get("port"):
        return int(info["port"])
    return DEFAULT_PORT if session_name() == "default" else 0


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if IS_WINDOWS:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _is_cdp_browser(pid: int, port: int | None) -> bool:
    """Is `pid` still the browser cb launched (not a recycled pid)? Checked via its command line or CDP port."""
    if not _pid_alive(pid):
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        try:
            return b"--remote-debugging-port" in cmdline.read_bytes()
        except OSError:
            return False
    return bool(port and _http_json(port, timeout=0.3))


def _kill_pid_tree(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                return
        for _ in range(30):
            if not _pid_alive(pid):
                return
            time.sleep(0.1)


def _kill_by_name(proc_name: str) -> None:
    """Force-kill every process of a browser (legacy `cb kill <kind> --all`)."""
    if IS_WINDOWS:
        subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                        f"Stop-Process -Name {proc_name} -Force -ErrorAction SilentlyContinue"],
                       check=False, capture_output=True)
        return
    names = _POSIX_PROC_ALIASES.get(proc_name, [proc_name])
    for n in names:
        subprocess.run(["pkill", "-x", n], check=False, capture_output=True)
    for _ in range(20):
        if not any(subprocess.run(["pgrep", "-x", n], capture_output=True).returncode == 0 for n in names):
            return
        time.sleep(0.1)
    for n in names:
        subprocess.run(["pkill", "-9", "-x", n], check=False, capture_output=True)


def _want_headless(flag: bool | None) -> bool:
    if flag is not None:
        return flag
    v = (_env("CB_HEADLESS") or "").lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return not IS_WINDOWS and not IS_MAC and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def launch_browser(kind: str | None = None, headless: bool | None = None, profile: str | None = None,
                   port: int | None = None, extra: list[str] | None = None) -> dict:
    """Start a browser with CDP enabled on its own profile; returns the browser.json record."""
    kind, exe = find_browser(kind)
    headless = _want_headless(headless)
    port = port or cdp_port() or _free_port()
    profile = profile or _env("CB_USER_DATA_DIR") or ""
    if profile == "default":
        user_data = None
    else:
        user_data = str(Path(os.path.expanduser(profile)) if profile else session_dir() / "profiles" / kind)
        Path(user_data).mkdir(parents=True, exist_ok=True)
    cmd = [exe, f"--remote-debugging-port={port}", "--no-first-run", "--no-default-browser-check",
           "--disable-search-engine-choice-screen", "--disable-features=Translate,MediaRouter",
           "--password-store=basic", "--use-mock-keychain",
           # Background tabs must keep running timers/rAF, or actionability checks stall.
           "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
           "--disable-renderer-backgrounding"]
    if user_data:
        cmd.append(f"--user-data-dir={user_data}")
    if headless:
        cmd += ["--headless=new", "--window-size=1280,900", "--hide-scrollbars", "--mute-audio",
                "--disable-background-networking", "--disable-component-update"]
    if not IS_WINDOWS and not IS_MAC:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.append("--no-sandbox")
        cmd.append("--disable-dev-shm-usage")
    cmd += list(extra or []) + shlex.split(_env("CB_BROWSER_ARGS") or "", posix=not IS_WINDOWS)
    cmd.append("about:blank")
    log = open(session_dir() / "browser.log", "ab")
    kw: dict = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
    if IS_WINDOWS:
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kw)
    log.close()
    deadline = time.time() + 20
    while time.time() < deadline:
        if _http_json(port):
            rec = {"pid": proc.pid, "port": port, "kind": kind, "exe": exe, "headless": headless,
                   "profile": user_data or "(browser default)", "started": time.time()}
            _write_json(session_dir() / "browser.json", rec)
            return rec
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    hint = ""
    if not user_data:
        hint = (" Chrome 136+ ignores --remote-debugging-port on the default profile, and an already-running"
                " instance swallows the flag; use the default cb profile instead (omit --profile).")
    tail = ""
    try:
        tail = (session_dir() / "browser.log").read_text(errors="replace")[-600:].strip()
    except OSError:
        pass
    raise CBError(f"Launched {kind} but CDP port {port} never came up.{hint}" + (f"\nbrowser log tail:\n{tail}" if tail else ""))


# ─── Page-side helpers ───────────────────────────────────────────────────────

_CALL = ("([v, fn, args]) => (window.__cb && window.__cb.v === v) ? window.__cb[fn](...args) : '__cb_missing__'")
_EL_CALL = ("(el, [v, fn, args]) => (window.__cb && window.__cb.v === v) ? window.__cb[fn](el, ...args) : '__cb_missing__'")


async def page_call(frame, fn: str, *args):
    """Call window.__cb.<fn>(...args) in a frame, injecting the library if needed."""
    payload = [PAGE_JS_VERSION, fn, list(args)]
    r = await frame.evaluate(_CALL, payload)
    if r == "__cb_missing__":
        await frame.evaluate(PAGE_JS)
        r = await frame.evaluate(_CALL, payload)
    return r


async def el_call(loc, fn: str, *args, timeout: int = 5000):
    """Call window.__cb.<fn>(el, ...args) on a Locator's (or ElementHandle's) element."""
    payload = [PAGE_JS_VERSION, fn, list(args)]
    is_loc = hasattr(loc, "element_handle")
    ev = (lambda: loc.evaluate(_EL_CALL, payload, timeout=timeout)) if is_loc else (lambda: loc.evaluate(_EL_CALL, payload))
    r = await ev()
    if r == "__cb_missing__":
        h = await loc.element_handle(timeout=timeout) if is_loc else loc
        fr = await h.owner_frame()
        await fr.evaluate(PAGE_JS)
        r = await ev()
    return r


async def chain_for_frame(rt, frame) -> list[int]:
    """Refs of the iframe elements leading from the top document to `frame`."""
    chain: list[int] = []
    f = frame
    while f.parent_frame is not None:
        el = await f.frame_element()
        d = await el_call(el, "stamp", rt.state.get("next_ref", 1))
        rt.state["next_ref"] = max(rt.state.get("next_ref", 1), d.get("next", 1))
        chain.insert(0, d["ref"])
        f = f.parent_frame
    return chain


async def find_everywhere(rt, page, query: str, intent: str = "click", limit: int = 8) -> list[tuple[dict, list[int]]]:
    """Score elements matching `query` in the scoped frame, then in child iframes if nothing is a strong match."""
    out: list[tuple[dict, list[int]]] = []
    scoped = rt.frame_chain
    frames = [(await rt.scope_frame(page), scoped)]
    for i, (frame, chain) in enumerate(frames):
        try:
            res = await page_call(frame, "find", query, {"intent": intent, "next": rt.state.get("next_ref", 1), "limit": limit})
        except Exception:
            continue
        rt.state["next_ref"] = max(rt.state.get("next_ref", 1), res.get("next", 1))
        out += [(m, chain) for m in res.get("matches") or []]
        if i == 0 and not scoped and (not out or out[0][0]["score"] < 90):
            for cf in [f for f in page.frames if f is not page.main_frame][:8]:
                try:
                    if cf.is_detached():
                        continue
                    frames.append((cf, await chain_for_frame(rt, cf)))
                except Exception:
                    continue
    out.sort(key=lambda mc: -mc[0]["score"])
    return out


# ─── State (per session, persisted between commands) ─────────────────────────

def _state_path():
    return session_dir() / "state.json"


def shots_dir() -> Path:
    d = Path(os.path.expanduser(_env("CB_SHOT_DIR") or str(session_dir() / "shots")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def downloads_dir() -> Path:
    d = Path(os.path.expanduser(_env("CB_DOWNLOAD_DIR") or str(session_dir() / "downloads")))
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── Error translation ───────────────────────────────────────────────────────

def clean_error(e: BaseException) -> str:
    """Turn a Playwright exception into one or two actionable lines."""
    msg = str(e).strip()
    if not msg:
        return type(e).__name__
    lines = msg.splitlines()
    head = re.sub(r"^(Locator|Page|Frame|ElementHandle|BrowserContext|Browser|Keyboard|Mouse|JSHandle)\.[\w_]+:\s*",
                  "", lines[0]).strip()
    log = [ln.strip().lstrip("-").strip() for ln in lines[1:] if ln.strip().startswith("-")]
    low = msg.lower()
    hint = ""
    blocker = next((ln for ln in log if "intercepts pointer events" in ln), None)
    if blocker:
        b = re.sub(r"\s+(from .*)?subtree intercepts pointer events", "", blocker)
        b = re.sub(r"\s+intercepts pointer events", "", b)
        return (f"click blocked: {b[:200]} is covering the element. Close/dismiss it first "
                f"(it is probably a dialog, banner or overlay), or retry with --force / --js.")
    if "element is not visible" in low or "not visible" in " ".join(log[-3:]).lower():
        hint = "element is hidden. Open the menu/section that contains it, scroll, or pick another ref from `cb snap`."
    elif "element is not enabled" in low or "element is disabled" in low:
        hint = "element is disabled (a required field may be empty or invalid)."
    elif "not an <input>" in low or "is not an <input>" in low:
        hint = "element is not a text field. Click it first, then use `cb type <text>`."
    elif "outside of the viewport" in low:
        hint = "element is outside the viewport. Try `cb scroll <target>` first, or --js."
    elif "element is not stable" in low:
        hint = "element is still animating. Retry, or use --force."
    elif "net::err_name_not_resolved" in low:
        hint = "host name did not resolve (typo, or no network access)."
    elif "net::err_connection_refused" in low:
        hint = "connection refused (is the server running?)."
    elif "net::err_" in low:
        m = re.search(r"net::ERR_[A-Z_]+", msg)
        hint = f"network error {m.group(0) if m else ''}".strip()
    elif "has been closed" in low or "target closed" in low:
        hint = "the page/tab was closed. Run `cb tabs` to pick another tab."
    elif "execution context was destroyed" in low:
        hint = "the page navigated while the command ran. Re-run it (or `cb snap`)."
    if "timeout" in head.lower() and hint:
        return f"{hint} ({head})"
    out = head
    if hint:
        out += f"\n  hint: {hint}"
    elif "timeout" in head.lower() and log:
        out += "\n  last step: " + log[-1][:200]
    return out


# ─── Runtime: one CDP connection plus event capture ──────────────────────────

NOISY_TYPES = {"websocket", "eventsource", "media", "manifest", "other", "ping", "texttrack"}


class Runtime:
    def __init__(self, persistent: bool):
        self.persistent = persistent      # True in the daemon
        self.pw_ctx = None
        self.browser = None
        self.port: int | None = None
        self.page = None
        self.state: dict = _read_json(_state_path())
        self.seq = 0
        self.events: deque = deque(maxlen=200)       # (seq, kind, text)
        self.reported = self.seq
        self.console: deque = deque(maxlen=1000)
        self.network: deque = deque(maxlen=1000)
        self.inflight: dict = {}
        self.responses: dict = {}
        self.req_id = 0
        self.watched_pages: set = set()
        self.watched_ctx: set = set()
        self.last_nav_seq = 0
        self.front = None

    # ── persistence
    def save(self) -> None:
        try:
            _write_json(_state_path(), self.state)
        except OSError:
            pass

    def bump(self) -> int:
        self.seq += 1
        return self.seq

    def event(self, kind: str, text: str) -> None:
        self.events.append((self.bump(), kind, text))

    # ── connection
    async def ensure(self, ctx: Ctx | None, launch: bool = True):
        if self.browser is not None and self.browser.is_connected():
            return self.browser
        self.browser = None
        port = cdp_port()
        if not port or not _http_json(port):
            if not launch or (_env("CB_AUTO_LAUNCH") or "1") in ("0", "false", "no"):
                raise CBError(f"No browser with CDP on port {port or '(none)'}. Run `cb launch`.")
            rec = launch_browser(port=port or None)
            port = rec["port"]
            if ctx:
                ctx.note(f"launched {rec['kind']}{' (headless)' if rec['headless'] else ''} on port {port}"
                         f" · profile {rec['profile']}")
        if self.pw_ctx is None:
            self.pw_ctx = await pw().async_playwright().start()
        try:
            self.browser = await self.pw_ctx.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
        except Exception as e:
            raise CBError(f"Cannot attach to the browser on CDP port {port}: {clean_error(e)}")
        self.port = port
        self.browser.on("disconnected", lambda *_: self._on_disconnect())
        for c in self.browser.contexts:
            self._watch_context(c)
        return self.browser

    def _on_disconnect(self):
        self.browser = None
        self.page = None
        self.watched_pages.clear()
        self.watched_ctx.clear()

    async def shutdown(self):
        try:
            if self.browser is not None:
                await self.browser.close()  # for CDP connections this only disconnects
        except Exception:
            pass
        self.browser = None
        try:
            if self.pw_ctx is not None:
                await self.pw_ctx.stop()
        except Exception:
            pass
        self.pw_ctx = None

    # ── pages
    def pages(self) -> list:
        if self.browser is None:
            return []
        out = []
        for c in self.browser.contexts:
            for p in c.pages:
                if not (p.url or "").startswith("devtools://"):
                    out.append(p)
        return out

    def set_page(self, page) -> None:
        self.page = page
        pages = self.pages()
        self.state["tab"] = {"url": page.url, "i": pages.index(page) if page in pages else 0}

    async def get_page(self, ctx: Ctx | None = None):
        await self.ensure(ctx)
        pages = self.pages()
        if self.page is not None and not self.page.is_closed() and self.page in pages:
            if self.page is not self.front:
                await self.activate(self.page)
            return self.page
        page = None
        want = (_env("CB_TAB_URL") or "").strip()
        if want:
            page = next((p for p in pages if want in (p.url or "")), None)
        hint = self.state.get("tab") or {}
        if page is None and hint:
            i, url = hint.get("i", -1), hint.get("url")
            if 0 <= i < len(pages) and pages[i].url == url:
                page = pages[i]
            else:
                page = next((p for p in pages if p.url == url), None)
        if page is None and pages:
            non_blank = [p for p in pages if p.url not in ("about:blank", "", "chrome://newtab/")]
            page = (non_blank or pages)[-1]
        if page is None:
            contexts = self.browser.contexts
            c = contexts[0] if contexts else await self.browser.new_context()
            self._watch_context(c)
            page = await c.new_page()
        self._watch_page(page)
        self.set_page(page)
        await self.activate(page)
        return page

    async def activate(self, page) -> None:
        """Bring a tab to the front: background tabs throttle timers and animation frames."""
        try:
            await page.bring_to_front()
            self.front = page
        except Exception:
            pass

    # ── frame scope (`cb frame`)
    @property
    def frame_chain(self) -> list[int]:
        return list(self.state.get("frame") or [])

    async def scope_frame(self, page):
        """The Frame commands operate in (top frame unless `cb frame` set one)."""
        chain = self.frame_chain
        if not chain:
            return page.main_frame
        try:
            return await frame_for_chain(page, chain)
        except CBError:
            self.state["frame"] = None
            return page.main_frame

    # ── listeners
    def _watch_context(self, c) -> None:
        if id(c) in self.watched_ctx:
            return
        self.watched_ctx.add(id(c))
        c.on("page", self._on_new_page)
        for p in c.pages:
            self._watch_page(p)

    def _on_new_page(self, page) -> None:
        self._watch_page(page)
        opener_known = self.page is not None and not self.page.is_closed()
        self.event("tab", f"new tab opened: {page.url or 'about:blank'}"
                          + (" (now active; `cb tabs` to list)" if opener_known else ""))
        if opener_known:
            self.page = page
            self.state["tab"] = {"url": page.url, "i": self.pages().index(page) if page in self.pages() else 0}

    def _watch_page(self, page) -> None:
        if id(page) in self.watched_pages:
            return
        self.watched_pages.add(id(page))
        page.on("console", lambda m: self._on_console(page, m))
        page.on("pageerror", lambda e: self._on_pageerror(page, e))
        page.on("dialog", lambda d: asyncio.ensure_future(self._on_dialog(page, d)))
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfinished", lambda r: self._on_request_done(r, None))
        page.on("requestfailed", lambda r: self._on_request_done(r, r.failure or "failed"))
        page.on("download", lambda d: asyncio.ensure_future(self._on_download(page, d)))
        page.on("framenavigated", lambda f: self._on_nav(page, f))
        page.on("close", lambda *_: self._on_close(page))

    def _on_close(self, page) -> None:
        self.watched_pages.discard(id(page))
        if page is self.page:
            self.page = None
            self.event("tab", "active tab was closed")

    def _on_nav(self, page, frame) -> None:
        if frame == page.main_frame:
            self.last_nav_seq = self.bump()

    def _on_console(self, page, m) -> None:
        try:
            loc = m.location or {}
            if "favicon" in (loc.get("url") or "") and m.type == "error":
                return
            where = f"{loc.get('url', '')}:{loc.get('lineNumber', '')}" if loc.get("url") else ""
            self.console.append({"seq": self.bump(), "type": m.type, "text": m.text[:4000], "where": where,
                                 "page": page.url})
        except Exception:
            pass

    def _on_pageerror(self, page, e) -> None:
        self.console.append({"seq": self.bump(), "type": "pageerror", "text": str(e)[:4000], "where": "",
                             "page": page.url})

    async def _on_dialog(self, page, d) -> None:
        pol = self.state.get("dialog") or [(_env("CB_DIALOG") or "accept").lower(), None, False]
        action, text, always = pol
        if not always:
            self.state.pop("dialog", None)
        try:
            if d.type == "beforeunload" or action == "accept":
                if d.type == "prompt":
                    await d.accept(text if text is not None else d.default_value)
                else:
                    await d.accept()
                action = "accepted"
            else:
                await d.dismiss()
                action = "dismissed"
        except Exception as e:
            action = f"could not handle: {clean_error(e)}"
        msg = d.message.replace("\n", " ")[:300]
        self.event("dialog", f"dialog ({d.type}) {msg!r} -> {action}"
                             + (" (`cb dialog dismiss` to decline next time)" if action == "accepted" and d.type == "confirm" else ""))

    def _on_request(self, r) -> None:
        self.req_id += 1
        e = {"id": self.req_id, "seq": self.bump(), "method": r.method, "url": r.url, "type": r.resource_type,
             "status": None, "error": None, "t0": time.time(), "ms": None}
        self.inflight[r] = e
        self.network.append(e)
        if len(self.inflight) > 500:  # long-polls/streams that never finish
            for k in [k for k, v in self.inflight.items() if time.time() - v["t0"] > 60]:
                self.inflight.pop(k, None)

    def _on_response(self, resp) -> None:
        e = self.inflight.get(resp.request)
        if e is not None:
            e["status"] = resp.status
            if e["type"] in ("xhr", "fetch", "document"):
                self.responses[e["id"]] = resp
                if len(self.responses) > 300:
                    self.responses.pop(next(iter(self.responses)))

    def _on_request_done(self, r, failure) -> None:
        e = self.inflight.pop(r, None)
        if e is None:
            return
        e["ms"] = int((time.time() - e["t0"]) * 1000)
        if failure:
            e["error"] = str(failure)
        elif self.persistent and e["type"] in ("xhr", "fetch") and e["id"] in self.responses:
            asyncio.ensure_future(self._grab_body(e))

    async def _grab_body(self, e) -> None:
        """Keep small API response bodies: Chrome evicts them soon after the request."""
        resp = self.responses.get(e["id"])
        try:
            if int((await resp.all_headers()).get("content-length") or 0) > 512_000:
                return
            body = await resp.body()
            if len(body) <= 512_000:
                e["body"] = body.decode("utf-8", errors="replace")
        except Exception:
            pass

    async def _on_download(self, page, d) -> None:
        try:
            name = re.sub(r'[\\/:*?"<>|]+', "_", d.suggested_filename or "download")
            dest = downloads_dir() / name
            stem, suf, i = dest.stem, dest.suffix, 1
            while dest.exists():
                dest = dest.with_name(f"{stem} ({i}){suf}")
                i += 1
            await d.save_as(str(dest))
            self.event("download", f"downloaded: {dest}")
        except Exception as e:
            self.event("download", f"download failed: {clean_error(e)}")

    # ── waiting for the page to settle after an action
    async def settle(self, page, mark: int, max_ms: int | None = None) -> None:
        max_ms = max_ms if max_ms is not None else env_int("CB_SETTLE_MS", 3000)
        if max_ms <= 0:
            return
        t_end = time.time() + max_ms / 1000
        if self.last_nav_seq > mark:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=max(1, int((t_end - time.time()) * 1000)))
            except Exception:
                pass
        net_end = min(t_end, time.time() + 2.0)
        while time.time() < net_end:
            pending = [e for e in self.inflight.values() if e["seq"] > mark and e["type"] not in NOISY_TYPES
                       and time.time() - e["t0"] < 5]
            if not pending:
                break
            await asyncio.sleep(0.04)
        remaining = int((t_end - time.time()) * 1000)
        if remaining > 50:
            for _ in range(2):
                try:
                    await page_call(page.main_frame, "settle", 120, min(1000, remaining))
                    break
                except Exception:
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=max(1, remaining))
                    except Exception:
                        break


async def frame_for_chain(page, chain: list[int]):
    f = page.main_frame
    for r in chain:
        try:
            h = await f.locator(f'[data-cb-ref="{r}"]').first.element_handle(timeout=2000)
            nf = await h.content_frame() if h else None
        except Exception:
            nf = None
        if nf is None:
            raise CBError(f"iframe [{r}] is gone (page changed). Run `cb snap`.")
        f = nf
    return f


def scope_for_chain(page, chain: list[int]):
    """Page or FrameLocator for a frame chain (lazy, auto-waiting)."""
    sc = page
    for r in chain:
        sc = sc.locator(f'[data-cb-ref="{r}"]').first.content_frame
    return sc


# ─── Snapshots: build, render, diff ──────────────────────────────────────────

async def take_snapshot(rt: Runtime, page, *, mode: str = "snap", viewport: bool = False, urls: bool = False,
                        scope_loc=None, links: bool = False, all_text: bool = False, images: bool = False) -> dict:
    """Outline of the page (and visible iframes) with stable [ref]s."""
    top = await rt.scope_frame(page)
    base_chain = rt.frame_chain
    refs: dict = {}
    frames_left = [15]

    async def one(frame, chain, depth0, handle):
        opts = {"mode": mode, "viewport": viewport, "urls": urls, "next": rt.state.get("next_ref", 1),
                "links": links, "all": all_text, "images": images}
        try:
            res = await page_call(frame, "snapshot", opts, handle)
        except Exception as e:
            if "Execution context was destroyed" in str(e) or "navigat" in str(e).lower():
                await asyncio.sleep(0.3)
                res = await page_call(frame, "snapshot", opts, handle)
            else:
                raise
        rt.state["next_ref"] = max(rt.state.get("next_ref", 1), res.get("next", 1))
        for k, v in (res.get("refs") or {}).items():
            v["f"] = chain
            refs[str(k)] = v
        out = []
        for ln in res["lines"]:
            ln["d"] += depth0
            out.append(ln)
            if ln["k"] == "f" and mode != "read" and frames_left[0] > 0 and len(chain) < 4:
                frames_left[0] -= 1
                try:
                    h = await frame.locator(f'[data-cb-ref="{ln["r"]}"]').first.element_handle(timeout=1500)
                    cf = await h.content_frame() if h else None
                    if cf is not None:
                        sub, _ = await asyncio.wait_for(one(cf, chain + [ln["r"]], ln["d"] + 1, None), 8)
                        out.extend(sub)
                except Exception:
                    out.append({"d": ln["d"] + 1, "k": "t", "t": "(frame not accessible)", "v": ln.get("v")})
        return out, res

    handle = None
    if scope_loc is not None:
        handle = await scope_loc.element_handle(timeout=5000)
    lines, res = await one(top, base_chain, 0, handle)
    return {"lines": lines, "refs": refs, "url": page.url, "title": res.get("title", ""), "truncated": res.get("truncated"),
            "blocker": res.get("blocker"),
            "vw": res.get("vw"), "vh": res.get("vh"), "sy": res.get("sy"), "sh": res.get("sh")}


def line_text(ln: dict) -> str:
    return "  " * ln["d"] + ln["t"]


def prune_structure(lines: list[dict]) -> list[dict]:
    """Drop container lines (nav:, form:, ...) that ended up with no children."""
    kept: list[dict] = []
    for ln in reversed(lines):
        if ln["k"] == "s" and (not kept or kept[-1]["d"] <= ln["d"]):
            continue
        kept.append(ln)
    return kept[::-1]


FORM_ROLES = {"textbox", "searchbox", "combobox", "select", "listbox", "checkbox", "radio", "switch", "slider",
              "spinbutton", "file", "input", "label"}


def filter_lines(lines: list[dict], *, interactive=False, forms=False) -> list[dict]:
    if forms:
        keep = []
        for ln in lines:
            if ln["k"] == "s" or ln["k"] == "f":
                keep.append(ln)
            elif ln["k"] == "i":
                m = re.search(r"\[\d+\] (\w+)", ln["t"])
                if m and (m.group(1) in FORM_ROLES or m.group(1) == "button"):
                    keep.append(ln)
        return prune_structure(keep)
    if interactive:
        return prune_structure([ln for ln in lines if ln["k"] in "isfh"])
    return lines


def budget_lines(lines: list[dict], max_chars: int) -> tuple[list[dict], str]:
    """Fit an outline into max_chars: drop off-screen text, then all text, then cut."""
    def size(ls):
        return sum(len(x["t"]) + 2 * x["d"] + 1 for x in ls)
    if max_chars <= 0 or size(lines) <= max_chars:
        return lines, ""
    tier1 = prune_structure([ln for ln in lines if ln["k"] in "isfh" or (ln.get("v") and ln["k"] in "tm")])
    if size(tier1) <= max_chars:
        return tier1, "off-screen text omitted to save tokens; `cb read` for page text, `--all` for everything"
    tier2 = prune_structure([ln for ln in lines if ln["k"] in "isfh"])
    note = "text omitted to save tokens; `cb read` for page text"
    if size(tier2) <= max_chars:
        return tier2, note
    out, n = [], 0
    for ln in tier2:
        n += len(ln["t"]) + 2 * ln["d"] + 1
        if n > max_chars:
            break
        out.append(ln)
    rest = len(tier2) - len(out)
    return out, (f"{rest} more lines not shown; use `cb snap --in <ref>`, `cb snap -v` (viewport only), "
                 f"`cb find <text>`, or `--max N`")


def page_header(page, snap: dict, rt: Runtime) -> list[str]:
    title = (snap.get("title") or "").strip()
    pages = rt.pages()
    url = snap.get("url") or page.url
    extra = []
    if len(pages) > 1 and page in pages:
        extra.append(f"tab {pages.index(page)}; {len(pages)} open")
    sh, vh, sy = snap.get("sh") or 0, snap.get("vh") or 0, snap.get("sy") or 0
    if vh and sh > vh * 1.3:
        extra.append(f"scrolled {int(100 * min(1, sy / max(1, sh - vh)))}% of {sh}px")
    out = [f"Page: {title or '(untitled)'}", f"URL: {url}" + (f"  ({', '.join(extra)})" if extra else "")]
    if rt.frame_chain:
        out.append(f"Frame scope: iframe {' > '.join(f'[{r}]' for r in rt.frame_chain)}  (`cb frame top` to reset)")
    if snap.get("blocker"):
        out.append(f"Note: {snap['blocker']} covers most of the page and will intercept clicks "
                   "(close it, press Escape, or use --force)")
    return out


def remember_snapshot(rt: Runtime, page, snap: dict) -> list[str]:
    rendered = [line_text(ln) for ln in snap["lines"]]
    rt.state["snap"] = {"url": snap["url"], "lines": rendered, "frame": rt.frame_chain, "t": time.time()}
    refs = snap["refs"]
    rt.state["refs"] = refs
    return rendered


def print_snapshot(ctx: Ctx, rt: Runtime, page, snap: dict, *, max_chars: int | None = None, lines=None,
                   header: bool = True) -> None:
    if header:
        for h in page_header(page, snap, rt):
            ctx.out(h)
    ls = snap["lines"] if lines is None else lines
    max_chars = env_int("CB_SNAP_MAX", 12000) if max_chars is None else max_chars
    shown, note = budget_lines(ls, max_chars)
    if not shown:
        ctx.out("(no visible content)" if not ls else "")
    for ln in shown:
        ctx.out(line_text(ln))
    if snap.get("truncated"):
        ctx.out("(page has a very large DOM; outline stopped early — use `cb snap --in <ref>`)")
    if note:
        ctx.out(f"({note})")


_REF_IN_LINE = re.compile(r"^\s*(?:[-+] )?\s*(?:- )?(?:#+ )?\[(\d+)\]")


def diff_lines(old: list[str], new: list[str], skip_refs: set | None = None) -> list[str]:
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete"):
            out += ["- " + x for x in old[i1:i2]]
        if tag in ("replace", "insert"):
            out += ["+ " + x for x in new[j1:j2]]
    if skip_refs:
        out = [x for x in out if not ((m := _REF_IN_LINE.match(x[2:])) and int(m.group(1)) in skip_refs)]
    return out


def _strip_hash(u: str) -> str:
    return (u or "").split("#", 1)[0]


async def capture_before(rt: Runtime, page) -> dict:
    return {"url": page.url, "page": page, "seq": rt.seq}


async def observe(ctx: Ctx, rt: Runtime, before: dict, *, target_ref: int | None = None,
                  full: bool = False, settle: bool = True, skip_refs: set | None = None) -> None:
    """After a mutating command: settle, report side effects, print a snapshot diff."""
    page0 = before["page"]
    if settle:
        try:
            await rt.settle(page0 if not page0.is_closed() else await rt.get_page(ctx), before["seq"])
        except Exception:
            pass
    if ctx.batch:
        if ctx.pending_observe is None:
            ctx.pending_observe = before
        if target_ref is not None:
            ctx.echoed.add(target_ref)
        report_events(ctx, rt)
        return
    page = await rt.get_page(ctx)
    report_events(ctx, rt)
    navigated = page is not page0 or _strip_hash(page.url) != _strip_hash(before["url"])
    if navigated and page is page0 and not full:
        ctx.out(f"-> {page.url}")
    if ctx.no_snap or (_env("CB_AUTOSNAP") or "diff").lower() in ("off", "0", "no", "false"):
        return
    try:
        snap = await take_snapshot(rt, page)
    except Exception as e:
        ctx.out(f"(could not snapshot: {clean_error(e)})")
        return
    prev = rt.state.get("snap") or {}
    old = prev.get("lines")
    new = remember_snapshot(rt, page, snap)
    if full or navigated or old is None or _strip_hash(prev.get("url", "")) != _strip_hash(snap["url"]) \
            or prev.get("frame") != rt.frame_chain:
        print_snapshot(ctx, rt, page, snap)
        return
    skip = set(skip_refs or ())
    if target_ref is not None:
        skip.add(target_ref)
    d = diff_lines(old, new, skip_refs=skip)
    if not d:
        ctx.out("(no visible change)")
        return
    if len(d) > max(40, len(new) * 0.6):
        ctx.out("(page changed substantially)")
        print_snapshot(ctx, rt, page, snap)
        return
    cap = env_int("CB_DIFF_MAX", 60)
    ctx.out(f"changes ({sum(1 for x in d if x[0] == '+')} added, {sum(1 for x in d if x[0] == '-')} removed):")
    for x in d[:cap]:
        ctx.out(x)
    if len(d) > cap:
        ctx.out(f"... {len(d) - cap} more changed lines (`cb snap` for the full page)")


def report_events(ctx: Ctx, rt: Runtime) -> None:
    since = rt.reported
    for seq, kind, text in list(rt.events):
        if seq > since:
            ctx.out(text)
    errs = [c for c in rt.console if c["seq"] > since and c["type"] in ("error", "pageerror")]
    for c in errs[:3]:
        ctx.out(f"console {c['type']}: {c['text'][:300]}")
    if len(errs) > 3:
        ctx.out(f"... {len(errs) - 3} more console errors (`cb console --errors`)")
    bad = [n for n in rt.network if n["seq"] > since and n["type"] in ("xhr", "fetch", "document")
           and ((n["status"] or 0) >= 400 or (n["error"] and "ERR_ABORTED" not in n["error"]))]
    for n in bad[:3]:
        ctx.out(f"request failed: {n['method']} {n['url'][:150]} -> {n['status'] or n['error']}")
    rt.reported = rt.seq


# ─── Target resolution: refs, selectors, text, coordinates ───────────────────

_REF_RE = re.compile(r"^(?:ref=)?(?:#|@|e|\[)?(\d+)\]?$")
_XY_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_ENGINE_RE = re.compile(r"^(css|xpath|text|role|id|data-testid|internal:[\w-]+|nth|visible)=|^//|^\.\.|^\(//|>>")
_HTML_TAGS = set("a article aside button canvas div fieldset footer form h1 h2 h3 h4 h5 h6 header iframe img "
                 "input li main nav ol p section select span table td textarea th tr ul video".split())


def parse_ref(s: str) -> int | None:
    m = _REF_RE.match(s.strip())
    return int(m.group(1)) if m else None


def looks_like_css(s: str) -> bool:
    """Heuristic: is this target a CSS selector (vs. visible text)? Wrong guesses fall back to text."""
    s = s.strip()
    if not s or len(s) > 300 or re.fullmatch(r"[\w\s#.\[\]=\"'\-:>~+*(),^$|@/]+", s) is None:
        return False
    tokens = s.split()
    if all(t.lower() in _HTML_TAGS for t in tokens):
        return True  # "button", "nav a"
    return any(re.search(r"[#.\[\]:=*]", t) for t in tokens) or any(t in (">", "+", "~") for t in tokens)


def normalize_selector(sel: str) -> str:
    if sel.startswith("#") and len(sel) > 1 and sel[1].isdigit() and not sel[1:].isdigit():
        return f'[id="{sel[1:]}"]'
    return sel


class Target:
    def __init__(self, loc=None, xy=None, ref=None, info=None, how=""):
        self.loc = loc
        self.xy = xy
        self.ref = ref
        self.info = info or {}
        self.how = how

    def desc(self) -> str:
        if self.xy:
            return f"({self.xy[0]:g},{self.xy[1]:g})"
        i = self.info
        s = f"[{self.ref}] {i.get('role') or i.get('tag') or 'element'}" if self.ref else (i.get("role") or "element")
        if i.get("name"):
            s += f' "{i["name"]}"'
        return s


INTENT_ROLES = {
    "fill": {"textbox", "searchbox", "combobox", "spinbutton", "select", "listbox", "slider", "input"},
    "check": {"checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio", "label"},
    "select": {"select", "listbox", "combobox"},
}


async def describe_loc(rt: Runtime, loc, timeout: int = 5000) -> dict:
    d = await el_call(loc, "stamp", rt.state.get("next_ref", 1), timeout=timeout)
    if d:
        rt.state["next_ref"] = max(rt.state.get("next_ref", 1), d.get("next", 1))
    return d or {}


async def resolve(ctx: Ctx, rt: Runtime, page, spec: str, intent: str = "click", timeout: int | None = None) -> Target:
    """Resolve anything that identifies an element: ref, x,y, selector, or visible text."""
    spec = (spec or "").strip()
    if not spec:
        raise UsageError("missing target")
    timeout = timeout if timeout is not None else env_int("CB_TIMEOUT_MS", 5000)
    chain = rt.frame_chain
    scope = scope_for_chain(page, chain)

    ref = parse_ref(spec)
    if ref is not None:
        t = await resolve_ref(rt, page, ref)
        if t.ref != ref:
            ctx.note(f"ref [{ref}] was re-rendered; using [{t.ref}] (same role and name)")
        return t

    m = _XY_RE.match(spec)
    if m:
        return Target(xy=(float(m.group(1)), float(m.group(2))), how="coordinates")

    if _ENGINE_RE.search(spec):
        loc = scope.locator(spec).first
        try:
            await loc.wait_for(state="attached", timeout=timeout)
        except Exception as e:
            raise CBError(f"no element matches selector {spec!r} ({clean_error(e).splitlines()[0]})")
        info = await describe_loc(rt, loc)
        return Target(loc=loc, ref=info.get("ref"), info=info, how="selector")

    css = normalize_selector(spec)
    if looks_like_css(css):
        try:
            loc = scope.locator(css)
            if await loc.count() > 0:
                info = await describe_loc(rt, loc.first)
                return Target(loc=loc.first, ref=info.get("ref"), info=info, how="css")
        except Exception:
            pass

    # Visible text / accessible name. Poll briefly: the element may still be rendering.
    deadline = time.time() + min(timeout, 6000) / 1000
    best: list = []
    while True:
        found = await find_everywhere(rt, page, spec, intent)
        best = [m for m, _ in found]
        if best and best[0]["score"] >= 50:
            top, tchain = found[0]
            info = {"role": top["role"], "name": top["name"], "tag": top["tag"], "f": tchain}
            rt.state.setdefault("refs", {})[str(top["ref"])] = info
            t = Target(loc=scope_for_chain(page, tchain).locator(f'[data-cb-ref="{top["ref"]}"]').first,
                       ref=top["ref"], info=info, how="text")
            ties = [b for b in best[1:] if b["score"] == top["score"]]
            if ties:
                ctx.note(f"{len(ties) + 1} equally good matches for {spec!r}; used [{top['ref']}]. Others: "
                         + ", ".join(f"[{b['ref']}] {b['role']} \"{b['name'][:40]}\"" for b in ties[:4]))
            return t
        # Playwright's own text/label/placeholder engines catch non-interactive text.
        for getter in (lambda: scope.get_by_label(spec), lambda: scope.get_by_placeholder(spec),
                       lambda: scope.get_by_text(spec, exact=True), lambda: scope.get_by_text(spec),
                       lambda: scope.get_by_title(spec), lambda: scope.get_by_alt_text(spec)):
            try:
                loc = getter()
                n = await loc.count()
                for i in range(min(n, 5)):
                    cand = loc.nth(i)
                    if await cand.is_visible():
                        info = await describe_loc(rt, cand)
                        return Target(loc=cand, ref=info.get("ref"), info=info, how="text")
            except Exception:
                pass
        if time.time() >= deadline:
            break
        await asyncio.sleep(0.25)
    msg = f"no element matches {spec!r}"
    if best:
        msg += ". Closest: " + ", ".join(f"[{b['ref']}] {b['role']} \"{b['name'][:50]}\"" for b in best[:5])
    msg += ". Run `cb snap` to see refs, or `cb find <text>`."
    raise CBError(msg)


async def resolve_ref(rt: Runtime, page, ref: int) -> Target:
    refs = rt.state.get("refs") or {}
    info = refs.get(str(ref))
    chains = [info.get("f") or []] if info else []
    if not info:
        chains = [rt.frame_chain] + ([[]] if rt.frame_chain else [])
    for chain in chains:
        scope = scope_for_chain(page, chain)
        loc = scope.locator(f'[data-cb-ref="{ref}"]')
        try:
            if await loc.count() > 0:
                return Target(loc=loc.first, ref=ref, info=info or await describe_loc(rt, loc.first), how="ref")
        except Exception:
            pass
    if info and info.get("name"):
        # Element was re-rendered: try to find its replacement by role + name.
        scope = scope_for_chain(page, info.get("f") or [])
        aria = {"select": "combobox", "file": "button", "input": "textbox", "label": None}.get(info["role"], info["role"])
        cands = []
        try:
            if aria:
                cands.append(scope.get_by_role(aria, name=info["name"], exact=True))
            cands.append(scope.get_by_text(info["name"], exact=True))
            for c in cands:
                if await c.count() == 1:
                    d = await describe_loc(rt, c.first)
                    return Target(loc=c.first, ref=d.get("ref"), info=d, how=f"re-found stale ref [{ref}]")
        except Exception:
            pass
    snap = rt.state.get("snap") or {}
    where = f" (it came from {snap.get('url')})" if snap.get("url") and _strip_hash(snap.get("url")) != _strip_hash(page.url) else ""
    raise CBError(f"ref [{ref}] is not on the page anymore{where}. Run `cb snap` for fresh refs.")


# ─── Argument parsing ────────────────────────────────────────────────────────

class Cmd:
    def __init__(self, name, fn, usage, summary, group, flags, short, aliases, browser, details):
        self.name, self.fn, self.usage, self.summary, self.group = name, fn, usage, summary, group
        self.flags, self.short, self.aliases, self.browser, self.details = flags, short, aliases, browser, details


COMMANDS: dict[str, Cmd] = {}
ALIASES: dict[str, str] = {}
ALIAS_PRESETS: dict[str, list[str]] = {}
GROUPS = ["Navigate", "Look", "Act", "Wait", "Tabs & frames", "Debug & data", "Session", "Scripting"]


def command(name, usage, summary, group, flags=None, short=None, aliases=(), browser=True, details=""):
    def deco(fn):
        COMMANDS[name] = Cmd(name, fn, usage, summary, group, flags or {}, short or {}, aliases, browser, details)
        for a in aliases:
            if isinstance(a, tuple):
                ALIASES[a[0]] = name
                ALIAS_PRESETS[a[0]] = list(a[1])
            else:
                ALIASES[a] = name
        return fn
    return deco


def parse_args(cmd: Cmd, argv: list[str]) -> tuple[list[str], dict]:
    pos: list[str] = []
    opts: dict = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            pos.extend(argv[i + 1:])
            break
        key = None
        val = None
        if a.startswith("--") and len(a) > 2:
            body = a[2:]
            key, _, val = body.partition("=")
            if not _:
                val = None
            key = key.replace("-", "_")
        elif re.fullmatch(r"-[A-Za-z]", a) and a[1] in cmd.short:
            key = cmd.short[a[1]]
        if key is None:
            pos.append(a)
            i += 1
            continue
        if key in ("no_snap", "nosnap"):
            opts["no_snap"] = True
            i += 1
            continue
        if key.startswith("no_") and cmd.flags.get(key[3:]) is bool:
            opts[key[3:]] = False
            i += 1
            continue
        if key not in cmd.flags:
            raise UsageError(f"unknown option --{key.replace('_', '-')} for `{cmd.name}`")
        typ = cmd.flags[key]
        if typ is bool:
            opts[key] = True if val is None else val.lower() not in ("0", "false", "no")
        else:
            if val is None:
                if i + 1 >= len(argv):
                    raise UsageError(f"--{key.replace('_', '-')} needs a value")
                i += 1
                val = argv[i]
            try:
                opts[key] = typ(val)
            except ValueError:
                raise UsageError(f"--{key.replace('_', '-')} expects {typ.__name__}, got {val!r}")
        i += 1
    return pos, opts


def ms_arg(s: str) -> int | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s)?", s.strip().lower())
    if not m:
        return None
    v = float(m.group(1))
    return int(v * 1000) if m.group(2) == "s" else int(v)


# ─── Command implementations ─────────────────────────────────────────────────

def _timeout(opts) -> int:
    return opts.get("timeout") or env_int("CB_TIMEOUT_MS", 5000)


async def _page(ctx, rt):
    return await rt.get_page(ctx)


async def _act(ctx: Ctx, rt: Runtime, opts: dict, fn, target: Target | None = None, done: str = "",
               full: bool = False, echo: bool = False):
    """echo=True: the action message already states the target's new value, so its own diff line is hidden."""
    """Run an action, then report what changed."""
    page = await _page(ctx, rt)
    before = await capture_before(rt, page)
    try:
        result = await fn()
    except CBError:
        raise
    except Exception as e:
        raise CBError(clean_error(e))
    msg = done or ""
    if isinstance(result, str) and result:
        msg = result
    if msg:
        ctx.out(msg)
    if opts.get("no_snap"):
        ctx.no_snap = True
    await observe(ctx, rt, before, target_ref=target.ref if (target and echo) else None, full=full)


# ── Look ─────────────────────────────────────────────────────────────────────

@command("snap", "snap [-i] [-v] [--in <target>] [--urls] [--shot] [--max N | --all] [--diff]",
         "page outline with [ref] numbers for every interactive element (start here)", "Look",
         flags={"interactive": bool, "viewport": bool, "in": str, "urls": bool, "shot": bool, "max": int,
                "all": bool, "diff": bool, "forms": bool, "full": bool},
         short={"i": "interactive", "v": "viewport", "s": "shot", "a": "all"},
         aliases=("snapshot", "look", "outline", ("forms", ["--forms"])),
         details="""Prints the page as an indented outline: headings (#), text, and one line per interactive
element as `[ref] role "name" = "value" (states)`. Refs are stable: the same element keeps the
same number across snapshots until the page navigates, so you can keep using them.

  -i, --interactive   only interactive elements, headings and containers (compact)
  -v, --viewport      only what is currently on screen
  --in <target>       only the subtree of one element (e.g. a dialog or form ref)
  --urls              show link destinations
  --forms             only form controls and buttons
  --shot              also save an annotated screenshot (boxes labelled with refs)
  --max N / --all     character budget (default 12000; CB_SNAP_MAX) / no budget
  --diff              show only what changed since the previous snapshot

Iframes are inlined under `[ref] iframe:` lines; refs inside them work like any other.""")
async def cmd_snap(ctx, rt, pos, opts):
    page = await _page(ctx, rt)
    scope = None
    if opts.get("in"):
        scope = (await resolve(ctx, rt, page, opts["in"])).loc
    snap = await take_snapshot(rt, page, viewport=opts.get("viewport", False), urls=opts.get("urls", False),
                               scope_loc=scope)
    prev = (rt.state.get("snap") or {}).get("lines")
    full_lines = snap["lines"]
    if opts.get("diff") and prev is not None and not scope:
        new = remember_snapshot(rt, page, snap)
        d = diff_lines(prev, new)
        report_events(ctx, rt)
        if not d:
            ctx.out("(no change since last snapshot)")
        for x in d:
            ctx.out(x)
        return
    if not scope and not opts.get("viewport"):
        remember_snapshot(rt, page, snap)
    else:
        rt.state.setdefault("refs", {}).update(snap["refs"])
    report_events(ctx, rt)
    lines = filter_lines(full_lines, interactive=opts.get("interactive", False), forms=opts.get("forms", False))
    max_chars = 0 if opts.get("all") else opts.get("max")
    print_snapshot(ctx, rt, page, snap, lines=lines, max_chars=max_chars)
    if opts.get("shot"):
        path = await annotated_shot(rt, page, full=opts.get("full", False))
        ctx.out(f"Screenshot: {path}")


async def annotated_shot(rt, page, full=False, path=None) -> Path:
    path = path or shots_dir() / f"snap-{time.strftime('%H%M%S')}-{rt.bump() % 1000:03d}.png"
    frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame][:10]
    try:
        for f in frames:
            try:
                await page_call(f, "overlay", True, None, full)
            except Exception:
                pass
        await page.screenshot(path=str(path), full_page=full, scale="css", animations="disabled", caret="initial")
    finally:
        for f in frames:
            try:
                await page_call(f, "overlay", False)
            except Exception:
                pass
    _prune_shots()
    return path


def _prune_shots(keep: int = 60) -> None:
    try:
        files = sorted(shots_dir().glob("*.png"), key=lambda p: p.stat().st_mtime)
        for f in files[:-keep]:
            f.unlink()
    except OSError:
        pass


@command("shot", "shot [<target>] [-a] [--full] [--out FILE]",
         "screenshot (path printed; open it with your image viewer/Read tool)", "Look",
         flags={"annotate": bool, "full": bool, "out": str, "timeout": int},
         short={"a": "annotate", "f": "full"}, aliases=("screenshot", "ss"),
         details="""Saves a PNG at CSS-pixel scale, so pixel coordinates in the image are exactly what
`cb click X,Y` expects (for non --full shots).

  <target>        screenshot just that element
  -a, --annotate  draw ref numbers on interactive elements (same refs as `cb snap`)
  --full          whole scrollable page
  --out FILE      where to save (default: ~/.cb/shots/, CB_SHOT_DIR)""")
async def cmd_shot(ctx, rt, pos, opts):
    page = await _page(ctx, rt)
    out = opts.get("out")
    if pos and re.search(r"\.(png|jpe?g)$", pos[-1], re.I) and not out:
        out = pos.pop()  # legacy: `cb screenshot name.png`
    path = ctx.path(out) if out else shots_dir() / f"shot-{time.strftime('%H%M%S')}-{rt.bump() % 1000:03d}.png"
    if pos:
        t = await resolve(ctx, rt, page, " ".join(pos), timeout=_timeout(opts))
        if t.xy:
            raise UsageError("shot takes an element target, not coordinates")
        try:
            await t.loc.screenshot(path=str(path), scale="css", timeout=_timeout(opts))
        except Exception as e:
            raise CBError(clean_error(e))
        ctx.out(f"{path}  ({t.desc()})")
        return
    if opts.get("annotate"):
        if not rt.state.get("snap") or _strip_hash(rt.state["snap"].get("url", "")) != _strip_hash(page.url):
            snap = await take_snapshot(rt, page)
            remember_snapshot(rt, page, snap)
        path = await annotated_shot(rt, page, full=opts.get("full", False), path=path)
    else:
        try:
            await page.screenshot(path=str(path), full_page=opts.get("full", False), scale="css")
        except Exception as e:
            raise CBError(clean_error(e))
        _prune_shots()
    vp = page.viewport_size or {}
    ctx.out(f"{path}" + (f"  ({vp.get('width')}x{vp.get('height')} viewport)" if vp else ""))


@command("read", "read [<target>] [--links] [--all] [--max N] [--offset N]",
         "page text as markdown (articles, results, docs) — no refs, cheap", "Look",
         flags={"links": bool, "all": bool, "max": int, "offset": int, "images": bool, "timeout": int},
         aliases=("text", "content"),
         details="""Readable text of the main content (or of <target>), as markdown: headings, paragraphs,
lists, tables. Picks <main>/<article> when present; --all reads the whole body.

  --links       include link URLs as [text](url)
  --max N       characters to print (default 20000); --offset N to continue""")
async def cmd_read(ctx, rt, pos, opts):
    page = await _page(ctx, rt)
    scope = None
    if pos:
        scope = (await resolve(ctx, rt, page, " ".join(pos), timeout=_timeout(opts))).loc
    snap = await take_snapshot(rt, page, mode="read", scope_loc=scope, links=opts.get("links", False),
                               all_text=opts.get("all", False), images=opts.get("images", False))
    out: list[str] = []
    for ln in snap["lines"]:
        t = ln["t"]
        if ln["k"] == "h" and out and out[-1] != "":
            out.append("")
        out.append(("  " * max(0, ln["d"]) if t.startswith("- ") else "") + t)
        if ln["k"] == "h":
            out.append("")
    text = "\n".join(out).strip()
    if not text:
        ctx.out("(no readable text)")
        return
    off = opts.get("offset") or 0
    mx = opts.get("max") or env_int("CB_READ_MAX", 20000)
    chunk = text[off:off + mx]
    if not pos and not off:
        ctx.out(f"Page: {snap.get('title') or '(untitled)'}\nURL: {page.url}\n")
    ctx.out(chunk)
    if off + mx < len(text):
        ctx.out(f"\n(... {len(text) - off - mx} more chars; `cb read --offset {off + mx}`)")


@command("html", "html [<target>] [--raw] [--max N]", "HTML of the page or an element (scripts/styles stripped)",
         "Look", flags={"raw": bool, "max": int, "timeout": int}, aliases=("source",))
async def cmd_html(ctx, rt, pos, opts):
    page = await _page(ctx, rt)
    if pos:
        t = await resolve(ctx, rt, page, " ".join(pos), timeout=_timeout(opts))
        html = await t.loc.evaluate("el => el.outerHTML")
    else:
        frame = await rt.scope_frame(page)
        html = await frame.content()
    html = re.sub(r'\s+data-cb-ref="\d+"', "", html)
    if not opts.get("raw"):
        html = re.sub(r"(<script\b[^>]*>)[\s\S]*?(</script>)", r"\1…\2", html, flags=re.I)
        html = re.sub(r"(<style\b[^>]*>)[\s\S]*?(</style>)", r"\1…\2", html, flags=re.I)
        html = re.sub(r"<!--[\s\S]*?-->", "", html)
        html = re.sub(r'\s(?:d|points)="[^"]{200,}"', ' d="…"', html)
        html = re.sub(r"\n\s*\n+", "\n", html)
    mx = opts.get("max") or 15000
    if len(html) > mx:
        html = html[:mx] + f"\n... [truncated, {len(html) - mx} more chars; --max N]"
    ctx.out(html)


@command("find", "find <text> [--limit N]", "list elements matching text, best first, with refs", "Look",
         flags={"limit": int}, aliases=("search",))
async def cmd_find(ctx, rt, pos, opts):
    if not pos:
        raise UsageError("what should I look for?")
    page = await _page(ctx, rt)
    query = " ".join(pos)
    shown = 0
    for m, chain in (await find_everywhere(rt, page, query, limit=opts.get("limit") or 10))[:opts.get("limit") or 10]:
        rt.state.setdefault("refs", {})[str(m["ref"])] = {"role": m["role"], "name": m["name"], "tag": m["tag"], "f": chain}
        where = f" (in iframe [{chain[-1]}])" if chain else ""
        ctx.out(f"[{m['ref']}] {m['role']} \"{m['name'][:80]}\"{'' if m['vis'] else ' (off-screen)'}"
                f"{' (disabled)' if m['disabled'] else ''}{where}")
        shown += 1
    try:
        n = await scope_for_chain(page, rt.frame_chain).get_by_text(query).count()
    except Exception:
        n = 0
    if n:
        ctx.out(f"text occurrences on page: {n}" + ("" if shown else "  (not interactive; `cb read` to see them, `cb click` still works)"))
    if not shown and not n:
        ctx.out(f"nothing matches {query!r}")


@command("links", "links [filter]", "list links (with refs and URLs), optionally filtered", "Look",
         flags={"max": int})
async def cmd_links(ctx, rt, pos, opts):
    page = await _page(ctx, rt)
    snap = await take_snapshot(rt, page, urls=True)
    rt.state.setdefault("refs", {}).update(snap["refs"])
    flt = " ".join(pos).lower()
    n = 0
    for ln in snap["lines"]:
        t = re.sub(r"^(- )?(#+ )?", "", ln["t"])
        if ln["k"] == "i" and re.match(r"\[\d+\] link\b", t) and (not flt or flt in t.lower()):
            ctx.out(t)
            n += 1
            if n >= (opts.get("max") or 300):
                ctx.out("...")
                break
    if not n:
        ctx.out("(no links)" if not flt else f"(no links matching {flt!r})")


@command("ax", "ax [<target>]", "Playwright ARIA snapshot (YAML) of the page or an element", "Look",
         flags={"all": bool}, aliases=("aria",))
async def cmd_ax(ctx, rt, pos, opts):
    page = await _page(ctx, rt)
    if pos:
        loc = (await resolve(ctx, rt, page, " ".join(pos))).loc
    else:
        loc = scope_for_chain(page, rt.frame_chain).locator("body")
    try:
        ctx.out(await loc.aria_snapshot())
    except Exception as e:
        raise CBError(clean_error(e))


@command("eval", "eval <js> [--on <target>] [--file F]", "run JavaScript; prints the result (JSON for objects)",
         "Debug & data", flags={"on": str, "file": str, "timeout": int}, aliases=("js",),
         details="""Evaluates an expression, statements (`const a = 1; return a + 1` works) or a function.
With --on <target>, the code receives the element: `cb eval "el => el.value" --on 12`.
Use `-` to read the code from stdin.""")
async def cmd_eval(ctx, rt, pos, opts):
    if opts.get("file"):
        code = ctx.path(opts["file"]).read_text(encoding="utf-8")
    elif pos == ["-"]:
        code = ctx.stdin or ""
    else:
        code = " ".join(pos)
    if not code.strip():
        raise UsageError("no JavaScript given")
    page = await _page(ctx, rt)
    before = await capture_before(rt, page)
    is_fn = bool(re.match(r"^\s*(async\s+)?(\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>|^\s*(async\s+)?function\b", code))
    try:
        if opts.get("on"):
            t = await resolve(ctx, rt, page, opts["on"])
            fn = code if is_fn else f"(el) => ({code})"
            result = await t.loc.evaluate(fn, timeout=_timeout(opts))
        else:
            frame = await rt.scope_frame(page)
            try:
                result = await frame.evaluate(code)
            except Exception as e:
                if not is_fn and re.search(r"SyntaxError|Illegal return|await is only valid|Unexpected", str(e)):
                    result = await frame.evaluate(f"(async () => {{\n{code}\n}})()")
                else:
                    raise
    except CBError:
        raise
    except Exception as e:
        raise CBError(clean_error(e))
    if isinstance(result, (dict, list)):
        s = json.dumps(result, ensure_ascii=False)
        ctx.out(s if len(s) < 200 else json.dumps(result, indent=2, ensure_ascii=False))
    elif result is None:
        ctx.out("undefined" if result is None else "null")
    elif isinstance(result, bool):
        ctx.out("true" if result else "false")
    else:
        ctx.out(str(result))
    report_events(ctx, rt)
    if page.url != before["url"]:
        ctx.out(f"-> {page.url}")


# ── Act ──────────────────────────────────────────────────────────────────────

@command("click", "click <target> [--double] [--right] [--force] [--js] [--mod Shift,Control]",
         "click an element (ref, text, selector) or x,y coordinates", "Act",
         flags={"double": bool, "right": bool, "middle": bool, "count": int, "force": bool, "js": bool,
                "mod": str, "timeout": int, "button": str, "clicks": int},
         aliases=(("dblclick", ["--double"]), ("rightclick", ["--right"]), "tap", "coord_click", "press_button"),
         details="""Targets (anywhere a <target> is accepted):
  12 / [12] / e12       ref from `cb snap` (most reliable)
  "Sign in"             visible text, label, placeholder or accessible name
  css / xpath / role=   button.primary · //div[2] · role=button[name="Save"] · text=Next
  340,120               viewport coordinates (CSS px, same as `cb shot` pixels)

After clicking, cb waits for the page to settle and prints what changed (or the new page).
  --force   skip actionability checks (overlays, animations)
  --js      dispatch element.click() in JS (last resort for stubborn elements)""")
async def cmd_click(ctx, rt, pos, opts):
    if len(pos) == 2 and all(re.fullmatch(r"-?\d+(\.\d+)?", p) for p in pos):
        pos = [f"{pos[0]},{pos[1]}"]  # legacy `coord_click X Y`
    if not pos:
        raise UsageError("what should I click?")
    page = await _page(ctx, rt)
    t = await resolve(ctx, rt, page, " ".join(pos), timeout=_timeout(opts))
    button = "right" if opts.get("right") else "middle" if opts.get("middle") else (opts.get("button") or "left")
    if button not in ("left", "right", "middle"):
        raise UsageError("--button must be left, right or middle")
    count = opts.get("count") or opts.get("clicks") or (2 if opts.get("double") else 1)
    mods = [m.strip().capitalize().replace("Ctrl", "Control").replace("Cmd", "Meta")
            for m in (opts.get("mod") or "").replace("+", ",").split(",") if m.strip()]
    verb = {1: "clicked", 2: "double-clicked"}.get(count, f"clicked x{count}") if button == "left" else f"{button}-clicked"

    async def go():
        if t.xy:
            await page.mouse.click(t.xy[0], t.xy[1], button=button, click_count=count)
            return f"{verb} at {t.desc()}"
        if opts.get("js"):
            await t.loc.evaluate("el => el.click()", timeout=_timeout(opts))
        else:
            timeout = _timeout(opts)
            if not opts.get("force") and not opts.get("timeout"):
                if await t.loc.is_disabled(timeout=timeout):
                    timeout = min(timeout, 2000)  # don't sit out the full timeout on a disabled button
                else:
                    try:  # a dry run reports an overlay quickly instead of retrying until the timeout
                        await t.loc.click(trial=True, timeout=min(timeout, 1500), button=button)
                    except Exception as e:
                        if "intercepts pointer events" in str(e):
                            raise CBError(clean_error(e))
            await t.loc.click(timeout=timeout, force=opts.get("force", False), button=button,
                              click_count=count, modifiers=mods or None)
        return f"{verb} {t.desc()}"
    await _act(ctx, rt, opts, go, t)


async def _checked_state(loc) -> bool | None:
    return await loc.evaluate("""el => {
        const c = (el.localName === 'label' && el.control) ? el.control : el;
        if (c.localName === 'input' && (c.type === 'checkbox' || c.type === 'radio')) return c.checked;
        const inner = c.querySelector && c.querySelector('input[type=checkbox],input[type=radio]');
        if (inner && !c.hasAttribute('aria-checked')) return inner.checked;
        const a = c.getAttribute('aria-checked') ?? c.getAttribute('aria-pressed') ?? c.getAttribute('aria-selected');
        return a === null ? null : a === 'true';
    }""")


async def set_checked(t: Target, want: bool, timeout: int) -> str:
    loc = t.loc
    state = await _checked_state(loc)
    if state is want:
        return f"{t.desc()} already {'checked' if want else 'unchecked'}"
    try:
        await loc.set_checked(want, timeout=min(timeout, 3000))
    except Exception:
        try:
            await loc.click(timeout=timeout)
        except Exception:
            await loc.click(timeout=timeout, force=True)
    await asyncio.sleep(0.05)
    state = await _checked_state(loc)
    if state is not None and state is not want:
        await loc.click(timeout=timeout, force=True)
        state = await _checked_state(loc)
    if state is not None and state is not want:
        raise CBError(f"{t.desc()} did not change state (still {'checked' if state else 'unchecked'})")
    return f"{'checked' if want else 'unchecked'} {t.desc()}"


async def select_options(page, t: Target, values: list[str], timeout: int) -> str:
    info = t.info or {}
    if info.get("tag") == "select":
        res = await el_call(t.loc, "selectOptions", values, timeout=timeout)
        if res.get("error") is not None:
            raise CBError(f"{t.desc()} has no option {res['error']!r}. Options: " + " | ".join(res["options"]))
        await t.loc.select_option(value=res["values"], timeout=timeout)
        return f"selected {', '.join(repr(v) for v in values)} in {t.desc()}"
    # Custom dropdown: open it, then click the option by text.
    await t.loc.click(timeout=timeout)
    picked = []
    for v in values:
        rx = re.compile(re.escape(v), re.I)
        loc = None
        for cand in (page.get_by_role("option", name=rx), page.get_by_role("menuitem", name=rx),
                     page.get_by_role("menuitemradio", name=rx), page.get_by_role("treeitem", name=rx),
                     page.get_by_text(v, exact=True), page.get_by_text(rx)):
            try:
                await cand.first.wait_for(state="visible", timeout=1500)
                n = await cand.count()
                for i in range(n):
                    if await cand.nth(i).is_visible():
                        loc = cand.nth(i)
                        break
            except Exception:
                continue
            if loc is not None:
                break
        if loc is None:
            raise CBError(f"opened {t.desc()} but found no visible option {v!r}. Run `cb snap` to see the list.")
        await loc.click(timeout=timeout)
        picked.append(v)
    return f"selected {', '.join(repr(v) for v in picked)} in {t.desc()}"


async def upload_files(page, t: Target, files: list[str], timeout: int) -> str:
    info = t.info or {}
    if (info.get("tag") == "input" and info.get("type") == "file") or (info.get("ctl") or {}).get("type") == "file":
        await t.loc.set_input_files(files, timeout=timeout)
    else:
        async with page.expect_file_chooser(timeout=timeout) as fc:
            await t.loc.click(timeout=timeout)
        chooser = await fc.value
        await chooser.set_files(files)
    return f"uploaded {', '.join(Path(f).name for f in files)} via {t.desc()}"


TRUTHY = {"1", "true", "yes", "y", "on", "checked", "check", "x"}
FALSY = {"0", "false", "no", "n", "off", "unchecked", "uncheck", ""}


async def set_value(ctx, rt, page, t: Target, value, timeout: int, submit: bool = False) -> str:
    """Fill any kind of field: text, select, checkbox/radio/switch, file, contenteditable, custom combobox."""
    if t.xy:
        await page.mouse.click(*t.xy)
        await page.keyboard.press("ControlOrMeta+A")
        await page.keyboard.type(str(value))
        msg = f"typed {str(value)!r} at {t.desc()}"
    else:
        info = t.info or {}
        role, tag, typ = info.get("role", ""), info.get("tag", ""), info.get("type", "")
        ctl = info.get("ctl") or {}
        if tag == "label" and ctl:
            role, typ = ctl.get("role", role), ctl.get("type", typ)
            if ctl.get("tag") == "select":
                tag = "select"
        sval = str(value)
        if tag == "select" or role in ("select", "listbox") and tag == "select":
            vals = value if isinstance(value, list) else [sval]
            msg = await select_options(page, t, [str(v) for v in vals], timeout)
        elif role in INTENT_ROLES["check"] or typ in ("checkbox", "radio"):
            want = value if isinstance(value, bool) else sval.strip().lower() not in FALSY
            msg = await set_checked(t, want, timeout)
        elif typ == "file" or role == "file":
            vals = value if isinstance(value, list) else [sval]
            msg = await upload_files(page, t, [str(ctx.path(v)) for v in vals], timeout)
        elif role in ("combobox", "listbox") and tag not in ("input", "textarea"):
            msg = await select_options(page, t, [sval], timeout)
        else:
            try:
                await t.loc.fill(sval, timeout=timeout)
            except Exception as e:
                if "not an <input>" not in str(e) and "not editable" not in str(e).lower():
                    raise CBError(clean_error(e))
                await t.loc.click(timeout=timeout)
                await page.keyboard.press("ControlOrMeta+A")
                await page.keyboard.type(sval)
            shown = "•" * min(len(sval), 8) if typ == "password" else repr(sval if len(sval) < 80 else sval[:77] + "...")
            msg = f"filled {t.desc()} = {shown}"
    if submit:
        if t.loc is not None:
            await t.loc.press("Enter", timeout=timeout)
        else:
            await page.keyboard.press("Enter")
        msg += " and pressed Enter"
    return msg


@command("fill", "fill <target> <value> [--submit]",
         "set a field's value — text, <select>, checkbox (true/false), file, custom dropdown", "Act",
         flags={"submit": bool, "enter": bool, "timeout": int}, aliases=("set", "input"),
         details="""Replaces the field's value with proper input/change events (React-safe).
The element type is detected: selects pick the option by value/label, checkboxes take
true/false, file inputs take a path. --submit presses Enter afterwards.
Multi-word targets need quotes: cb fill "Email address" alex@example.com""")
async def cmd_fill(ctx, rt, pos, opts):
    if len(pos) < 2:
        if len(pos) == 1 and "=" in pos[0] and parse_ref(pos[0].split("=")[0]) is not None:
            pos = pos[0].split("=", 1)
        else:
            raise UsageError("need a target and a value")
    page = await _page(ctx, rt)
    t = await resolve(ctx, rt, page, pos[0], intent="fill", timeout=_timeout(opts))
    value = " ".join(pos[1:])

    async def go():
        return await set_value(ctx, rt, page, t, value, _timeout(opts), submit=opts.get("submit") or opts.get("enter"))
    await _act(ctx, rt, opts, go, t, echo=True)


@command("type", "type <text> [--into <target>] [--delay ms] [--submit]",
         "type text with real key events into the focused element (or --into a target)", "Act",
         flags={"into": str, "delay": int, "submit": bool, "clear": bool, "timeout": int},
         details="""Unlike `fill`, sends individual key presses — use it for autocomplete widgets, editors,
or fields that ignore programmatic fills. --clear empties the field first.""")
async def cmd_type(ctx, rt, pos, opts):
    if not pos:
        raise UsageError("what should I type?")
    text = " ".join(pos)
    page = await _page(ctx, rt)
    t = None
    if opts.get("into"):
        t = await resolve(ctx, rt, page, opts["into"], intent="fill", timeout=_timeout(opts))

    async def go():
        if t is not None:
            if t.xy:
                await page.mouse.click(*t.xy)
            else:
                await t.loc.click(timeout=_timeout(opts))
            if opts.get("clear"):
                await page.keyboard.press("ControlOrMeta+A")
                await page.keyboard.press("Backspace")
        await page.keyboard.type(text, delay=opts.get("delay") or 0)
        if opts.get("submit"):
            await page.keyboard.press("Enter")
        return f"typed {text!r}" + (f" into {t.desc()}" if t else "") + (" and pressed Enter" if opts.get("submit") else "")
    await _act(ctx, rt, opts, go, t, echo=True)


KEY_ALIASES = {"esc": "Escape", "escape": "Escape", "enter": "Enter", "return": "Enter", "tab": "Tab",
               "space": "Space", "spacebar": "Space", "backspace": "Backspace", "bksp": "Backspace", "del": "Delete",
               "delete": "Delete", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
               "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
               "home": "Home", "end": "End", "pageup": "PageUp", "pagedown": "PageDown", "pgup": "PageUp",
               "pgdn": "PageDown", "insert": "Insert", "ins": "Insert",
               "ctrl": "Control", "control": "Control", "cmd": "Meta", "command": "Meta", "meta": "Meta",
               "win": "Meta", "super": "Meta", "alt": "Alt", "option": "Alt", "opt": "Alt", "shift": "Shift",
               "mod": "ControlOrMeta", "controlormeta": "ControlOrMeta"}


def normalize_key(k: str) -> str | None:
    parts = k.split("+") if k != "+" else ["+"]
    out = []
    for i, p in enumerate(parts):
        if p == "" and i == len(parts) - 1:
            out.append("+")
            continue
        low = p.lower()
        if low in KEY_ALIASES:
            out.append(KEY_ALIASES[low])
        elif re.fullmatch(r"f([1-9]|1[0-2])", low):
            out.append(low.upper())
        elif len(p) == 1:
            out.append(p)
        elif re.fullmatch(r"(Key[A-Z]|Digit\d|Numpad\w+|Arrow\w+|Page\w+|Audio\w+|Media\w+)", p):
            out.append(p)
        else:
            return None
    if len(out) > 1 and out[-1] in ("Control", "Shift", "Alt", "Meta", "ControlOrMeta") and len(parts) > 1:
        pass
    return "+".join(out)


@command("press", "press <key>... | press <target> <key>", "press keys: Enter, Tab, Escape, Control+A, ArrowDown...",
         "Act", flags={"times": int, "timeout": int}, aliases=("keys", "key"),
         details="""Keys go to the focused element. Several keys are pressed in order: `cb press Tab Tab Enter`.
Give a target first to focus it: `cb press 7 Enter`. Modifiers: Control/Ctrl, Shift, Alt, Meta/Cmd,
Mod (Control on Linux/Windows, Cmd on macOS). --times N repeats.""")
async def cmd_press(ctx, rt, pos, opts):
    if not pos:
        raise UsageError("which key?")
    page = await _page(ctx, rt)
    t = None
    keys = [normalize_key(k) for k in pos]
    if keys[0] is None or (len(pos) >= 2 and parse_ref(pos[0]) is not None):
        t = await resolve(ctx, rt, page, pos[0], timeout=_timeout(opts))
        keys = [normalize_key(k) for k in pos[1:]]
        if not keys:
            raise UsageError("which key?")
    bad = [pos[i + (1 if t else 0)] for i, k in enumerate(keys) if k is None]
    if bad:
        raise UsageError(f"unknown key {bad[0]!r} (examples: Enter, Tab, Escape, ArrowDown, Control+A, F5, a)")
    times = max(1, opts.get("times") or 1)

    async def go():
        for _ in range(times):
            for k in keys:
                if t is not None and t.loc is not None:
                    await t.loc.press(k, timeout=_timeout(opts))
                else:
                    await page.keyboard.press(k)
        return f"pressed {' '.join(keys)}" + (f" x{times}" if times > 1 else "") + (f" on {t.desc()}" if t else "")
    await _act(ctx, rt, opts, go, t)


@command("select", "select <target> <option>...", "choose option(s) in a <select> or custom dropdown", "Act",
         flags={"timeout": int}, aliases=("choose",),
         details="Options match by value, exact label, then label prefix/substring (case-insensitive).")
async def cmd_select(ctx, rt, pos, opts):
    if len(pos) < 2:
        raise UsageError("need a target and an option")
    page = await _page(ctx, rt)
    t = await resolve(ctx, rt, page, pos[0], intent="select", timeout=_timeout(opts))
    values = pos[1:]
    if (t.info or {}).get("tag") == "select" and not (t.info or {}).get("multiple") and len(values) > 1:
        values = [" ".join(values)]

    async def go():
        return await select_options(page, t, values, _timeout(opts))
    await _act(ctx, rt, opts, go, t, echo=True)


@command("check", "check <target>", "tick a checkbox / radio / switch (works with custom widgets)", "Act",
         flags={"timeout": int})
async def cmd_check(ctx, rt, pos, opts):
    await _check(ctx, rt, pos, opts, True)


@command("uncheck", "uncheck <target>", "untick a checkbox / switch", "Act", flags={"timeout": int})
async def cmd_uncheck(ctx, rt, pos, opts):
    await _check(ctx, rt, pos, opts, False)


async def _check(ctx, rt, pos, opts, want):
    if not pos:
        raise UsageError("which checkbox?")
    page = await _page(ctx, rt)
    t = await resolve(ctx, rt, page, " ".join(pos), intent="check", timeout=_timeout(opts))

    async def go():
        return await set_checked(t, want, _timeout(opts))
    await _act(ctx, rt, opts, go, t, echo=True)


@command("upload", "upload <target> <file>...", "attach file(s) to a file input or an upload button", "Act",
         flags={"timeout": int}, aliases=("attach",),
         details="Works on <input type=file> (even hidden) and on buttons/labels that open a file chooser.")
async def cmd_upload(ctx, rt, pos, opts):
    if len(pos) < 2:
        raise UsageError("need a target and at least one file")
    files = [str(ctx.path(f)) for f in pos[1:]]
    for f in files:
        if not os.path.exists(f):
            raise CBError(f"file not found: {f}")
    page = await _page(ctx, rt)
    t = await resolve(ctx, rt, page, pos[0], intent="upload", timeout=_timeout(opts))

    async def go():
        return await upload_files(page, t, files, _timeout(opts))
    await _act(ctx, rt, opts, go, t, echo=True)


def _simple_action(name, usage, summary, verb, method, aliases=(), intent="click"):
    @command(name, usage, summary, "Act", flags={"timeout": int, "force": bool}, aliases=aliases)
    async def _cmd(ctx, rt, pos, opts):
        if len(pos) == 2 and all(re.fullmatch(r"-?\d+(\.\d+)?", p) for p in pos):
            pos = [f"{pos[0]},{pos[1]}"]
        if not pos:
            raise UsageError("missing target")
        page = await _page(ctx, rt)
        t = await resolve(ctx, rt, page, " ".join(pos), intent=intent, timeout=_timeout(opts))

        async def go():
            if t.xy:
                if method != "hover":
                    raise UsageError(f"{name} needs an element, not coordinates")
                await page.mouse.move(*t.xy)
            elif method == "hover":
                await t.loc.hover(timeout=_timeout(opts), force=opts.get("force", False))
            elif method == "focus":
                await t.loc.focus(timeout=_timeout(opts))
            elif method == "blur":
                await t.loc.evaluate("el => el.blur()")
            elif method == "clear":
                await t.loc.fill("", timeout=_timeout(opts))
            return f"{verb} {t.desc()}"
        await _act(ctx, rt, opts, go, t)
    return _cmd


_simple_action("hover", "hover <target>", "move the mouse over an element (menus, tooltips)", "hovered", "hover",
               aliases=("coord_move", "mouseover"))
_simple_action("focus", "focus <target>", "focus an element", "focused", "focus")
_simple_action("blur", "blur <target>", "remove focus from an element", "blurred", "blur")
_simple_action("clear", "clear <target>", "empty a text field", "cleared", "clear", intent="fill")


@command("scroll", "scroll [down|up|top|bottom|left|right] [px] [--in <target>] | scroll <target>",
         "scroll the page (or a scrollable box), or scroll an element into view", "Act",
         flags={"in": str, "timeout": int, "wheel": bool},
         details="""Default step is ~80% of the visible height. If the page itself doesn't scroll, the largest
scrollable box on screen is used (or name one with --in). Newly loaded content (infinite
scroll) appears in the printed changes.""")
async def cmd_scroll(ctx, rt, pos, opts):
    page = await _page(ctx, rt)
    dirs = {"up", "down", "top", "bottom", "left", "right"}
    direction = "down"
    px = 0
    t = None
    if pos and pos[0].lower() in dirs:
        direction = pos[0].lower()
        if len(pos) > 1 and re.fullmatch(r"\d+", pos[1]):
            px = int(pos[1])
    elif pos and len(pos) == 1 and re.fullmatch(r"-\d+", pos[0]):
        direction, px = "up", -int(pos[0])
    elif pos:
        t = await resolve(ctx, rt, page, " ".join(pos), timeout=_timeout(opts))
    box = None
    if opts.get("in"):
        box = await resolve(ctx, rt, page, opts["in"], timeout=_timeout(opts))

    async def go():
        if t is not None:
            if t.xy:
                raise UsageError("scroll <target> needs an element")
            await t.loc.scroll_into_view_if_needed(timeout=_timeout(opts))
            return f"scrolled {t.desc()} into view"
        if opts.get("wheel"):
            vp = page.viewport_size or {"width": 1280, "height": 800}
            if box is not None and box.loc is not None:
                await box.loc.hover(timeout=_timeout(opts))
            else:
                await page.mouse.move(vp["width"] / 2, vp["height"] / 2)
            step = px or int(vp["height"] * 0.8)
            dy = {"down": step, "up": -step, "bottom": 10 ** 6, "top": -10 ** 6}.get(direction, 0)
            dx = {"right": step, "left": -step}.get(direction, 0)
            await page.mouse.wheel(dx, dy)
            await asyncio.sleep(0.15)
            return f"wheel-scrolled {direction}"
        frame = await rt.scope_frame(page)
        if box is not None and box.loc is not None:
            r = await el_call(box.loc, "scroll", direction, px)
        else:
            r = await page_call(frame, "scroll", direction, px, None)
        where = "page" if r["page"] else r["box"]
        if direction in ("left", "right"):
            pos_s = f"x={r['x']} of {r['maxX']}"
        else:
            pos_s = f"y={r['y']} of {r['max']}" + (" (at bottom)" if r["y"] >= r["max"] - 2 else " (at top)" if r["y"] <= 0 else "")
        return f"scrolled {where} {direction}: {pos_s}" + ("" if r["moved"] else " (did not move)")
    await _act(ctx, rt, opts, go, t)


@command("drag", "drag <from> <to>", "drag and drop between elements or x,y points", "Act",
         flags={"steps": int, "timeout": int})
async def cmd_drag(ctx, rt, pos, opts):
    if len(pos) != 2:
        raise UsageError("need <from> and <to>")
    page = await _page(ctx, rt)
    a = await resolve(ctx, rt, page, pos[0], timeout=_timeout(opts))
    b = await resolve(ctx, rt, page, pos[1], timeout=_timeout(opts))

    async def center(t):
        if t.xy:
            return t.xy
        bb = await t.loc.bounding_box(timeout=_timeout(opts))
        if not bb:
            raise CBError(f"{t.desc()} has no box (hidden?)")
        return bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2

    async def go():
        if a.loc is not None and b.loc is not None and not opts.get("steps"):
            try:
                await a.loc.drag_to(b.loc, timeout=_timeout(opts))
                return f"dragged {a.desc()} to {b.desc()}"
            except Exception:
                pass
        (x1, y1), (x2, y2) = await center(a), await center(b)
        await page.mouse.move(x1, y1)
        await page.mouse.down()
        await page.mouse.move(x2, y2, steps=opts.get("steps") or 12)
        await page.mouse.up()
        return f"dragged {a.desc()} to {b.desc()}"
    await _act(ctx, rt, opts, go, a)


# ── Smart actions ────────────────────────────────────────────────────────────

ACT_VERBS = {"click": "click", "tap": "click", "open": "click", "press": "press", "hit": "click",
             "fill": "fill", "type": "fill", "enter": "fill", "set": "fill", "write": "fill", "input": "fill",
             "check": "check", "tick": "check", "uncheck": "uncheck", "untick": "uncheck", "toggle": "toggle",
             "select": "select", "choose": "select", "pick": "select",
             "upload": "upload", "attach": "upload", "hover": "hover", "focus": "focus", "clear": "clear",
             "go": "goto", "goto": "goto", "navigate": "goto", "visit": "goto"}


def parse_act(desc: str) -> tuple[str, str, str | None]:
    """'<verb> <target> [with|to|= <value>]' -> (verb, target, value)."""
    desc = desc.strip()
    if not desc:
        raise UsageError("empty description")
    first, _, rest = desc.partition(" ")
    verb = ACT_VERBS.get(first.lower())
    if verb is None:
        m = re.match(r"^(.+?)\s*=\s*(.+)$", desc)
        if m and not _ENGINE_RE.search(desc):
            return "fill", _unquote(m.group(1)), _unquote(m.group(2))
        verb, rest = "click", desc
    if verb == "goto":
        rest = re.sub(r"^to\s+", "", rest.strip(), flags=re.I)
        return "goto", rest, None
    value = None
    if verb in ("fill", "select", "upload"):
        m = re.search(r"\s+(?:with|to|as)\s+|\s*=\s*", rest, re.I)
        m2 = re.match(r"^(.+?)\s+(?:in|into|on|for)\s+(.+)$", rest, re.I)
        if m:
            value, rest = rest[m.end():], rest[:m.start()]
        elif m2:  # "type foo into Email", "select Canada in Country"
            value, rest = m2.group(1), m2.group(2)
    if verb == "press":
        m = re.match(r"^(.+?)\s+(?:on|in)\s+(.+)$", rest, re.I)
        if m:
            return "press", _unquote(m.group(2)), m.group(1).strip()
        return "press", "", rest.strip()
    rest = re.sub(r"^(the|on)\s+", "", rest.strip(), flags=re.I)
    rest = re.sub(r"\s+(button|link|field|checkbox|box|tab|input)$", "", rest, flags=re.I) or rest
    return verb, _unquote(rest), _unquote(value) if value is not None else None


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


@command("act", 'act "<verb> <target> [with <value>]"', "natural-language action: \"click Sign in\", \"fill Email with x@y.z\"",
         "Act", flags={"timeout": int}, aliases=("do",),
         details="""Verbs: click/tap/open, fill/type/enter/set, check/uncheck/toggle, select/choose/pick,
upload/attach, press, hover, focus, clear, go to. Also "<field> = <value>".
  cb act "click Sign in"            cb act "fill Email with alex@example.com"
  cb act "select Canada in Country" cb act "check I agree"     cb act "press Enter"
Every command already accepts text targets, so `cb click "Sign in"` is equivalent.""")
async def cmd_act(ctx, rt, pos, opts):
    verb, target, value = parse_act(" ".join(pos))
    fwd = ["--timeout", str(opts["timeout"])] if opts.get("timeout") else []
    if opts.get("no_snap"):
        fwd.append("--no-snap")
    if verb == "goto":
        return await dispatch(ctx, rt, ["goto", target] + fwd)
    if verb == "press":
        return await dispatch(ctx, rt, ["press"] + ([target] if target else []) + (value or "").split() + fwd)
    if verb in ("fill", "select", "upload"):
        if value is None:
            raise UsageError(f"'{verb}' needs a value: \"{verb} <target> with <value>\"")
        return await dispatch(ctx, rt, [verb, target] + ([value] if verb != "select" else [value]) + fwd)
    if verb == "toggle":
        page = await _page(ctx, rt)
        t = await resolve(ctx, rt, page, target, intent="check")
        st = await _checked_state(t.loc)
        verb = "uncheck" if st else "check"
    return await dispatch(ctx, rt, [verb, target] + fwd)


@command("fill_form", "fill_form <json | file.json> [--submit]",
         "fill many fields at once: {\"Email\": \"a@b.c\", \"Country\": \"Canada\", \"Agree\": true}", "Act",
         flags={"submit": bool, "timeout": int}, aliases=("form",),
         details="""Keys are field labels/placeholders/names (or refs like "12"); values are text, true/false for
checkboxes, an option for selects, a path for file inputs. Reports each field; --submit presses
Enter in the last field.""")
async def cmd_fill_form(ctx, rt, pos, opts):
    if not pos:
        raise UsageError("give a JSON object or a .json file")
    raw = " ".join(pos)
    try:
        p = ctx.path(raw)
        data = json.loads(p.read_text(encoding="utf-8")) if len(raw) < 400 and p.exists() else json.loads(raw)
    except ValueError as e:
        raise UsageError(f"invalid JSON: {e}")
    if not isinstance(data, dict):
        raise UsageError("fill_form expects a JSON object {field: value}")
    page = await _page(ctx, rt)
    before = await capture_before(rt, page)
    ok = 0
    last = None
    filled: set = set()
    for key, val in data.items():
        try:
            t = await resolve(ctx, rt, page, str(key), intent="fill", timeout=min(_timeout(opts), 3000))
            msg = await set_value(ctx, rt, page, t, val, _timeout(opts))
            ctx.out(f"  ok  {msg}")
            ok += 1
            last = t
            if t.ref is not None:
                filled.add(t.ref)
        except CBError as e:
            ctx.out(f"  ERR {key!r}: {str(e).splitlines()[0]}")
        except Exception as e:
            ctx.out(f"  ERR {key!r}: {clean_error(e).splitlines()[0]}")
    ctx.out(f"filled {ok}/{len(data)} fields")
    if opts.get("submit") and last is not None and last.loc is not None:
        await last.loc.press("Enter")
        ctx.out("pressed Enter")
    if opts.get("no_snap"):
        ctx.no_snap = True
    await observe(ctx, rt, before, skip_refs=filled)
    if ok < len(data):
        raise CBError(f"{len(data) - ok} field(s) failed")


# ── Navigate ─────────────────────────────────────────────────────────────────

def normalize_url(u: str, cwd: str) -> str:
    u = u.strip()
    drive = re.match(r"^[a-zA-Z]:[\\/]", u)  # C:\path\page.html is a file, not a "c:" URL
    if not drive and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", u) and not re.match(r"^[\w.-]+:\d+", u):
        return u
    p = Path(os.path.expanduser(u))
    if not p.is_absolute():
        p = Path(cwd) / p
    if (drive or u.startswith((".", "/", "~", "\\")) or u.endswith((".html", ".htm", ".svg", ".pdf"))) and p.exists():
        return p.resolve().as_uri()
    if re.match(r"^(localhost|127\.|0\.0\.0\.0|\[::1\]|192\.168\.|10\.)", u) or re.match(r"^[\w.-]+:\d+", u):
        return "http://" + u
    return "https://" + u


@command("goto", "goto <url> [--wait load|domcontentloaded|networkidle]", "navigate (prints the new page's outline)",
         "Navigate", flags={"wait": str, "timeout": int}, aliases=("open", "navigate", "nav", "go"),
         details="""Scheme is optional: `cb goto example.com` -> https, `cb goto localhost:3000` -> http,
`cb goto ./page.html` -> file. Waits for DOMContentLoaded plus a short network/DOM settle.""")
async def cmd_goto(ctx, rt, pos, opts):
    if not pos:
        raise UsageError("which URL?")
    url = normalize_url(pos[0], ctx.cwd)
    wait = opts.get("wait") or "domcontentloaded"
    if wait not in ("load", "domcontentloaded", "networkidle", "commit"):
        raise UsageError("--wait must be load, domcontentloaded, networkidle or commit")
    page = await _page(ctx, rt)

    async def go():
        resp = await page.goto(url, wait_until=wait, timeout=opts.get("timeout") or 30000)
        status = f" ({resp.status})" if resp is not None and resp.status >= 400 else ""
        return f"loaded {page.url}{status}"
    await _act(ctx, rt, opts, go, full=True)


def _history(name, summary, method):
    @command(name, name, summary, "Navigate", flags={"timeout": int})
    async def _cmd(ctx, rt, pos, opts):
        page = await _page(ctx, rt)

        async def go():
            url0 = page.url
            # wait_until="commit": pages restored from the back/forward cache never fire DOMContentLoaded.
            if method == "reload":
                await page.reload(wait_until="commit", timeout=opts.get("timeout") or 30000)
            else:
                r = await getattr(page, method)(wait_until="commit", timeout=opts.get("timeout") or 30000)
                if r is None and page.url == url0:
                    return f"{name}: nowhere to go (no history entry) — still at {page.url}"
            return f"{name} -> {page.url}"
        await _act(ctx, rt, opts, go, full=True)
    return _cmd


_history("back", "go back in history", "go_back")
_history("forward", "go forward in history", "go_forward")
_history("reload", "reload the page", "reload")


# ── Wait ─────────────────────────────────────────────────────────────────────

@command("wait", "wait <target> | <2s|500ms> | --text T | --url U | --gone <target> | --idle | --js EXPR",
         "wait for an element, text, URL, network idle, JS condition, or a duration", "Wait",
         flags={"text": str, "url": str, "gone": bool, "idle": bool, "js": str, "timeout": int},
         aliases=(("wait_hidden", ["--gone"]), ("wait_idle", ["--idle"]), "sleep", "wait_for"),
         details="""Default timeout 15s (--timeout ms). Prints what changed when it succeeds.
  cb wait "Order confirmed"        element/text to appear (ref, selector or text)
  cb wait --gone "Loading"         ... to disappear
  cb wait --url /dashboard         URL contains (or /regex/, or glob with *)
  cb wait --idle                   no network activity for 500ms
  cb wait --js "window.ready"      JS expression becomes truthy
  cb wait 2s                       fixed delay (last resort)""")
async def cmd_wait(ctx, rt, pos, opts, *, legacy: str | None = None):
    timeout = opts.get("timeout") or 15000
    if pos and re.fullmatch(r"\d+", pos[-1]) and not opts.get("timeout") and legacy != "sleep" and (
            len(pos) >= 2 or any(opts.get(k) is not None for k in ("idle", "text", "url", "js"))):
        timeout = int(pos.pop())  # legacy positional timeout: `wait_idle 5000`, `wait <sel> 5000`
    page = await _page(ctx, rt)
    before = await capture_before(rt, page)
    scope = scope_for_chain(page, rt.frame_chain)
    label = ""
    try:
        if opts.get("text") is not None:
            loc = scope.get_by_text(opts["text"]).first
            await loc.wait_for(state="hidden" if opts.get("gone") else "visible", timeout=timeout)
            label = f"text {opts['text']!r} {'gone' if opts.get('gone') else 'visible'}"
        elif opts.get("url") is not None:
            pat = opts["url"]
            if len(pat) > 2 and pat.startswith("/") and pat.endswith("/"):
                matcher = re.compile(pat[1:-1])
            elif "*" in pat:
                matcher = pat
            else:
                matcher = (lambda u, p=pat: p in u)
            await page.wait_for_url(matcher, timeout=timeout, wait_until="commit")
            label = f"URL is {page.url}"
        elif opts.get("idle"):
            await page.wait_for_load_state("networkidle", timeout=timeout)
            label = "network idle"
        elif opts.get("js") is not None:
            frame = await rt.scope_frame(page)
            await frame.wait_for_function(opts["js"], timeout=timeout)
            label = f"{opts['js']!r} is truthy"
        elif pos and ms_arg(" ".join(pos)) is not None and (legacy == "sleep" or re.search(r"[a-z]$", pos[0])):
            ms = ms_arg(" ".join(pos))
            await asyncio.sleep(ms / 1000)
            ctx.out(f"waited {ms}ms")
            return
        elif legacy == "sleep":
            await asyncio.sleep(1)
            ctx.out("waited 1000ms")
            return
        elif pos:
            spec = " ".join(pos)
            if opts.get("gone"):
                ref = parse_ref(spec)
                if ref is not None or looks_like_css(spec) or _ENGINE_RE.search(spec):
                    sel = f'[data-cb-ref="{ref}"]' if ref is not None else normalize_selector(spec)
                    await scope.locator(sel).first.wait_for(state="hidden", timeout=timeout)
                else:
                    await scope.get_by_text(spec).first.wait_for(state="hidden", timeout=timeout)
                label = f"{spec!r} is gone"
            else:
                t = await resolve(ctx, rt, page, spec, timeout=timeout)
                if t.loc is not None:
                    await t.loc.wait_for(state="visible", timeout=timeout)
                label = f"{t.desc()} is visible"
        else:
            raise UsageError("wait for what? (target, 2s, --text, --url, --gone, --idle, --js)")
    except (CBError, UsageError):
        raise
    except Exception as e:
        what = (f"text {opts['text']!r}" if opts.get("text") is not None else f"URL {opts['url']!r}" if opts.get("url") is not None
                else "network idle" if opts.get("idle") else f"{opts['js']!r}" if opts.get("js") is not None else repr(" ".join(pos)))
        state = "to disappear" if opts.get("gone") else "to be true" if opts.get("js") is not None else "" if opts.get("idle") else "to appear"
        raise CBError(f"timed out after {timeout}ms waiting for {what} {state}".rstrip()
                      + (f" ({clean_error(e).splitlines()[0]})" if "Timeout" not in str(e) else ""))
    ctx.out(f"ok: {label}")
    if opts.get("no_snap"):
        ctx.no_snap = True
    await observe(ctx, rt, before, settle=False)


@command("wait_url", "wait_url <substring|/regex/> [timeout_ms]", "legacy: wait for URL", "Wait", flags={"timeout": int})
async def cmd_wait_url(ctx, rt, pos, opts):
    if not pos:
        raise UsageError("which URL?")
    if len(pos) > 1 and re.fullmatch(r"\d+", pos[1]):
        opts["timeout"] = int(pos[1])
    opts["url"] = pos[0]
    await cmd_wait(ctx, rt, [], opts)


@command("wait_text", "wait_text <text> [timeout_ms]", "legacy: wait for visible text", "Wait", flags={"timeout": int})
async def cmd_wait_text(ctx, rt, pos, opts):
    if len(pos) > 1 and re.fullmatch(r"\d+", pos[-1]):
        opts["timeout"] = int(pos.pop())
    opts["text"] = " ".join(pos)
    await cmd_wait(ctx, rt, [], opts)


@command("wait_js", "wait_js <expr> [timeout_ms]", "legacy: wait for a JS condition", "Wait", flags={"timeout": int})
async def cmd_wait_js(ctx, rt, pos, opts):
    if len(pos) > 1 and re.fullmatch(r"\d+", pos[-1]):
        opts["timeout"] = int(pos.pop())
    opts["js"] = " ".join(pos)
    await cmd_wait(ctx, rt, [], opts)


# ── Tabs & frames ────────────────────────────────────────────────────────────

@command("tabs", "tabs", "list open tabs (* = active)", "Tabs & frames")
async def cmd_tabs(ctx, rt, pos, opts):
    page = await _page(ctx, rt)
    for i, p in enumerate(rt.pages()):
        try:
            title = await asyncio.wait_for(p.title(), 2)
        except Exception:
            title = "?"
        ctx.out(f"{i}{'*' if p is page else ' '} {title or '(untitled)'} — {p.url}")


@command("tab", "tab <n | text> | tab new [url] | tab close [n]", "switch to a tab by index or title/URL text; open/close tabs",
         "Tabs & frames")
async def cmd_tab(ctx, rt, pos, opts):
    if not pos:
        return await cmd_tabs(ctx, rt, pos, opts)
    if pos[0] == "new":
        return await cmd_new_tab(ctx, rt, pos[1:], opts)
    if pos[0] == "close":
        return await cmd_close_tab(ctx, rt, pos[1:], opts)
    await rt.ensure(ctx)
    pages = rt.pages()
    spec = " ".join(pos)
    target = None
    m = re.fullmatch(r"(\d+)(?::(\d+))?", spec)
    if m:
        i = int(m.group(2) if m.group(2) is not None else m.group(1))
        if m.group(2) is not None:  # legacy ctx:idx
            ctxs = rt.browser.contexts
            c = int(m.group(1))
            if c < len(ctxs) and i < len(ctxs[c].pages):
                target = ctxs[c].pages[i]
        elif i < len(pages):
            target = pages[i]
    else:
        low = spec.lower()
        for p in pages:
            try:
                title = (await p.title()).lower()
            except Exception:
                title = ""
            if low in title or low in p.url.lower():
                target = p
                break
    if target is None:
        raise CBError(f"no tab {spec!r}; `cb tabs` lists them")
    before = await capture_before(rt, await rt.get_page(ctx))
    rt._watch_page(target)
    rt.set_page(target)
    try:
        await target.bring_to_front()
    except Exception:
        pass
    ctx.out(f"switched to tab {pages.index(target)}")
    ctx.no_snap = bool(opts.get("no_snap"))
    await observe(ctx, rt, before, full=True, settle=False)


@command("new_tab", "new_tab [url]", "open a new tab (and switch to it)", "Tabs & frames",
         aliases=("newtab",))
async def cmd_new_tab(ctx, rt, pos, opts):
    await rt.ensure(ctx)
    old = await rt.get_page(ctx)
    contexts = rt.browser.contexts
    c = old.context if old else (contexts[0] if contexts else await rt.browser.new_context())
    page = await c.new_page()
    rt._watch_page(page)
    rt.set_page(page)
    rt.reported = rt.seq  # the "new tab" event is expected here
    before = await capture_before(rt, page)
    if pos:
        try:
            await page.goto(normalize_url(pos[0], ctx.cwd), wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            raise CBError(clean_error(e))
    ctx.out(f"opened tab {rt.pages().index(page)}: {page.url}")
    if pos:
        ctx.no_snap = bool(opts.get("no_snap"))
        before["url"] = "about:blank"
        await observe(ctx, rt, before, full=True)


@command("close_tab", "close_tab [n]", "close a tab (default: the active one)", "Tabs & frames", aliases=("closetab",))
async def cmd_close_tab(ctx, rt, pos, opts):
    page = await _page(ctx, rt)
    pages = rt.pages()
    target = page
    if pos:
        m = re.fullmatch(r"(\d+)(?::(\d+))?", pos[0])
        if not m:
            raise UsageError("close_tab takes a tab index")
        i = int(m.group(2) if m.group(2) is not None else m.group(1))
        if i >= len(pages):
            raise CBError(f"no tab {i}")
        target = pages[i]
    url = target.url
    await target.close()
    if target is page:
        rt.page = None
        rest = rt.pages()
        if rest:
            rt.set_page(rest[-1])
    rt.reported = rt.seq
    ctx.out(f"closed {url}" + (f"; active tab is now {rt.pages().index(rt.page)}: {rt.page.url}" if rt.page in rt.pages() else ""))


@command("frames", "frames", "list iframes on the page", "Tabs & frames")
async def cmd_frames(ctx, rt, pos, opts):
    page = await _page(ctx, rt)
    for i, f in enumerate(page.frames):
        depth = 0
        p = f.parent_frame
        while p is not None:
            depth += 1
            p = p.parent_frame
        ctx.out(f"{'  ' * depth}{i} {'(top) ' if f is page.main_frame else ''}{f.name or ''} {f.url}".rstrip())
    ctx.out("(iframes also appear inline in `cb snap`; their refs work directly)")


@command("frame", "frame <target> | frame top", "scope later commands to an iframe (rarely needed: refs work across frames)",
         "Tabs & frames", aliases=(("frame_reset", ["top"]),))
async def cmd_frame(ctx, rt, pos, opts):
    if not pos:
        raise UsageError("which iframe? (or `top`)")
    if pos[0] in ("top", "main", "reset", "none", "0"):
        rt.state["frame"] = None
        ctx.out("frame scope cleared (top document)")
        return
    page = await _page(ctx, rt)
    t = await resolve(ctx, rt, page, " ".join(pos))
    if t.loc is None or (t.info or {}).get("tag") not in ("iframe", "frame"):
        raise CBError(f"{t.desc()} is not an iframe")
    info = (rt.state.get("refs") or {}).get(str(t.ref)) or {}
    chain = list(info.get("f") or rt.frame_chain) + [t.ref]
    await frame_for_chain(page, chain)
    rt.state["frame"] = chain
    ctx.out(f"frame scope: iframe {' > '.join(f'[{r}]' for r in chain)}")


# ── Debug & data ─────────────────────────────────────────────────────────────

@command("console", "console [--errors] [--last N] [--clear]", "browser console messages and page errors", "Debug & data",
         flags={"errors": bool, "last": int, "clear": bool, "filter": str})
async def cmd_console(ctx, rt, pos, opts):
    await rt.get_page(ctx)
    items = [c for c in rt.console if not opts.get("errors") or c["type"] in ("error", "pageerror")]
    flt = (opts.get("filter") or " ".join(pos)).lower()
    if flt:
        items = [c for c in items if flt in c["text"].lower()]
    if opts.get("clear"):
        rt.console.clear()
        ctx.out("console log cleared")
        return
    if not rt.persistent:
        ctx.note("console history needs the daemon (unset CB_DAEMON=0); only this command's messages are shown")
    for c in items[-(opts.get("last") or 50):]:
        ctx.out(f"[{c['type']}] {c['text'][:1000]}" + (f"  ({c['where']})" if c["where"] else ""))
    if not items:
        ctx.out("(no console messages)")


@command("network", "network [filter] [--all] [--failed] [--body ID] [--last N] [--clear]",
         "recent requests (XHR/fetch/document by default); --body shows a response body", "Debug & data",
         flags={"all": bool, "failed": bool, "body": int, "last": int, "clear": bool}, aliases=("net", "requests"))
async def cmd_network(ctx, rt, pos, opts):
    await rt.get_page(ctx)
    if opts.get("clear"):
        rt.network.clear()
        ctx.out("network log cleared")
        return
    if opts.get("body") is not None:
        entry = next((n for n in rt.network if n["id"] == opts["body"]), {})
        body = entry.get("body")
        if body is None:
            resp = rt.responses.get(opts["body"])
            if resp is None:
                raise CBError(f"no stored response #{opts['body']} (only XHR/fetch/document bodies are kept)")
            try:
                body = await resp.text()
            except Exception as e:
                raise CBError(f"body no longer available ({clean_error(e)}); re-trigger the request and retry")
        try:
            body = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
        except ValueError:
            pass
        ctx.out(body[:30000] + (f"\n... [{len(body) - 30000} more chars]" if len(body) > 30000 else ""))
        return
    flt = " ".join(pos).lower()
    items = [n for n in rt.network
             if (opts.get("all") or n["type"] in ("xhr", "fetch", "document"))
             and (not opts.get("failed") or (n["status"] or 0) >= 400 or n["error"])
             and (not flt or flt in n["url"].lower())]
    if not rt.persistent:
        ctx.note("network history needs the daemon (unset CB_DAEMON=0)")
    for n in items[-(opts.get("last") or 40):]:
        st = n["status"] if n["status"] is not None else (n["error"] or "pending")
        ctx.out(f"#{n['id']} {n['method']} {st} {n['type']} {n['url'][:200]}" + (f" ({n['ms']}ms)" if n["ms"] is not None else ""))
    if not items:
        ctx.out("(no matching requests)")


@command("dialog", "dialog accept [text] | dismiss [--always]", "decide how the next alert/confirm/prompt is answered",
         "Debug & data", flags={"always": bool},
         details="""Dialogs are auto-accepted by default (and reported in the command output).
Run this *before* the action that triggers the dialog. --always keeps the policy.""")
async def cmd_dialog(ctx, rt, pos, opts):
    if not pos or pos[0] not in ("accept", "dismiss"):
        raise UsageError("dialog accept [prompt text] | dialog dismiss")
    rt.state["dialog"] = [pos[0], " ".join(pos[1:]) if len(pos) > 1 else None, bool(opts.get("always"))]
    ctx.out(f"next dialog will be {pos[0]}ed" + (" (always)" if opts.get("always") else ""))


@command("cookies", "cookies [name]", "list cookies (JSON)", "Debug & data")
async def cmd_cookies(ctx, rt, pos, opts):
    await rt.ensure(ctx)
    all_c = []
    for c in rt.browser.contexts:
        all_c.extend(await c.cookies())
    if pos:
        all_c = [c for c in all_c if c.get("name") == pos[0] or pos[0] in c.get("domain", "")]
    ctx.out(json.dumps(all_c, indent=2, ensure_ascii=False))


@command("cookies_save", "cookies_save <file>", "save all cookies to a JSON file", "Debug & data")
async def cmd_cookies_save(ctx, rt, pos, opts):
    if not pos:
        raise UsageError("which file?")
    await rt.ensure(ctx)
    all_c = []
    for c in rt.browser.contexts:
        all_c.extend(await c.cookies())
    p = ctx.path(pos[0])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(all_c, indent=2, ensure_ascii=False), encoding="utf-8")
    ctx.out(f"saved {len(all_c)} cookies to {p}")


@command("cookies_load", "cookies_load <file>", "restore cookies from a JSON file", "Debug & data")
async def cmd_cookies_load(ctx, rt, pos, opts):
    if not pos:
        raise UsageError("which file?")
    cookies = json.loads(ctx.path(pos[0]).read_text(encoding="utf-8"))
    await rt.ensure(ctx)
    c = rt.browser.contexts[0] if rt.browser.contexts else await rt.browser.new_context()
    await c.add_cookies(cookies)
    ctx.out(f"loaded {len(cookies)} cookies")


@command("storage", "storage [key] [--session]", "localStorage (or sessionStorage) dump / one key", "Debug & data",
         flags={"session": bool})
async def cmd_storage(ctx, rt, pos, opts):
    page = await _page(ctx, rt)
    store = "sessionStorage" if opts.get("session") else "localStorage"
    frame = await rt.scope_frame(page)
    if pos:
        ctx.out(str(await frame.evaluate(f"k => {store}.getItem(k)", pos[0])))
    else:
        data = await frame.evaluate(f"() => Object.fromEntries(Object.keys({store}).map(k => [k, {store}.getItem(k)]))")
        ctx.out(json.dumps(data, indent=2, ensure_ascii=False))


@command("downloads", "downloads", "files downloaded this session (saved under ~/.cb/downloads)", "Debug & data")
async def cmd_downloads(ctx, rt, pos, opts):
    files = sorted(downloads_dir().iterdir(), key=lambda p: p.stat().st_mtime)
    for f in files[-50:]:
        ctx.out(f"{f}  ({f.stat().st_size} bytes)")
    if not files:
        ctx.out(f"(no downloads in {downloads_dir()})")


@command("viewport", "viewport <width> <height>", "resize the page viewport (CSS px)", "Debug & data")
async def cmd_viewport(ctx, rt, pos, opts):
    if len(pos) != 2 or not all(p.isdigit() for p in pos):
        raise UsageError("viewport <width> <height>")
    page = await _page(ctx, rt)
    await page.set_viewport_size({"width": int(pos[0]), "height": int(pos[1])})
    ctx.out(f"viewport {pos[0]}x{pos[1]}")


@command("pdf", "pdf [file]", "save the page as PDF (headless browsers only)", "Debug & data")
async def cmd_pdf(ctx, rt, pos, opts):
    page = await _page(ctx, rt)
    path = ctx.path(pos[0]) if pos else shots_dir() / f"page-{time.strftime('%H%M%S')}.pdf"
    try:
        await page.pdf(path=str(path))
    except Exception as e:
        raise CBError(f"{clean_error(e)} (PDF printing needs a headless browser: `cb launch --headless --restart`)")
    ctx.out(str(path))


# ── Session ──────────────────────────────────────────────────────────────────

@command("launch", "launch [brave|chrome|edge|chromium] [--headless|--headed] [--profile DIR] [--restart] [-- browser args]",
         "start a browser with its own cb profile (most commands auto-launch anyway)", "Session",
         flags={"headless": bool, "headed": bool, "profile": str, "port": int, "restart": bool, "kill_existing": bool},
         details="""Starts the browser with remote debugging on a dedicated profile (~/.cb/profiles/<browser>),
so it never touches your everyday browser and logins persist between cb sessions.
Headless by default on Linux without a display.

  --profile DIR       use another profile dir; `--profile default` = the browser's own profile
                      (needs the browser fully closed; Chrome 136+ refuses this — use --kill-existing at your own risk)
  --restart           close the running cb browser first
  -- <args>           extra browser flags, e.g. `cb launch -- --proxy-server=host:3128 --lang=de`
  CB_BROWSER / CB_BROWSER_PATH / CB_BROWSER_ARGS / CB_CDP_PORT / CB_HEADLESS also apply.""")
async def cmd_launch(ctx, rt, pos, opts):
    extra = [p for p in pos if p.startswith("-")]
    pos = [p for p in pos if not p.startswith("-")]
    kind = pos[0].lower() if pos else None
    port = opts.get("port") or cdp_port() or None
    if port and _http_json(port):
        if not opts.get("restart"):
            info = browser_info()
            ver = (_http_json(port) or {}).get("Browser", "")
            ctx.out(f"browser already running on port {port}: {ver}"
                    + (f" (pid {info.get('pid')}, profile {info.get('profile')})" if info.get("port") == port else "")
                    + ". Use --restart to relaunch.")
            await rt.ensure(ctx)
            return
        await close_browser(rt, port)
    headless = True if opts.get("headless") else False if opts.get("headed") else None
    if opts.get("profile") == "default" and opts.get("kill_existing"):
        k, _ = find_browser(kind)
        _kill_by_name(PROCESS_NAMES[k])
        await asyncio.sleep(1)
    rec = launch_browser(kind, headless=headless, profile=opts.get("profile"), port=port, extra=extra)
    ctx.out(f"launched {rec['kind']} ({'headless' if rec['headless'] else 'headed'}) pid {rec['pid']}, "
            f"CDP port {rec['port']}, profile {rec['profile']}")
    await rt.ensure(ctx, launch=False)


async def close_browser(rt: Runtime, port: int | None) -> str:
    info = browser_info()
    port = port or cdp_port()
    closed = False
    if port and _http_json(port):
        try:
            await rt.ensure(None, launch=False)
            cdp = await rt.browser.new_browser_cdp_session()
            try:
                await cdp.send("Browser.close")
            except Exception:
                pass
            closed = True
        except Exception:
            pass
        for _ in range(50):
            if not _http_json(port, timeout=0.2):
                break
            await asyncio.sleep(0.1)
    pid = info.get("pid")
    if pid and info.get("port") == port and _is_cdp_browser(pid, port):
        _kill_pid_tree(pid)
        closed = True
    rt._on_disconnect()
    try:
        (session_dir() / "browser.json").unlink()
    except FileNotFoundError:
        pass
    for k in ("snap", "refs", "tab", "frame", "next_ref"):
        rt.state.pop(k, None)
    return "closed browser" if closed else "no browser was running"


@command("close", "close", "close the cb browser and stop the daemon", "Session", aliases=("quit", "stop", "exit"),
         browser=False)
async def cmd_close(ctx, rt, pos, opts):
    ctx.out(await close_browser(rt, None))
    ctx.exit_daemon = True


@command("kill", "kill [brave|chrome|edge|chromium] [--all]",
         "force-close the cb browser; with a browser name + --all, kill every process of it", "Session",
         flags={"all": bool}, browser=False)
async def cmd_kill(ctx, rt, pos, opts):
    if pos and opts.get("all"):
        k = pos[0].lower()
        if k not in PROCESS_NAMES:
            raise UsageError(f"unknown browser {k!r}")
        _kill_by_name(PROCESS_NAMES[k])
        rt._on_disconnect()
        ctx.out(f"killed all {k} processes")
        return
    if pos:
        ctx.note("killing only the cb-launched browser; add --all to kill every process of that browser")
    info = browser_info()
    if info.get("pid") and _is_cdp_browser(info["pid"], info.get("port")):
        _kill_pid_tree(info["pid"])
        ctx.out(f"killed {info.get('kind')} pid {info['pid']}")
    else:
        ctx.out(await close_browser(rt, None))
    rt._on_disconnect()
    try:
        (session_dir() / "browser.json").unlink()
    except FileNotFoundError:
        pass


@command("status", "status", "what's running: browser, daemon, active tab, frame scope", "Session", browser=False,
         aliases=("connect", "title", "info", "doctor"))
async def cmd_status(ctx, rt, pos, opts):
    port = cdp_port()
    ver = _http_json(port) if port else None
    info = browser_info()
    ctx.out(f"cb {VERSION} · session {session_name()} · {'daemon' if rt.persistent else 'no daemon'} · state {session_dir()}")
    if not ver:
        try:
            k, exe = find_browser()
            ctx.out(f"browser: not running (port {port or 'auto'}); `cb launch` or any command will start {k}: {exe}")
        except CBError as e:
            ctx.out(f"browser: not running; {e}")
        return
    ctx.out(f"browser: {ver.get('Browser')} on port {port}"
            + (f" · pid {info.get('pid')} · {'headless' if info.get('headless') else 'headed'} · profile {info.get('profile')}"
               if info.get("port") == port else " · (not launched by cb)"))
    page = await rt.get_page(ctx)
    pages = rt.pages()
    try:
        title = await page.title()
    except Exception:
        title = ""
    ctx.out(f"tab {pages.index(page) if page in pages else '?'} of {len(pages)}: {title or '(untitled)'} — {page.url}")
    if rt.frame_chain:
        ctx.out(f"frame scope: {rt.frame_chain}")
    snap = rt.state.get("snap") or {}
    if snap:
        ctx.out(f"last snapshot: {int(time.time() - snap.get('t', time.time()))}s ago, {len(snap.get('lines', []))} lines, "
                f"{len(rt.state.get('refs') or {})} refs")


@command("version", "version", "print the cb version", "Session", browser=False)
async def cmd_version(ctx, rt, pos, opts):
    ctx.out(f"cb {VERSION}")


@command("daemon", "daemon [status|stop|restart|log]", "manage the background daemon (auto-started; rarely needed)",
         "Session", browser=False)
async def cmd_daemon(ctx, rt, pos, opts):
    pass  # handled client-side in main()


# ── Scripting ────────────────────────────────────────────────────────────────

OBSERVE_CMDS = {"snap", "read", "html", "find", "links", "shot", "ax", "tabs", "status", "console", "network"}


def parse_script(text: str, name: str = "") -> list[list[str]]:
    """Batch script: one cb command per line (default), or legacy YAML/JSON list of {cmd: args}."""
    stripped = text.lstrip()
    legacy = None
    if name.endswith(".json") or stripped.startswith("["):
        legacy = json.loads(text)
    elif name.endswith((".yaml", ".yml")) or re.match(r"^\s*-\s+[\w_]+:", text, re.M) and not re.search(r"^\s*[a-z_]+\s", stripped.splitlines()[0] if stripped else "", re.M):
        try:
            import yaml  # type: ignore
        except ImportError:
            raise CBError("YAML scripts need PyYAML (pip install pyyaml); or write one cb command per line")
        legacy = yaml.safe_load(text)
    steps: list[list[str]] = []
    if legacy is not None:
        if not isinstance(legacy, list):
            raise UsageError("a YAML/JSON batch must be a list of {command: args} entries")
        for i, step in enumerate(legacy):
            if not isinstance(step, dict) or len(step) != 1:
                raise UsageError(f"step {i}: each entry must be a single-key map, got {step!r}")
            (cmd, raw), = step.items()
            if raw is None:
                args = []
            elif isinstance(raw, str):
                args = split_line(raw)
            elif isinstance(raw, dict):
                args = [json.dumps(raw, ensure_ascii=False)]
            elif isinstance(raw, list):
                args = [json.dumps(a) if isinstance(a, (dict, list)) else str(a) for a in raw]
            else:
                args = [str(raw)]
            steps.append([str(cmd)] + args)
        return steps
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = split_line(s)
        if parts and parts[0] == "cb":
            parts = parts[1:]
        if parts:
            steps.append(parts)
    return steps


def split_line(s: str) -> list[str]:
    lex = shlex.shlex(s, posix=True)
    lex.whitespace_split = True
    lex.escape = ""          # keep backslashes literal (Windows paths, regexes)
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError as e:
        raise UsageError(f"cannot parse line {s!r}: {e}")


@command("batch", "batch <file | -> [--continue] | batch \"cmd 1\" \"cmd 2\" ...",
         "run many commands in one call; prints one combined change report at the end", "Scripting",
         flags={"continue": bool, "verbose": bool}, short={"k": "continue"}, aliases=("run",), browser=False,
         details="""One cb command per line (a leading `cb` is optional, # starts a comment):

  cb batch <<'EOF'
  goto example.com/login
  fill Email alex@example.com
  fill Password hunter2
  click "Sign in"
  EOF

or inline: cb batch "fill 4 alex" "fill 5 secret" "click 6"
Intermediate page outlines are suppressed; the final state is reported once.
Legacy YAML/JSON lists ([{goto: url}, {click: "#btn"}]) still work. --continue keeps going after errors.""")
async def cmd_batch(ctx, rt, pos, opts):
    if not pos or pos == ["-"]:
        text, name = ctx.stdin or "", "<stdin>"
        if not text.strip():
            raise UsageError("no script: pass a file, `-` with stdin, or quoted commands")
    elif len(pos) == 1 and ctx.path(pos[0]).is_file():
        p = ctx.path(pos[0])
        text, name = p.read_text(encoding="utf-8"), str(p)
    else:
        text, name = "\n".join(pos), "<args>"
    steps = parse_script(text, name)
    if not steps:
        raise UsageError("script has no commands")
    ctx.batch = True
    failures = 0
    last_cmd = None
    for i, argv in enumerate(steps, 1):
        cname = ALIASES.get(argv[0], argv[0])
        if cname == "batch":
            raise UsageError("batch scripts cannot nest `batch`")
        ctx.out(f"[{i}] {' '.join(shlex.quote(a) for a in argv)}")
        try:
            await dispatch(ctx, rt, argv)
            last_cmd = cname
        except CBError as e:
            failures += 1
            ctx.out(f"  ERROR: {e}")
            if not opts.get("continue"):
                ctx.out(f"stopped at step {i} of {len(steps)} (--continue to keep going)")
                break
    before, echoed = ctx.pending_observe, ctx.echoed
    ctx.batch = False
    ctx.pending_observe = None
    ctx.echoed = set()
    if before is not None and last_cmd not in OBSERVE_CMDS:
        ctx.out("--")
        await observe(ctx, rt, before, settle=False, skip_refs=echoed)
    if failures:
        raise CBError(f"{failures} step(s) failed")


@command("help", "help [command]", "this list, or details for one command", "Scripting", browser=False)
async def cmd_help(ctx, rt, pos, opts):
    ctx.out(help_text(pos[0] if pos else None))


def help_text(name: str | None = None) -> str:
    if name:
        cname = ALIASES.get(name, name)
        c = COMMANDS.get(cname)
        if not c:
            close = difflib.get_close_matches(name, list(COMMANDS) + list(ALIASES), n=3)
            raise UsageError(f"no command {name!r}" + (f"; did you mean {', '.join(close)}?" if close else ""))
        al = [a[0] if isinstance(a, tuple) else a for a in c.aliases]
        s = f"cb {c.usage}\n  {c.summary}"
        if al:
            s += f"\n  aliases: {', '.join(al)}"
        if c.details:
            s += "\n\n" + c.details.strip()
        return s
    return OVERVIEW.replace("{VERSION}", VERSION)


OVERVIEW = """cb {VERSION} — a browser for LLM agents. Actions print what changed, so you rarely need to re-look.

  cb goto example.com          open a page; prints its outline with [ref]s
  cb click 12                  click ref 12 (or "Sign in", a CSS selector, or 340,120)
  cb fill 7 alex@example.com   set a field (text, select, checkbox, file — detected)
  cb snap                      re-print the outline (refs stay stable until navigation)

TARGET = 12 | [12] | "visible text or label" | css / xpath= / role= / text= selector | x,y

Look      snap [-i] [-v] [--in T] [--shot]   outline: headings, text, [ref] role "name" (states)
          read [T] [--links]                 page text as markdown (articles, results); cheapest
          find <text>                        best-matching elements with refs (search huge pages)
          shot [T] [-a] [--full]             screenshot path (-a: boxes labelled with refs)
          links [filter] · html [T] · ax [T] · eval <js> [--on T]
Act       click T [--double|--right|--force]  fill T <value> [--submit]   type <text> [--into T]
          press <key>... (Enter, Tab, Escape, Control+A)   select T <option>   check T / uncheck T
          upload T <file>   hover T   scroll [down|up|top|bottom|T]   drag T T   clear T   focus T
          fill_form '{"Email": "a@b.c", "Agree": true}'    act "fill Email with a@b.c"
Navigate  goto <url> · back · forward · reload
Wait      wait T | wait 2s | wait --text "Done" | --gone T | --url /path | --idle | --js "expr"
Tabs      tabs · tab <n|text> · tab new [url] · tab close [n] · frames · frame T|top
Debug     console [--errors] · network [filter] [--body ID] · dialog accept|dismiss · downloads
          cookies · cookies_save/cookies_load <file> · storage [key] · viewport W H · pdf [file]
Session   status · launch [chrome|edge|brave|chromium] [--headed] · close · kill · daemon
Scripts   batch <<'EOF' (one command per line) · batch "cmd 1" "cmd 2" — one change report at the end

Every action waits for the page to settle, then prints: new tab / dialog / download / console error
notices, and either the changed outline lines (+/-) or the whole new page after navigation.
Flags: --timeout ms · --no-snap (skip the report). `cb help <command>` for details.
Env: CB_SESSION=name (separate browser per agent) · CB_HEADLESS=0/1 · CB_BROWSER · CB_DAEMON=0"""


# ─── Dispatch ────────────────────────────────────────────────────────────────

async def dispatch(ctx: Ctx, rt: Runtime, argv: list[str]) -> None:
    if not argv:
        raise UsageError("no command")
    name = argv[0]
    cname = ALIASES.get(name, name)
    c = COMMANDS.get(cname)
    if c is None:
        close = difflib.get_close_matches(name, list(COMMANDS) + list(ALIASES), n=3)
        raise UsageError(f"unknown command {name!r}" + (f"; did you mean {', '.join(close)}?" if close else "")
                         + " (`cb help` lists commands)")
    args = ALIAS_PRESETS.get(name, []) + argv[1:]
    try:
        pos, opts = parse_args(c, args)
    except UsageError as e:
        raise UsageError(f"{e}\nusage: cb {c.usage}")
    saved = ctx.no_snap
    try:
        if cname == "wait" and name in ("sleep",):
            await cmd_wait(ctx, rt, pos, opts, legacy="sleep")
        else:
            await c.fn(ctx, rt, pos, opts)
    except UsageError as e:
        if "usage:" in str(e):
            raise
        raise UsageError(f"{e}\nusage: cb {c.usage}")
    finally:
        ctx.no_snap = saved
        if rt.state is not None:
            rt.save()


async def run_command(ctx: Ctx, rt: Runtime, argv: list[str]) -> int:
    token = _CTX_VAR.set(ctx)
    try:
        await dispatch(ctx, rt, argv)
        return 0
    except UsageError as e:
        ctx.err(f"ERROR: {e}")
        return 2
    except CBError as e:
        ctx.err(f"ERROR: {e}")
        return 1
    except asyncio.CancelledError:
        raise
    except Exception as e:
        ctx.err(f"ERROR: {clean_error(e)}")
        if _env("CB_DEBUG"):
            import traceback
            ctx.err(traceback.format_exc())
        return 1
    finally:
        rt.save()
        _CTX_VAR.reset(token)


# ─── Daemon server ───────────────────────────────────────────────────────────

async def daemon_main() -> None:
    import secrets
    sd = session_dir()
    token = secrets.token_hex(16)
    rt = Runtime(persistent=True)
    lock = asyncio.Lock()
    last_used = [time.time()]
    stop = asyncio.Event()
    idle = env_int("CB_DAEMON_IDLE", 3600)

    async def handle(reader, writer):
        try:
            line = await reader.readline()
            req = json.loads(line or b"{}")
        except Exception:
            writer.close()
            return
        if req.get("token") != token:
            writer.close()
            return

        def write(stream, text):
            try:
                writer.write((json.dumps({stream: text}) + "\n").encode("utf-8"))
            except Exception:
                pass

        argv = req.get("argv") or []
        if argv == ["__shutdown__"]:
            write("x", 0)
            await writer.drain()
            writer.close()
            stop.set()
            return
        async with lock:
            last_used[0] = time.time()
            ctx = Ctx(write, cwd=req.get("cwd"), env=req.get("env") or {}, stdin=req.get("stdin"))
            ctx.exit_daemon = False
            task = asyncio.ensure_future(run_command(ctx, rt, argv))
            gone = asyncio.ensure_future(reader.read(1))
            done, _ = await asyncio.wait({task, gone}, return_when=asyncio.FIRST_COMPLETED)
            if task not in done:
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                rt.save()
                code = 130
            else:
                gone.cancel()
                code = task.result()
            write("x", code)
            try:
                await writer.drain()
                writer.close()
            except Exception:
                pass
            last_used[0] = time.time()
            if getattr(ctx, "exit_daemon", False):
                stop.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    _write_json(sd / "daemon.json", {"pid": os.getpid(), "port": port, "token": token,
                                     "version": code_version(), "started": time.time()})
    try:
        os.chmod(sd / "daemon.json", 0o600)
    except OSError:
        pass

    async def idle_watch():
        while not stop.is_set():
            await asyncio.sleep(5)
            if not lock.locked() and time.time() - last_used[0] > idle:
                stop.set()
    watcher = asyncio.ensure_future(idle_watch())
    try:
        await stop.wait()
    finally:
        watcher.cancel()
        server.close()
        info = _read_json(sd / "daemon.json")
        if info.get("pid") == os.getpid():
            try:
                (sd / "daemon.json").unlink()
            except FileNotFoundError:
                pass
        rt.save()
        await rt.shutdown()


# ─── Entry point ─────────────────────────────────────────────────────────────

def _local_daemon_cmd(pos: list[str]) -> int:
    sd = session_dir()
    info = _read_json(sd / "daemon.json")
    sub = pos[0] if pos else "status"
    alive = bool(info) and _daemon_connect(info) is not None
    if sub == "status":
        if alive:
            print(f"daemon pid {info['pid']} on 127.0.0.1:{info['port']}, up {int(time.time() - info.get('started', time.time()))}s"
                  + ("" if info.get("version") == code_version() else " (stale code; restarts on next command)"))
        else:
            print("daemon not running (starts automatically on the next command)")
        return 0
    if sub in ("stop", "restart"):
        if alive:
            _daemon_stop(info)
            print("daemon stopped")
        if sub == "restart":
            sock, info = _ensure_daemon(sd)
            sock.close()
            print(f"daemon started (pid {info['pid']})")
        return 0
    if sub in ("log", "logs"):
        p = sd / "daemon.log"
        print(p.read_text(errors="replace")[-4000:] if p.exists() else "(no log)")
        return 0
    print("usage: cb daemon [status|stop|restart|log]", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    while argv and argv[0].startswith("--") and argv[0] in ("--session", "--no-daemon", "--headless", "--headed"):
        flag = argv.pop(0)
        if flag == "--session":
            if not argv:
                print("ERROR: --session needs a name", file=sys.stderr)
                return 2
            os.environ["CB_SESSION"] = argv.pop(0)
        elif flag == "--no-daemon":
            os.environ["CB_DAEMON"] = "0"
        elif flag == "--headless":
            os.environ["CB_HEADLESS"] = "1"
        elif flag == "--headed":
            os.environ["CB_HEADLESS"] = "0"
    if argv and argv[0].startswith("--session="):
        os.environ["CB_SESSION"] = argv.pop(0).split("=", 1)[1]
    if not argv or argv[0] in ("-h", "--help"):
        print(help_text())
        return 0
    if argv[0] in ("-V", "--version"):
        print(f"cb {VERSION}")
        return 0
    if argv[0] == "__daemon__":
        asyncio.run(daemon_main())
        return 0
    try:
        if argv[0] == "help":
            print(help_text(argv[1] if len(argv) > 1 else None))
            return 0
        if argv[0] == "version":
            print(f"cb {VERSION}")
            return 0
        if argv[0] == "daemon":
            return _local_daemon_cmd(argv[1:])
        if (os.environ.get("CB_DAEMON") or "1").lower() in ("0", "false", "no", "off"):
            return run_local(argv)
        return client_main(argv)
    except UsageError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except CBError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def run_local(argv: list[str]) -> int:
    stdin = sys.stdin.read() if _wants_stdin(argv) else None

    def write(stream, text):
        (sys.stdout if stream == "o" else sys.stderr).write(text)

    async def go():
        rt = Runtime(persistent=False)
        ctx = Ctx(write, stdin=stdin, env={})
        try:
            return await run_command(ctx, rt, argv)
        finally:
            await rt.shutdown()
    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
