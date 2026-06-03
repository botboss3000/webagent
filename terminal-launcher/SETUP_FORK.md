# Setting up the fork build (plain-English guide)

This build makes Terminal Launcher **self-contained**: it carries its own copy of
the Termux Linux environment, downloads it on first run, and from then on opens
straight into a real terminal with `bash`, `python`, `node`, `pkg`, and anything
else you install. You do **not** need a separate Termux app afterwards.

There's one important consequence to understand before you start.

---

## The one thing to know first

To make the prebuilt Termux programs work without recompiling them, this app has
to install under the **same identity as Termux** (`com.termux`). Android only
allows one app per identity, so:

> **This app cannot be installed next to the regular Termux app. It takes
> Termux's place.** If you already have Termux installed, you'll uninstall it
> first — and uninstalling Termux erases its files.

If you have stuff in your current Termux you care about, back it up first (in
Termux: `termux-setup-storage` then copy your files to shared storage, or run
`tar -czf /sdcard/termux-backup.tar.gz -C /data/data/com.termux/files/home .`).

If you've never really used Termux, there's nothing to lose — just uninstall it.

---

## Step by step

### 1. (If needed) back up and uninstall the existing Termux

- Back up anything important (see above).
- Long-press the Termux icon → **Uninstall** (or Settings → Apps → Termux →
  Uninstall).

### 2. Build the APK

1. Install **Android Studio** on your PC if you don't have it.
2. **Open** the `terminal-launcher` folder in Android Studio and let Gradle sync
   finish (downloads dependencies + the terminal engine; first time is slow).
3. Menu → **Build → Build Bundle(s) / APK(s) → Build APK(s)**.
4. Click **locate** in the popup to find `app-debug.apk`.

### 3. Put it on the phone and install

- Easiest: connect the phone by USB (enable "USB debugging" in Developer
  Options) and press the green **Run** button — it installs and launches.
- Or copy `app-debug.apk` to the phone, tap it, and allow installation.

### 4. First launch — install the environment

- The app opens on a **"Set up the Linux environment"** screen.
- Tap **Set up now**. It downloads ~30–50 MB (use Wi-Fi) and unpacks it. This
  happens **once**.
- When it finishes, you drop straight into the terminal. Try `ls`, `python3`,
  `pkg install <something>`.

### 5. Make it do your thing

Open **Settings** (gear, bottom-left) and set:

- **Default project folder** — where new sessions start.
- **Startup command** — e.g. `claude`. Then every new session can `cd` into your
  project and launch it automatically.

That's it. From now on the app opens directly into the terminal.

---

## Keeping the environment up to date

The Linux environment is downloaded from Termux's official bootstrap releases.
The release tag is set in one place:
[`BootstrapInstaller.kt`](app/src/main/java/com/webagent/terminallauncher/terminal/BootstrapInstaller.kt)
→ `BOOTSTRAP_VERSION`.

If the download ever fails because the tag is outdated, open
<https://github.com/termux/termux-packages/releases>, find the newest release
named `bootstrap-...` (it will have `bootstrap-aarch64.zip` etc. attached), copy
its tag into `BOOTSTRAP_VERSION`, and rebuild.

Day-to-day package updates happen *inside* the terminal as usual: `pkg upgrade`.

---

## Troubleshooting

| Problem | Cause / fix |
|--------|-------------|
| "App not installed" / signature conflict | The regular Termux (or an older build) is still installed. Uninstall it, then install this. |
| Setup download fails | No/again network, or the bootstrap tag is stale — update `BOOTSTRAP_VERSION` (see above). |
| Terminal opens but every command says "permission denied" / "no such file" | The app was built with the wrong target SDK. It **must** be `targetSdk = 28` (already set in `build.gradle.kts`); a higher value makes Android block running the Linux binaries. |
| Want to wipe and reinstall the environment | Clear the app's storage (Settings → Apps → Terminal Launcher → Storage → Clear storage), reopen, and run setup again. |

---

## Why it has to be this way (the honest version)

The prebuilt Termux programs have the path `/data/data/com.termux/files/usr`
hard-baked into them. An app's private folder is decided by its identity, so the
only way those programs find their own files unchanged is for this app to **be**
`com.termux`. The alternative — recompiling the entire Linux environment with a
different path — is exactly what we're avoiding by reusing Termux's official
build. And Android forbids running programs from an app's writable folder unless
the app targets the older SDK 28, which is why that setting is locked in. These
aren't choices we made for fun; they're the rules that make a real terminal
possible on an unrooted phone.
