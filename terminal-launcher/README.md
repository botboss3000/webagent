# Terminal Launcher

A custom, modern Android terminal **front-end** that runs on top of the Linux
environment you already have installed in **Termux**. It replaces Termux's UI and
input experience with a clean, dark, footer-driven interface designed for
non-technical users — while keeping a **real, PTY-backed terminal** underneath
(genuine shells, ANSI colour, full-screen TUI apps, the works).

It is **not** a fake terminal, **not** a chat-style terminal, and it has **no
separate input bar**. You type into the terminal itself; the footer only adds
shortcuts, voice, keyboard toggle, settings and sessions.

> **Build mode: self-contained Termux fork.** This app installs under the
> `com.termux` identity and carries Termux's Linux environment, which it
> downloads and unpacks on first run. It therefore **replaces** a stock Termux
> install rather than sitting beside it. New here? Start with
> **[SETUP_FORK.md](SETUP_FORK.md)** — a plain-English, step-by-step guide.
>
> Why a fork and not a standalone front-end? Because Android sandboxes every app:
> a separately-installed APK genuinely cannot reach a stock Termux's files on an
> unrooted phone. Building as the `com.termux` identity is what makes the prebuilt
> Termux binaries resolve their own paths and run. See **"How the fork works"**.

---

## At a glance

| Area | What you get |
|------|--------------|
| Terminal | Real PTY via Termux's `terminal-emulator` native library; ANSI colour, cursor movement, resize, scrollback, selection/copy, TUI apps |
| Layout | Terminal fills the screen, **footer-only** controls, **no top app bar**, no side nav |
| Footer | Settings · Sessions · scrollable shortcut row · Voice · Keyboard |
| Shortcuts | Multiple stacked rows; add/remove/reorder buttons **and** rows; 5 action types; 7 built-in special keys |
| Voice | Speech inserted at the cursor like typed input — **never auto-submitted, never adds a newline** |
| Keyboard | One button shows/hides the soft keyboard; hardware keyboards fully supported |
| Sessions | Multiple concurrent sessions (browser-tab style) in a slide-over panel; metadata persisted across restarts |
| Panels | Settings and Sessions **slide over** the terminal from the left — they never resize the terminal |
| Theme | Dark by default; basic customisation (accent colour, font size, light mode) |
| Devices | Phones and tablets, portrait and landscape, soft-keyboard resize handled |

---

## How the fork works (read this)

This is the part most "custom Termux UI" tutorials gloss over, so here it is plainly.

**Android isolates every app.** Each installed app gets its own Linux user-id and
a private data directory (`/data/data/<package>/files`) that other apps cannot
read from or execute. So a *separately-installed* app **cannot exec a stock
Termux's binaries** — and the "shared user-id" trick is a dead end too, because it
needs both apps signed with the same key (you don't have Termux's), and modern
Termux no longer declares a shared user-id anyway.

So this project takes the path that genuinely works on an unrooted phone: it is
built **as a Termux fork**.

1. **It installs as `com.termux`.** Its `applicationId` is `com.termux`, so its
   private dir is exactly `/data/data/com.termux/files` — the path the prebuilt
   Termux binaries have baked into their shebangs/rpaths. They run unmodified.
   (The code/namespace stays `com.webagent.terminallauncher`; only the *install
   identity* is `com.termux`.) Because of the shared identity, it **replaces** a
   stock Termux rather than coexisting with it.
2. **It carries Termux's environment.** On first run,
   [`BootstrapInstaller`](app/src/main/java/com/webagent/terminallauncher/terminal/BootstrapInstaller.kt)
   downloads the official prebuilt Termux *bootstrap* for the device's CPU and
   unpacks it (handling `SYMLINKS.txt`, setting executable bits) into
   `…/files/usr`. After that it launches `bash -l` with Termux's environment
   (`HOME`, `PREFIX`, `PATH`, `LD_LIBRARY_PATH`, `TMPDIR`, `LANG`, `TERM`).
3. **It targets SDK 28.** Android's SELinux policy forbids `exec()` from an app's
   writable home dir on `targetSdk` 29+. Termux itself targets 28 for exactly
   this reason (and is distributed via F-Droid, not the Play Store). So is this.

**The two states** the app resolves on launch (logic isolated in
[`TermuxEnvironment.kt`](app/src/main/java/com/webagent/terminallauncher/terminal/TermuxEnvironment.kt)):

- `READY` → the bootstrap is installed; open straight into the terminal.
- `NEEDS_BOOTSTRAP` → first run (or storage cleared); show the one-time setup
  screen that downloads and installs the environment with a progress bar.

`TermuxEnvironment` and `BootstrapInstaller` are the only files that care about
the backend — the rest of the app is unaware of how the shell is provided.

### Setup

See **[SETUP_FORK.md](SETUP_FORK.md)** for the full beginner walk-through
(back up & uninstall any stock Termux, build the APK, run the one-time setup).

---

## Build & run

**Recommended:** open the `terminal-launcher/` folder in **Android Studio**
(Hedgehog or newer). It will sync Gradle and generate the wrapper automatically.

From the command line you need the Gradle wrapper jar (Android Studio creates it;
or run `gradle wrapper --gradle-version 8.7` once), then:

```
./gradlew assembleDebug        # produces app/build/outputs/apk/debug/app-debug.apk
```

Install the APK (after uninstalling any stock Termux — see SETUP_FORK.md):

```
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

On first launch the app downloads and installs the Linux environment (one time),
then opens into the terminal.

### Dependencies of note

The real terminal comes from Termux's own libraries, resolved via **JitPack**
(see `settings.gradle.kts`):

```
com.github.termux.termux-app:terminal-view:v0.118.0
com.github.termux.termux-app:terminal-emulator:v0.118.0
```

If that tag ever fails to resolve, open <https://jitpack.io/#termux/termux-app>,
pick the latest green build, and set **both** coordinates to that tag. These
libraries define the `TerminalSessionClient` / `TerminalViewClient` interfaces we
implement; if you bump the version and the compiler reports a missing/extra
override, align the overrides in
[`TerminalSessionClientImpl`](app/src/main/java/com/webagent/terminallauncher/terminal/TerminalSessionClientImpl.kt)
and
[`TerminalViewClientImpl`](app/src/main/java/com/webagent/terminallauncher/terminal/TerminalViewClientImpl.kt)
with that version's interface — those two files are the only ones affected.

- **minSdk 24**, **targetSdk 28** (required to exec the Linux binaries; do not
  raise it), **applicationId `com.termux`** (required so the bootstrap paths
  resolve), Kotlin, ViewBinding, kotlinx.serialization.

---

## Project structure

```
terminal-launcher/
├── settings.gradle.kts          # modules + JitPack repo
├── build.gradle.kts             # plugin versions
├── gradle.properties
└── app/
    ├── build.gradle.kts         # deps incl. Termux terminal libs
    ├── proguard-rules.pro
    └── src/main/
        ├── AndroidManifest.xml  # RECORD_AUDIO, Termux <queries>, single Activity
        ├── java/com/webagent/terminallauncher/
        │   ├── TerminalLauncherApp.kt      # applies day/night before UI inflates
        │   ├── MainActivity.kt             # the single Activity / orchestrator
        │   ├── model/
        │   │   ├── SpecialKey.kt           # special keys as byte sequences (extensible)
        │   │   ├── Shortcut.kt             # Shortcut + ShortcutType + ShortcutRow
        │   │   ├── AppConfig.kt            # config + ThemeConfig + AccentColor + defaults
        │   │   └── SessionMeta.kt          # persisted per-session metadata
        │   ├── store/
        │   │   ├── ConfigStore.kt          # atomic JSON config persistence
        │   │   └── SessionStore.kt         # session metadata persistence
        │   ├── terminal/
        │   │   ├── TermuxEnvironment.kt    # THE integration point (paths/env/status)
        │   │   ├── BootstrapInstaller.kt   # first-run download+unpack of the Linux env
        │   │   ├── TerminalManager.kt      # sessions, active selection, input routing
        │   │   ├── TerminalSessionClientImpl.kt   # output → view, clipboard, metadata
        │   │   └── TerminalViewClientImpl.kt      # gestures, zoom, hardware keys
        │   └── ui/
        │       ├── FooterController.kt      # renders shortcut rows
        │       ├── ConfigPanelController.kt # settings + full shortcut/row editor
        │       ├── SessionPanelController.kt# session list + actions
        │       ├── SlidePanelController.kt  # slide-over animation (over, not resize)
        │       ├── VoiceInputController.kt  # speech → cursor insert
        │       └── KeyboardController.kt    # soft-keyboard show/hide toggle
        └── res/
            ├── layout/          # activity_main, panel_config, panel_session, items, dialog, setup
            ├── drawable/        # icons + backgrounds (no hard-coded theme hexes in feature code)
            ├── values/          # LIGHT colours, strings, dimens, styles, theme (DayNight)
            └── values-night/    # DARK colours (the default look)
```

---

## How each feature behaves

### Footer & shortcuts

- The **first shortcut row** renders inline in the footer (scrolls horizontally).
  **Additional rows stack above** the footer, row 1 nearest the bar.
- In **Settings → Footer shortcuts** you can add/remove/reorder rows and, within
  each row, add/remove/edit shortcut buttons (reorder by editing; rows reorder via
  up/down arrows). Reordering uses arrow buttons rather than drag-and-drop for
  reliability and accessibility.
- Each shortcut has one of **five action types**:
  1. **Insert text** — inserts at the cursor; does **not** run (no newline).
  2. **Run command** — types the command and presses Enter.
  3. **Run multi-step script** — sends each configured line, each followed by Enter.
  4. **Run in project dir** — `cd` into the configured default folder, then runs the command.
  5. **Special key** — sends a raw control sequence.
- **Built-in special keys:** Left, Right, Up, Down, Ctrl+C, Tab, Escape. The model
  ([`SpecialKey.kt`](app/src/main/java/com/webagent/terminallauncher/model/SpecialKey.kt))
  is data-driven — Home, End, PageUp/Down, Enter, Backspace, Ctrl+D/Z/L are
  already defined and more can be added in one line.
- **Every shortcut targets the active session only.** Inactive sessions are never
  touched. Buttons run immediately on tap, except *Insert text* which only inserts.

### Voice input

1. Tap the mic. Android speech recognition starts (tap again to cancel).
2. Speak. The recognised text is inserted into the **active** session at the
   current cursor/focus, exactly like typed or pasted input.
3. The app **never auto-submits** and **never appends a newline**. You edit and
   submit yourself.

Because the text is written straight into the **PTY input stream**, it lands
wherever the foreground program's cursor is — a shell prompt *or* a focused text
field inside a TUI app. There is no separate text box and no chat compose bar.

**Partial results:** Android's partial hypotheses are frequently revised mid-
utterance, so inserting them live would duplicate/garble terminal input. This app
therefore inserts only the **final** recognition result. (Partial text is
available internally for a "listening…" hint but is not written to the terminal.)

All failure modes are handled with a friendly message and never crash: recogniser
unavailable, permission denied, cancelled, nothing heard, and offline-model
missing (which suggests installing an offline language pack).

### Keyboard

One button. Tap to **show** the Android soft keyboard, tap again to **hide** it.
No custom input bar, no coupling with voice. Soft-keyboard input and **hardware
keyboards** (arrows, Tab, Esc, Enter, Backspace, Ctrl-combinations) flow through
the TerminalView's own input connection straight to the active PTY. The window
uses `adjustResize`, so showing the keyboard resizes the terminal cleanly.

### Sessions

Open the Sessions panel (slides over from the left). Each entry shows the session
name, current folder, last command, a running/idle indicator, and last-used time.
Actions: **new**, **switch**, **rename**, **close**, **duplicate**. New sessions
start in the configured default directory and optionally run the configured
startup command (e.g. `cd` into your project and run `claude`).

**Background sessions keep running** while you switch between them or background
the app — their PTY children stay alive.

#### Session persistence (documented limitation)

Session **metadata** (names, order, last folder, last command) is persisted and
restored after the app is closed and reopened. **Live processes are not.** The PTY
child processes are owned by this app's process; if Android kills the process to
reclaim memory, those children die with it. On relaunch the app restores the
metadata and opens a **fresh** shell per restored session — it cannot resurrect a
program that was mid-run. This is a hard Android constraint, not a bug.

### Theme

Dark by default. Settings exposes **basic** customisation only: accent colour
(tints the footer controls), terminal font size (also pinch-to-zoom), and a light-
mode toggle. Light/dark are real resource qualifiers (`values/` vs `values-night/`)
applied app-wide via `AppCompatDelegate`; toggling light mode recreates the
Activity to apply instantly. No feature code hard-codes theme colours.

---

## First launch

- **Very first launch only:** the one-time environment setup screen downloads and
  installs the Linux environment (progress shown), then continues automatically.
- After that it opens **directly into the terminal** — no onboarding, no top bar.
- One default session is created automatically.
- The footer ships with one row of the seven built-in special keys plus a small
  "Handy" row (`ls`, `clear`, and an `sudo ` insert) so it isn't empty.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| "App not installed" / signature conflict on install | A stock Termux (same `com.termux` identity) is still installed. Uninstall it first — see [SETUP_FORK.md](SETUP_FORK.md). |
| Setup screen download fails | No network, or the bootstrap tag is stale — update `BOOTSTRAP_VERSION` in `BootstrapInstaller.kt` to the latest at github.com/termux/termux-packages/releases. |
| Every command says "permission denied" / "not found" | The build's `targetSdk` was raised above 28 — Android then blocks running the Linux binaries. Keep `targetSdk = 28`. |
| Reinstall the environment from scratch | Settings → Apps → Terminal Launcher → Storage → **Clear storage**, reopen, run setup again. |
| Voice says network needed | The device speech service is online-only here; install an offline language pack in system settings, or connect to a network. |
| Gradle can't find the Termux libs | Update both `terminal-view` / `terminal-emulator` tags to the latest green build at jitpack.io/#termux/termux-app. |
| Compile error about a missing/extra `override` in a `*ClientImpl` | The pinned Termux library version's interface differs; align the overrides in those two files with that version. |
| Colours wrong in light mode | Don't add hard-coded hex to feature code — add a colour to both `values/colors.xml` and `values-night/colors.xml`. |

---

## License / attribution

This project depends on the **Termux** `terminal-emulator` and `terminal-view`
libraries (Apache-2.0). Termux is a separate project; this app reuses its
open-source terminal widgets and, at runtime, its installed environment.
