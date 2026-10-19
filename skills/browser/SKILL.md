---
name: browser
description: Drive a real Chrome/Edge/Brave/Chromium browser from the shell with `cb` — open pages, read them as a compact text outline, click, fill forms, upload files, handle dialogs/tabs/iframes, take screenshots, inspect console and network. Use for any task that needs a web page: testing a web app, filling a form, logging in, scraping or reading a site, reproducing a UI bug, or checking what a page looks like.
---

# Browsing with cb

`cb` is a CLI on PATH (or `./cb` in the cb folder; `cb.bat` on Windows). The browser and a
background daemon start automatically on first use. Every command is one fast shell call.

## The loop

1. `cb goto <url>` — navigates and prints the page outline.
2. Pick an element by its `[ref]` and act: `cb click 12`, `cb fill 7 "text"`.
3. Read what the action printed. It already waited for the page to settle and shows only what
   changed (`+`/`-` lines), or the whole new page if it navigated. You usually do **not** need
   `cb snap` after an action.

```
$ cb goto example.com/login
Page: Log in
URL: https://example.com/login
form "Log in":
  [4] textbox "Email" type=email (required)
  [5] textbox "Password" type=password
  [6] button "Sign in"
$ cb fill 4 alex@example.com
$ cb fill 5 hunter2 --submit
-> https://example.com/dashboard
Page: Dashboard
...
```

## Targets

Anywhere a command takes a target: a ref (`12`, `[12]`), visible text or a field label
(`"Sign in"`, `"Email address"` — quote multi-word text), a CSS/`xpath=`/`role=` selector, or
`x,y` viewport pixels (same pixels as `cb shot`). Prefer refs; text is fine when unambiguous.

## Reading the outline

- `[7] textbox "Email" = "a@b.c" (required, invalid)` — role, name, current value, states.
- `[9] select "Country" = "Canada" options: …` — use `cb select 9 Mexico` or `cb fill 9 Mexico`.
- `[11] checkbox "Remember me" (unchecked)` — `cb check 11`.
- `[14] clickable "Settings"` — a div/span that acts like a button.
- `dialog "…" (modal):` — deal with the dialog first.
- `Note: … covers most of the page` — an overlay will block clicks; close it (often `cb press Escape`).
- `(… text omitted …)` on big pages — use `cb read` for text, `cb find <text>` to locate elements,
  `cb snap --in <ref>` for one region.
- Refs stay valid until navigation; an outdated ref errors instead of clicking the wrong thing.

## Commands you'll use most

```
cb goto <url>                 cb back / forward / reload
cb snap [-i] [--in T]         outline (-i: interactive only)
cb read                       page text as markdown (articles, search results, docs)
cb find "text"                matching elements with refs
cb click T                    cb fill T <value> [--submit]      cb select T <option>
cb check T / uncheck T        cb press Enter | Tab | Escape | Control+A
cb type "text" --into T       real keystrokes (autocomplete, editors)
cb upload T ./file.pdf        cb hover T      cb scroll down|bottom|T
cb wait "Done" | --gone T | --url /path | --idle | 2s
cb shot [-a]                  screenshot path (-a labels refs); view it with your image reader
cb tabs / tab <n>             cb console --errors      cb network [--body ID]
cb eval "js"                  cb help <command>
```

Several steps in one call (one combined change report at the end):

```bash
cb batch <<'EOF'
fill "Email" alex@example.com
fill "Password" hunter2
click "Sign in"
EOF
```

## Tips

- `(no visible change)` after a click means it probably didn't work — check for an overlay, a
  `(disabled)` state, `cb console --errors`, or try another element.
- Dialogs (alert/confirm/prompt) are auto-accepted and reported. To decline one, run
  `cb dialog dismiss` before the action that opens it.
- A link with `target=_blank` opens a new tab and cb switches to it (reported in the output).
- Need to see layout or a canvas? `cb shot -a`, then read the PNG. Click pixels with `cb click X,Y`.
- Logins persist in cb's own browser profile. If a site shows a CAPTCHA or 2FA, ask the user to
  complete it in the cb browser window (`cb launch --headed`), then continue.
- Parallel agents: give each one `CB_SESSION=<name>` so they get separate browsers.
- Errors are one or two lines and say what to do next; read them before retrying.
