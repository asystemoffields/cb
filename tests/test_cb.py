"""End-to-end tests: drive a real headless Chromium through the cb CLI (and its daemon)."""
import json
import re
import time
from pathlib import Path


import cb as cbmod


def ref_of(out: str, what: str) -> str:
    m = re.search(r"\[(\d+)\] " + re.escape(what), out)
    assert m, f"{what!r} not in:\n{out}"
    return m.group(1)


# ─── Unit tests (no browser) ─────────────────────────────────────────────────

def test_parse_ref():
    assert cbmod.parse_ref("12") == 12
    assert cbmod.parse_ref("#12") == 12
    assert cbmod.parse_ref("[12]") == 12
    assert cbmod.parse_ref("e12") == 12
    assert cbmod.parse_ref("ref=e12") == 12
    assert cbmod.parse_ref("@7") == 7
    assert cbmod.parse_ref("#main") is None
    assert cbmod.parse_ref("Sign in") is None


def test_looks_like_css():
    assert cbmod.looks_like_css("button.primary")
    assert cbmod.looks_like_css("#email")
    assert cbmod.looks_like_css("input[name=q]")
    assert cbmod.looks_like_css("form > button")
    assert cbmod.looks_like_css("button")
    assert not cbmod.looks_like_css("Sign in")
    assert not cbmod.looks_like_css("Submit")


def test_normalize_selector_digit_id():
    assert cbmod.normalize_selector("#1abc") == '[id="1abc"]'
    assert cbmod.normalize_selector("#abc") == "#abc"


def test_parse_act():
    assert cbmod.parse_act("click Sign in") == ("click", "Sign in", None)
    assert cbmod.parse_act("click the Submit button") == ("click", "Submit", None)
    assert cbmod.parse_act("fill Email with a@b.c") == ("fill", "Email", "a@b.c")
    assert cbmod.parse_act("Email = a@b.c") == ("fill", "Email", "a@b.c")
    assert cbmod.parse_act("select Canada in Country") == ("select", "Country", "Canada")
    assert cbmod.parse_act("select Country with Canada") == ("select", "Country", "Canada")
    assert cbmod.parse_act("type hello into Search") == ("fill", "Search", "hello")
    assert cbmod.parse_act("click text=Login") == ("click", "text=Login", None)
    assert cbmod.parse_act("press Enter") == ("press", "", "Enter")
    assert cbmod.parse_act("go to example.com") == ("goto", "example.com", None)
    assert cbmod.parse_act("Sign in") == ("click", "Sign in", None)


def test_normalize_key():
    assert cbmod.normalize_key("enter") == "Enter"
    assert cbmod.normalize_key("ctrl+a") == "Control+a"
    assert cbmod.normalize_key("Cmd+Shift+K") == "Meta+Shift+K"
    assert cbmod.normalize_key("esc") == "Escape"
    assert cbmod.normalize_key("f5") == "F5"
    assert cbmod.normalize_key("Down") == "ArrowDown"
    assert cbmod.normalize_key("#email") is None
    assert cbmod.normalize_key("Sign") is None


def test_normalize_url(tmp_path):
    assert cbmod.normalize_url("example.com", "/") == "https://example.com"
    assert cbmod.normalize_url("localhost:3000/x", "/") == "http://localhost:3000/x"
    assert cbmod.normalize_url("127.0.0.1:8080", "/") == "http://127.0.0.1:8080"
    assert cbmod.normalize_url("https://a.b/c", "/") == "https://a.b/c"
    assert cbmod.normalize_url("about:blank", "/") == "about:blank"
    f = tmp_path / "p.html"
    f.write_text("<p>x</p>")
    assert cbmod.normalize_url("p.html", str(tmp_path)).startswith("file://")


def test_parse_script_formats():
    lines = cbmod.parse_script('# comment\ncb goto example.com\nfill "Email address" a@b.c\n\nclick 3\n')
    assert lines == [["goto", "example.com"], ["fill", "Email address", "a@b.c"], ["click", "3"]]
    js = cbmod.parse_script('[{"goto": "x.com"}, {"fill_form": {"A": 1}}, {"snap": []}]', "s.json")
    assert js == [["goto", "x.com"], ["fill_form", '{"A": 1}'], ["snap"]]
    yml = cbmod.parse_script("- goto: x.com\n- click: '\"text=Sign In\"'\n- fill: ['#e', 'a b']\n", "s.yaml")
    assert yml == [["goto", "x.com"], ["click", "text=Sign In"], ["fill", "#e", "a b"]]
    assert cbmod.split_line(r'upload 3 C:\path\file.pdf') == ["upload", "3", r"C:\path\file.pdf"]


def test_budget_and_prune():
    lines = [{"d": 0, "k": "s", "t": "nav:"}, {"d": 1, "k": "t", "t": "x" * 50, "v": False},
             {"d": 0, "k": "s", "t": "main:"}, {"d": 1, "k": "i", "t": "[1] button \"Go\"", "v": True},
             {"d": 1, "k": "t", "t": "visible text", "v": True}]
    shown, note = cbmod.budget_lines(lines, 60)
    assert [x["t"] for x in shown] == ["main:", '[1] button "Go"', "visible text"]
    assert "omitted" in note
    assert cbmod.budget_lines(lines, 0)[0] == lines


def test_diff_lines_skips_refs():
    d = cbmod.diff_lines(['[1] textbox "A"', "x"], ['[1] textbox "A" = "v"', "x", "+new"], skip_refs={1})
    assert d == ["+ +new"]


def test_overview_mentions_every_command():
    words = set(re.findall(r"[a-z_]+", cbmod.OVERVIEW))
    legacy = {"wait_url", "wait_text", "wait_js", "version", "help", "new_tab", "close_tab", "blur"}
    missing = [c for c in cbmod.COMMANDS if c not in words and c not in legacy]
    assert not missing, missing


def test_help_and_usage_errors(cb):
    r = cb("help")
    assert "snap" in r.stdout and "click" in r.stdout and "TARGET" in r.stdout
    r = cb("help", "click")
    assert "coordinates" in r.stdout
    r = cb("clikc", "3", ok=False)
    assert r.returncode == 2 and "did you mean click" in r.stderr
    r = cb("click", "3", "--bogus", ok=False)
    assert r.returncode == 2 and "usage: cb click" in r.stderr


# ─── Browser tests ───────────────────────────────────────────────────────────

def test_goto_prints_outline(cb):
    out = cb.goto("index.html").stdout
    assert "Page: CB Test Home" in out
    assert "# Welcome to the test site" in out
    assert 'nav "Primary":' in out
    assert 'form "Sign up":' in out
    assert re.search(r'\[\d+\] textbox "Email address" type=email \(required\)', out)
    assert re.search(r'\[\d+\] select "Country" = "Choose…" options: Choose… \| United States', out)
    assert re.search(r'\[\d+\] checkbox "I agree to the terms" \(unchecked\)', out)
    assert re.search(r'\[\d+\] button "Disabled action" \(disabled\)', out)
    assert re.search(r'\[\d+\] clickable "Fancy card"', out)
    assert "Skip to content" not in out  # visually hidden
    assert "Free | $0" in out
    assert 'img "Company logo"' in out


def test_refs_are_stable(cb):
    a = cb.goto("index.html").stdout
    b = cb("snap").stdout
    assert ref_of(a, 'button "Add item"') == ref_of(b, 'button "Add item"')
    cb("click", "Add item")
    c = cb("snap", "-i").stdout
    assert ref_of(a, 'button "Add item"') == ref_of(c, 'button "Add item"')


def test_form_flow_with_text_targets(cb):
    cb.goto("index.html")
    r = cb("fill", "Email address", "alex@example.com")
    assert 'filled [' in r.stdout and "alex@example.com" in r.stdout
    assert "Email address" in r.stdout
    cb("fill", "Country", "Canada")
    cb("check", "I agree")
    r = cb("click", "Sign up")
    assert "+   Submitted: alex@example.com / ca / agree=true" in r.stdout


def test_click_by_ref_selector_and_coords(cb):
    out = cb.goto("index.html").stdout
    r = cb("click", ref_of(out, 'button "Add item"'))
    assert "+   - Item 2" in r.stdout
    r = cb("click", "#add")
    assert "Item 3" in r.stdout
    r = cb("click", "[" + ref_of(out, 'button "Add item"') + "]")
    assert "Item 4" in r.stdout
    box = cb("eval", "(() => { const r = document.querySelector('#add').getBoundingClientRect(); "
                     "return Math.round(r.x + r.width/2) + ',' + Math.round(r.y + r.height/2) })()").stdout.strip()
    r = cb("click", box)
    assert "clicked at" in r.stdout and "Item 5" in r.stdout


def test_no_visible_change_and_disabled(cb):
    cb.goto("index.html")
    t = time.time()
    r = cb("click", "Disabled action", ok=False)
    assert "disabled" in r.stderr
    assert time.time() - t < 5


def test_dialogs(cb):
    cb.goto("index.html")
    r = cb("click", "Delete everything")
    assert "dialog (confirm) 'Really delete?' -> accepted" in r.stdout
    assert "deleted" in r.stdout
    cb("dialog", "dismiss")
    r = cb("click", "Delete everything")
    assert "-> dismissed" in r.stdout and "kept" in r.stdout
    cb("dialog", "accept", "Ada")
    r = cb("click", "Ask name")
    assert "name=Ada" in r.stdout


def test_async_settle_and_wait(cb):
    cb.goto("index.html")
    r = cb("click", "Fetch items")
    assert "Got alpha, beta, gamma" in r.stdout  # waited for the fetch
    cb("click", "Load slowly")
    r = cb("wait", "Loaded late")
    assert "ok:" in r.stdout
    r = cb("wait", "--gone", "Nope not here", "--timeout", "500")
    assert "ok:" in r.stdout
    r = cb("wait", "--text", "Never appears", "--timeout", "300", ok=False)
    assert "timed out after 300ms waiting for text 'Never appears' to appear" in r.stderr
    assert "waited 200ms" in cb("wait", "200ms").stdout
    assert "URL is" in cb("wait", "--url", "index").stdout
    assert "network idle" in cb("wait_idle", "3000").stdout


def test_new_tab_popup_and_tabs(cb):
    cb.goto("index.html")
    r = cb("click", "Open in new tab")
    assert "new tab opened" in r.stdout and "Page: Page Two" in r.stdout
    tabs = cb("tabs").stdout
    assert re.search(r"\d\* Page Two", tabs)
    r = cb("tab", "CB Test Home")
    assert "Page: CB Test Home" in r.stdout
    r = cb("close_tab", "1")
    assert "closed" in r.stdout
    assert len(cb("tabs").stdout.strip().splitlines()) == 1


def test_navigation_back_forward(cb):
    cb.goto("index.html")
    r = cb("click", "Page two")
    assert "Page: Page Two" in r.stdout
    t = time.time()
    r = cb("back")
    assert "Page: CB Test Home" in r.stdout and time.time() - t < 10
    r = cb("forward")
    assert "Page: Page Two" in r.stdout
    assert "Page: Page Two" in cb("reload").stdout


def test_stale_ref_after_navigation(cb):
    out = cb.goto("index.html").stdout
    ref = ref_of(out, 'button "Add item"')
    cb.goto("page2.html")
    r = cb("click", ref, ok=False)
    assert "not on the page anymore" in r.stderr and "cb snap" in r.stderr


def test_shadow_dom(cb):
    out = cb.goto("shadow.html").stdout
    assert re.search(r'\[\d+\] textbox "Shadow input"', out) and "Slotted label" in out
    cb("fill", "Shadow input", "hi")
    r = cb("click", "Shadow button")
    assert "shadow clicked hi" in r.stdout


def test_iframes_inline_and_targetable(cb):
    out = cb.goto("frames.html").stdout
    assert re.search(r'\[\d+\] iframe "Payment form":\n  \[\d+\] textbox "Card number"', out)
    cb("fill", "Card number", "4242")
    r = cb("click", "Pay now")
    assert "paid 4242" in r.stdout
    card = ref_of(out, 'textbox "Card number"')
    r = cb("fill", card, "1111")
    assert "1111" in r.stdout
    r = cb("find", "pay")
    assert "(in iframe [" in r.stdout


def test_custom_widgets(cb, tmp_path):
    out = cb.goto("custom.html").stdout
    assert re.search(r'\[\d+\] combobox "Fruit" = "Pick a fruit" \(collapsed\)', out)
    assert re.search(r'\[\d+\] label "Subscribe to newsletter" \(for checkbox, unchecked\)', out)
    assert re.search(r'\[\d+\] switch "Dark mode" \(unchecked\)', out)
    assert "Banana" in cb("select", "Fruit", "Banana").stdout
    assert "Banana" in cb("snap").stdout
    cb("check", "Subscribe to newsletter")
    assert "(for checkbox, checked)" in cb("snap").stdout
    cb("check", "Dark mode")
    assert re.search(r'switch "Dark mode" \(checked\)', cb("snap").stdout)
    f = tmp_path / "resume.txt"
    f.write_text("hi")
    r = cb("upload", "Upload resume", str(f))
    assert "file: resume.txt" in r.stdout
    r = cb("fill", "Editor", "rich text")
    assert "rich text" in r.stdout
    r = cb("click", "Open dialog")
    assert 'dialog "Confirm order" (modal):' in r.stdout
    r = cb("click", "Confirm")
    assert "confirmed" in r.stdout


def test_overlay_detection_fails_fast(cb):
    cb.goto("custom.html")
    cb("click", "Show overlay")
    assert "covers most of the page" in cb("snap").stdout
    t = time.time()
    r = cb("click", "Open dialog", ok=False)
    assert "click blocked" in r.stderr and "overlay" in r.stderr
    assert time.time() - t < 4
    r = cb("click", "Open dialog", "--force")
    assert "dialog" in r.stdout


def test_batch_heredoc_inline_and_errors(cb):
    cb.goto("index.html")
    script = 'cb fill "Email address" bob@example.com\nfill Country "United States"\ncheck "I agree"\nclick "Sign up"\n'
    r = cb("batch", input=script)
    assert "[4] click 'Sign up'" in r.stdout
    assert "Submitted: bob@example.com / us / agree=true" in r.stdout
    assert 'textbox "Email address" type=email = "bob@example.com"' not in r.stdout.split("--")[-1]
    r = cb("batch", "click 'Add item'", "click 'Nope nothing'", "click 'Add item'", ok=False)
    assert "stopped at step 2 of 3" in r.stdout and "Item 2" in r.stdout and "Item 3" not in r.stdout
    r = cb("batch", "--continue", "click 'Nope nothing'", "click 'Add item'", ok=False)
    assert "Item 3" in r.stdout


def test_batch_legacy_yaml(cb, tmp_path, site):
    p = tmp_path / "s.yaml"
    p.write_text(f"- goto: {site}/index.html\n- fill_form:\n    Email address: y@y.y\n    I agree to the terms: true\n- snap: []\n")
    r = cb("batch", str(p))
    assert "filled 2/2 fields" in r.stdout and 'checkbox "I agree to the terms" (checked)' in r.stdout


def test_fill_form(cb):
    cb.goto("index.html")
    r = cb("fill_form", json.dumps({"Email address": "z@z.z", "Country": "mx", "I agree to the terms": True,
                                    "Pro": True, "Bio": "hello", "Nope": "x"}), ok=False)
    assert "filled 5/6 fields" in r.stdout and "ERR 'Nope'" in r.stdout
    assert 'select "Country" = "Mexico"' in cb("snap").stdout


def test_read_find_links_html(cb):
    cb.goto("docs.html")
    r = cb("read").stdout
    assert "# Documentation" in r and "## Install" in r and "- Step two\n  - Sub step" in r
    assert "many things about the site" in r
    assert "[many things](" in cb("read", "--links").stdout
    cb.goto("index.html")
    r = cb("find", "sign").stdout
    assert re.search(r'\[\d+\] button "Sign up"', r)
    r = cb("links", "two").stdout
    assert re.search(r'\[\d+\] link "Page two" -> /page2.html', r)
    h = cb("html", "Sign up").stdout
    assert h.strip() == '<button type="submit">Sign up</button>'
    assert "data-cb-ref" not in cb("html").stdout


def test_snap_modes(cb):
    cb.goto("index.html")
    full = cb("snap").stdout
    i = cb("snap", "-i").stdout
    assert "Read the" in full and "Read the" not in i and 'button "Add item"' in i
    forms = cb("snap", "--forms").stdout
    assert "textbox" in forms and 'link "Home"' not in forms and "header:" not in forms
    dialog = cb("snap", "--in", "#signup").stdout
    assert "Email address" in dialog
    long_out = cb.goto("long.html").stdout
    assert len(long_out) < 13000 and "Bottom button" in long_out and "omitted" in long_out
    assert len(cb("snap", "--all").stdout) > 30000


def test_eval_variants(cb):
    cb.goto("index.html")
    assert cb("eval", "document.title").stdout.strip() == "CB Test Home"
    assert cb("eval", "const x = [1,2]; return x.map(v => v * 2)").stdout.strip() == "[2, 4]"
    assert cb("eval", "await new Promise(r => setTimeout(r, 10)); return 'ok'").stdout.strip() == "ok"
    cb("fill", "Email address", "e@e.e")
    assert cb("eval", "el => el.value", "--on", "Email address").stdout.strip() == "e@e.e"
    assert cb("eval", "-", input="1 + 1").stdout.strip() == "2"
    r = cb("eval", "throw new Error('kaboom')", ok=False)
    assert "kaboom" in r.stderr


def test_console_and_network(cb):
    cb.goto("index.html")
    r = cb("eval", "console.error('boom from page'); 1")
    assert "console error: boom from page" in r.stdout
    assert "boom from page" in cb("console", "--errors").stdout
    cb("click", "Fetch items")
    net = cb("network", "api").stdout
    ids = re.findall(r"#(\d+) GET 200 fetch .*/api/items", net)
    assert ids, net
    cb.goto("page2.html")  # bodies must survive navigation
    assert '"alpha"' in cb("network", "--body", ids[-1]).stdout
    r = cb("eval", "fetch('/api/fail').then(r => r.status)")
    assert "request failed: GET" in r.stdout and "500" in r.stdout


def test_press_and_type(cb):
    cb.goto("index.html")
    cb("type", "hello", "--into", "Search anything")
    assert '= "hello"' in cb("snap").stdout
    cb("press", "Control+A", "Backspace")
    assert 'textbox "Search anything"\n' in cb("snap").stdout
    cb("type", "x y", "--into", "Search anything", "--clear")
    r = cb("press", "Search anything", "Enter")
    assert 'textbox "Email address" type=email (required, invalid)' in r.stdout  # validation feedback shows up
    cb("fill", "Email address", "v@v.v")
    assert "Submitted" in cb("press", "Search anything", "Enter").stdout
    r = cb("press", "NotAKey", ok=False)
    assert "no element matches" in r.stderr or "unknown key" in r.stderr


def test_screenshots(cb):
    cb.goto("index.html")
    p = cb("shot").stdout.split()[0]
    assert Path(p).exists() and Path(p).read_bytes()[:4] == b"\x89PNG"
    r = cb("snap", "-i", "--shot").stdout
    p2 = re.search(r"Screenshot: (\S+)", r).group(1)
    assert Path(p2).exists()
    p3 = cb("shot", "Sign up").stdout.split()[0]
    assert Path(p3).exists()
    assert "data-cb-overlay" not in cb("html").stdout  # overlay removed after the shot


def test_scroll_infinite(cb):
    cb.goto("infinite.html")
    r = cb("scroll", "bottom")
    assert "scrolled page bottom" in r.stdout and "+ Post 21" in r.stdout
    r = cb("scroll", "top")
    assert "(at top)" in r.stdout


def test_download(cb):
    cb.goto("page2.html")
    r = cb("click", "Download report")
    assert "downloaded:" in r.stdout
    p = re.search(r"downloaded: (.+)", r.stdout).group(1).strip()
    assert Path(p).read_text() == "a,b\n1,2\n"


def test_act(cb):
    cb.goto("index.html")
    assert "filled" in cb("act", "fill Email address with q@q.q").stdout
    assert "selected" in cb("act", "select Canada in Country").stdout
    assert "Item 2" in cb("act", "click Add item").stdout


def test_status_and_no_daemon(cb, site):
    s = cb("status").stdout
    assert "browser:" in s and "daemon" in s
    r = cb("goto", f"{site}/page2.html", env={"CB_DAEMON": "0"})
    assert "Page: Page Two" in r.stdout
    r = cb("click", "Back home", env={"CB_DAEMON": "0"})
    assert "Page: CB Test Home" in r.stdout


def test_pipe_to_head_is_quiet(cb_env, cb):
    import subprocess
    import sys
    from conftest import RUNNER, ROOT
    cb.goto("long.html")
    r = subprocess.run(f'"{sys.executable}" -c "{RUNNER}" "{ROOT}" snap --all | head -2', shell=True,
                       env=cb_env, capture_output=True, text=True, timeout=60)
    assert len(r.stdout.splitlines()) == 2 and "Error" not in r.stderr and "ERROR" not in r.stderr
