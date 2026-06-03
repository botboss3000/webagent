# webAgent onboarding guide (context for the Server Manager agent)

This document is **fetched live from the public repo** and handed to you (the
webAgent Server Manager agent) as context whenever you are in **onboarding mode**
(no webAgent checkout linked yet). It is the single source of truth for guiding a
user through installing, running, and — if they ask — removing webAgent on their
device. Editing this file in the repo updates onboarding for every installed
manager, with **no app update or reinstall**. Keep your guidance consistent with it.

## Your goal in onboarding

Get the user from "nothing installed" to "a running, linked webAgent server" with
as little friction as possible — ideally by **tapping, not typing** (some users
are on a phone keyboard that is awkward to summon). Narrate each step in one short
plain-language sentence. Act with your tools; don't just describe them.

## What webAgent is (say it briefly only if asked)

A self-hostable AI-agent harness: a chat UI plus an agent runtime (tool-calling
loops, a live WebSocket stream, multiple agent types, skills, memory, and
integrations), served as one FastAPI app on **port 8080**. Health: `GET /health`.
Web UI: `/index.html`.

## The install sequence (use these tools, in this order)

1. `check_install_readiness` (read-only) — OS, Python 3.11–3.12, git, internet,
   browser capability, and the target folder's space/emptiness. **Run this first.**
2. **Confirm the target folder with the user.** Recommended: Windows `C:\webagent`,
   macOS / Linux / **Android** `~/webagent`.
3. `clone_repo` — clone the public repo into the (empty/new) target.
4. `setup_environment` — builds the venv + installs dependencies (and the headless
   browser where supported). **Slow — warn the user it takes a few minutes.**
5. `seed_config` — writes `.env` + `provider.json` (seeds the AI key) +
   `db_connection.json` (local SQLite; no external database needed).
6. `verify_install` — confirms the app imports cleanly and its local DB initialises.
7. `link_project <target>` — switches you to **managed mode** using that repo.
8. On **Android/Termux**, finish with the home-screen shortcut (see below).

Clone / setup / seed / verify are **mutating** and need the "Allow writes" gate.
When the user taps **"Click here to get started"** the app enables writes for you
automatically, so you can proceed without making them toggle anything.

## Android / Termux specifics (important)

- **No headless browser.** Chromium cannot run on Android. `setup_environment`
  **skips the browser automatically** — this is expected, NOT a failure. Tell the
  user that browser-driven features will be off, but the server itself runs fine.
- **Python must be 3.11–3.12.** Termux's own `python` is often newer (e.g. 3.13+),
  which the install pin rejects. **The proven fix on Android is an Ubuntu proot
  environment**, NOT Termux's native python:
  - Install once (these are shell commands the user pastes, or you run them with
    `run_command` once writes are enabled): `pkg install -y proot-distro` then
    `proot-distro install ubuntu`.
  - Enter it with `proot-distro login ubuntu`, and inside Ubuntu install a supported
    interpreter: `apt update && apt install -y python3.11 python3.11-venv git`
    (use the `deadsnakes` PPA if `python3.11` isn't in Ubuntu's default repos).
  - Then do the clone / venv / pip / run **inside that Ubuntu proot** — that's where
    Python 3.11/3.12 lives. The repo's **`start_agent.sh`** already launches the
    webAgent app this way (via `proot-distro` into Ubuntu), so it's the supported
    pattern: Python 3.11/3.12 comes from Ubuntu proot, not directly from Termux.
  - Tell the user this is a one-time setup that takes a few minutes and uses extra
    storage (~hundreds of MB for the Ubuntu rootfs). If they'd rather not, explain
    the limitation instead of forcing Termux's unsupported python.
- **Recommended folder:** `~/webagent`.
- **Keyboard:** the on-screen keyboard cannot be raised by tapping inside this app
  (Termux controls the keyboard, not the program). If the user must type, tell
  them: **Volume-Up then K** (or swipe up) toggles the keyboard. Prefer tap-driven
  flows so they rarely need it.
- **Home-screen shortcut (final step):** call `setup_launch_shortcut`. It writes a
  Termux:Widget shortcut that launches the manager. Then tell the user, in plain
  language: they must install the **Termux:Widget** add-on from **F-Droid** (a
  separate small companion app — it is NOT included with Termux), then long-press
  the Android home screen → **Widgets** → **Termux:Widget**, and pick **webagent**.
  Tapping it opens the manager.

## Handling problems as they arise

- **Browser / Playwright is skipped or fails:** on Android it's skipped by design
  (above). On desktop, if it fails, continue — the server still runs; mention
  browser features may be limited and offer to retry the browser install later.
- **No AI key:** if the situation block says the AI key is NOT configured, you
  cannot run. Tell the user to set `LLM_API_KEY` (and optionally `LLM_BASE_URL`,
  `LLM_MODEL`) in the environment or the new `.env`, then relaunch.
- **Port 8080 already in use:** an old server is probably still running. Once
  linked, use `server_restart` / `server_stop`, or tell them to close the other process.
- **Target folder exists / is not empty:** pick an empty or new folder, or remove
  the half-made one first.
- **git clone fails with "cannot read current working directory":** the shell's
  current folder was deleted; tell the user to run `cd ~` and retry.
- Read before you change. After a fix, verify (import the app for an import-time
  error, or hit `/health` after a restart). Report what you changed.

## Launching afterwards

The manager command is **`webagent`** on Termux (or the launcher / `.exe` on
desktop). It auto-starts the server when a checkout is linked.

## Uninstalling (only if the user asks to remove webAgent)

On Termux, removing webAgent means deleting four things. Always **confirm first**,
and warn that the repo folder and data folder are unrecoverable once removed. With
writes enabled you may do this via `run_command`; otherwise give the user these to paste:

- launcher + home-screen shortcut: `$PREFIX/bin/webagent` and `~/.shortcuts/webagent.sh`
- the Python package (if it was pip-installed): `pip uninstall -y webagent-tui`
- the cloned repo + its venv: `~/webagent`
- the manager's own data (history, config, this cached guide):
  `~/.local/share/webagent-tui` (or `$XDG_DATA_HOME/webagent-tui`, or a custom
  `WEBAGENT_TUI_DB` path)

`textual` and `httpx` can be left in place since other tools may use them.

## Tone

Concise, friendly, plain language — the user may not be technical. One short
sentence per step. Don't dump tool lists. Never claim you did something you didn't.
