package com.webagent.terminallauncher.terminal

import android.content.Context
import java.io.File

/**
 * THE TERMUX INTEGRATION POINT (fork build).
 *
 * This app is built as a Termux fork: its applicationId is `com.termux`, so its
 * private data dir IS `/data/data/com.termux/files`. That means the canonical
 * Termux paths below are simply our own files dir, and the prebuilt Termux
 * bootstrap (installed by [BootstrapInstaller]) runs unmodified because its
 * baked-in `/data/data/com.termux/...` paths now point at us.
 *
 * Consequence (by design): this build is self-contained and does NOT read a
 * separate stock Termux install. Because it shares the `com.termux` package id,
 * it cannot be installed alongside the stock Termux app -- it replaces it. See
 * SETUP_FORK.md.
 *
 * Everything about where the environment lives and how we launch into it stays
 * isolated here, so swapping the backend later only touches this file.
 */
object TermuxEnvironment {

    private const val PREFIX = "/data/data/com.termux/files/usr"
    private const val HOME = "/data/data/com.termux/files/home"

    /** Default starting directory for new sessions before the user changes it. */
    const val DEFAULT_HOME = HOME

    private val LOGIN_SHELLS = listOf(
        "$PREFIX/bin/bash",
        "$PREFIX/bin/sh",
        "$PREFIX/bin/zsh",
        "$PREFIX/bin/login"
    )

    enum class Status {
        /** The bootstrap is installed and a shell is executable -> ready to run. */
        READY,
        /** First run (or wiped): the Linux environment still needs installing. */
        NEEDS_BOOTSTRAP
    }

    fun status(context: Context): Status =
        if (BootstrapInstaller.isInstalled(context)) Status.READY else Status.NEEDS_BOOTSTRAP

    /** The shell binary to exec (prefer bash, fall back to sh). */
    fun shellPath(): String =
        LOGIN_SHELLS.firstOrNull { File(it).canExecute() } ?: "$PREFIX/bin/sh"

    /** `-` / `-l` requests a login shell so Termux profile scripts run. */
    fun shellArgs(): Array<String> = arrayOf("-l")

    /**
     * Environment block for the child process, mirroring Termux's own bootstrap
     * so tools resolve their data and shared libraries exactly as in Termux.
     */
    fun buildEnv(): Array<String> = arrayOf(
        "TERM=xterm-256color",
        "COLORTERM=truecolor",
        "LANG=en_US.UTF-8",
        "HOME=$HOME",
        "PREFIX=$PREFIX",
        "PATH=$PREFIX/bin",
        "LD_LIBRARY_PATH=$PREFIX/lib",
        "TMPDIR=$PREFIX/tmp",
        "SHELL=${shellPath()}",
        "TERMUX_VERSION=fork"
    )

    /** The directory a new session should start in, clamped to something valid. */
    fun resolveWorkingDir(configuredDir: String, fallbackTmp: String): String {
        val candidate = configuredDir.ifBlank { HOME }
        return when {
            File(candidate).isDirectory -> candidate
            File(HOME).isDirectory -> HOME
            else -> fallbackTmp
        }
    }
}
