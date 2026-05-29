# webAgent Android launcher

A Textual-based TUI for running webAgent on an Android phone via Termux +
proot-distro Ubuntu. It handles the install dance, surfaces a dependency
**Doctor** so you can see exactly what's broken, and gives you one-tap
**Launch / Restart / Kill / Browser** buttons for the server.

It is the Android counterpart to the Windows TUI in [`../launcher/`](../launcher/).

## What it does

- **Launch / Restart / Kill** — runs `python run.py` from the project venv
  inside the proot, tracks PID, kills the process group on stop, and
  cleans up any stale listener on port 8080.
- **Browser** — pops Android's default browser open to
  `http://localhost:8080/index.html` (uses Termux:API's `termux-open-url`
  via a tiny "bridge file" the shim watches outside the proot).
- **Doctor** — checks Python venv, `.env`, apt build deps, every
  webAgent requirement, and Playwright. Required deps show as **red** if
  missing; optional deps (Playwright Chromium, Supabase, Twilio, etc.)
  show as **yellow** since webAgent runs without them.
- **Fix** — per-row Fix buttons for things like "create venv",
  "install build-essential", "pip install httpx", "skip Playwright
  browser download". A **Fix all** button runs the whole sequence in
  order.

## First-time setup on the phone

You need three things installed on Android:

1. **[Termux](https://f-droid.org/en/packages/com.termux/)** (from F-Droid — the Play Store version is outdated)
2. **[Termux:API](https://f-droid.org/en/packages/com.termux.api/)** addon (optional — needed for the Browser button)
3. The webAgent repo cloned into Termux storage (e.g. `~/webagent`)

Then in Termux:

```bash
# inside Termux
pkg update -y
pkg install -y git termux-api
git clone https://github.com/<your-fork>/webagent.git ~/webagent
bash ~/webagent/launcher_android/start.sh
```

That's it. The shim will:

- Install `proot-distro` if missing.
- Install the Ubuntu distro if missing (one-time, a few minutes).
- Bind-mount your `~/webagent` into the proot at `/root/webagent`.
- Create a tiny `launcher_android/.venv` inside the proot with just
  `textual`.
- Drop you into the TUI.

## Resolving dependency issues

When you launch the TUI the first time it'll run **Doctor**
automatically. You'll typically see:

| Severity | What |
|----------|------|
| red `X`  | Python venv missing — press **Fix** on that row, or **Fix all** |
| red `X`  | `.env` missing — Fix copies `.env.example` to `.env` |
| yellow `!` | apt build deps incomplete — Fix runs `apt-get install build-essential …` |
| yellow `!` | Playwright Chromium not downloaded — Fix sets `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` so install no-ops on ARM |
| yellow `!` | Some optional pip pkg failed (e.g. `supabase` cryptography deps) — listed but **non-fatal**, webAgent still launches |

Once Doctor is green (or only yellow), press **L** to launch. The Logs
tab streams `run.py`'s stdout/stderr live.

## Keyboard

| Key | Action |
|-----|--------|
| `L` | Launch the server |
| `R` | Restart (graceful TERM → KILL fallback) |
| `K` | Kill the server |
| `B` | Open browser (forwards to `termux-open-url`) |
| `D` | Re-run Doctor |
| `F` | Fix all |
| `Q` / `Ctrl+C` | Quit launcher (server keeps running unless you Kill first) |

## Layout

```
launcher_android/
├── start.sh                # Termux entry point — handles proot-distro
├── pyproject.toml          # `textual` only
├── requirements.txt        # ditto, for pip fans
├── launcher_android/
│   ├── __main__.py         # `python -m launcher_android`
│   ├── app.py              # Textual App: tabs, buttons, log pane
│   ├── server.py           # subprocess controller for run.py
│   ├── doctor.py           # dep checks → DoctorReport
│   ├── fixes.py            # async fix actions
│   └── styles.tcss         # mobile-first CSS
└── README.md
```

## Env vars

| Var | Default | What |
|-----|---------|------|
| `DISTRO` | `ubuntu` | proot-distro distro to use |
| `WEBAGENT_DIR` | `/root/webagent` | path inside the proot |
| `WEBAGENT_PROJECT_DIR` | (auto-detected) | override the project root inside the proot |
| `WEBAGENT_BROWSER_BRIDGE` | `/tmp/webagent_open.url` | bridge file for Browser button |
| `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD` | (set by Fix) | tells playwright not to pull Chromium |

## Notes

- The launcher's own venv (`launcher_android/.venv`) is **separate** from
  the webAgent project venv (`venv/` at the repo root). That way `Reset
  Python` style fixes on the project venv never break the TUI.
- `.venv/` inside `launcher_android/` is gitignored — it's per-machine.
- The browser bridge is necessary because `termux-open-url` lives on the
  Termux side, not inside the proot. The shim watches a file in the
  proot's `/tmp` (exposed at
  `$PREFIX/var/lib/proot-distro/installed-rootfs/<distro>/tmp/`) and
  forwards any URL the TUI writes there.
