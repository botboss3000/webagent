package com.webagent.terminallauncher.terminal

import android.content.Context
import com.termux.terminal.TerminalSession
import com.termux.terminal.TerminalSessionClient
import com.webagent.terminallauncher.model.AppConfig
import com.webagent.terminallauncher.model.SessionMeta
import com.webagent.terminallauncher.model.SpecialKey
import com.webagent.terminallauncher.store.SessionStore
import java.io.File

/**
 * Owns every terminal session: creation, the active selection, input routing,
 * lifecycle, and metadata persistence.
 *
 * Design notes:
 *  - A single [com.termux.view.TerminalView] is shared by the UI; we attach the
 *    active session to it and detach on switch (exactly how Termux tabs work).
 *  - Background sessions keep running: their [TerminalSession] (and its PTY
 *    child) stay alive while merely not attached to the view.
 *  - All input helpers target the ACTIVE session only, satisfying the brief's
 *    "shortcut actions must not affect inactive sessions" rule.
 */
class TerminalManager(
    private val appContext: Context,
    private val sessionStore: SessionStore,
    private val sessionClient: TerminalSessionClient
) {
    /** Carriage return, built from a char code to avoid embedding a control byte. */
    private val CR = Char(13).toString()

    data class Handle(
        val meta: SessionMeta,
        val session: TerminalSession
    )

    private val handles = mutableListOf<Handle>()
    private var activeId: String? = null

    /** Notified whenever the session list or active selection changes. */
    var onSessionsChanged: (() -> Unit)? = null

    val sessions: List<Handle> get() = handles
    val activeHandle: Handle? get() = handles.firstOrNull { it.meta.id == activeId }
    val activeSession: TerminalSession? get() = activeHandle?.session
    val isEmpty: Boolean get() = handles.isEmpty()

    // ---------------------------------------------------------------------
    // Creation / restoration
    // ---------------------------------------------------------------------

    private fun nowMs(): Long = System.currentTimeMillis()

    private fun fallbackTmp(): String = appContext.cacheDir.absolutePath

    /**
     * Create a brand-new session using [config]'s default directory and startup
     * command. Becomes the active session.
     */
    fun createSession(config: AppConfig, name: String? = null): Handle {
        val workingDir = TermuxEnvironment.resolveWorkingDir(
            config.defaultDirectory, fallbackTmp()
        )
        val meta = SessionMeta(
            name = name ?: defaultName(),
            workingDir = workingDir,
            startupCommand = config.startupCommand,
            lastUsedEpochMs = nowMs()
        )
        val session = spawn(meta)
        val handle = Handle(meta, session)
        handles.add(handle)
        setActive(meta.id)

        if (config.startupCommand.isNotBlank()) {
            // Submit the startup command once the shell is ready.
            runCommand(config.startupCommand)
            meta.lastCommand = config.startupCommand
        }
        persist()
        return handle
    }

    /**
     * Re-open sessions saved from a previous run. Only metadata is restored; each
     * gets a FRESH shell (running processes cannot be resurrected -- documented).
     */
    fun restoreSessions(savedMetas: List<SessionMeta>, config: AppConfig) {
        savedMetas.forEach { saved ->
            val dir = if (File(saved.workingDir).isDirectory) saved.workingDir
            else TermuxEnvironment.resolveWorkingDir(config.defaultDirectory, fallbackTmp())
            val meta = saved.copy(workingDir = dir, isRunning = false)
            val session = spawn(meta)
            handles.add(Handle(meta, session))
        }
        if (handles.isNotEmpty()) {
            activeId = handles.first().meta.id
        }
        onSessionsChanged?.invoke()
    }

    private fun spawn(meta: SessionMeta): TerminalSession {
        val shell = TermuxEnvironment.shellPath()
        val args = TermuxEnvironment.shellArgs()
        val env = TermuxEnvironment.buildEnv()
        return TerminalSession(
            shell,
            meta.workingDir,
            args,
            env,
            TRANSCRIPT_ROWS,
            sessionClient
        )
    }

    private fun defaultName(): String {
        val n = handles.size + 1
        return "session $n"
    }

    // ---------------------------------------------------------------------
    // Active selection / lifecycle
    // ---------------------------------------------------------------------

    fun setActive(id: String) {
        if (handles.none { it.meta.id == id }) return
        activeId = id
        activeHandle?.meta?.lastUsedEpochMs = nowMs()
        onSessionsChanged?.invoke()
    }

    fun rename(id: String, newName: String) {
        handles.firstOrNull { it.meta.id == id }?.meta?.name = newName.ifBlank { "session" }
        persist()
    }

    fun duplicate(id: String, config: AppConfig): Handle? {
        val src = handles.firstOrNull { it.meta.id == id } ?: return null
        val meta = SessionMeta(
            name = src.meta.name + " (copy)",
            workingDir = src.meta.workingDir,
            startupCommand = src.meta.startupCommand,
            lastUsedEpochMs = nowMs()
        )
        val session = spawn(meta)
        val handle = Handle(meta, session)
        handles.add(handle)
        setActive(meta.id)
        persist()
        return handle
    }

    fun close(id: String) {
        val idx = handles.indexOfFirst { it.meta.id == id }
        if (idx < 0) return
        val handle = handles.removeAt(idx)
        runCatching { handle.session.finishIfRunning() }
        if (activeId == id) {
            activeId = handles.getOrNull(idx.coerceAtMost(handles.size - 1))?.meta?.id
                ?: handles.firstOrNull()?.meta?.id
        }
        persist()
        onSessionsChanged?.invoke()
    }

    fun closeAll() {
        handles.forEach { runCatching { it.session.finishIfRunning() } }
        handles.clear()
        activeId = null
    }

    // ---------------------------------------------------------------------
    // Input routing -- ACTIVE session only
    // ---------------------------------------------------------------------

    /** Insert raw text at the cursor. No submit, no newline. (INSERT_TEXT, voice.) */
    fun insertText(text: String) {
        if (text.isEmpty()) return
        activeSession?.write(text)
    }

    /** Type [command] and submit it with a carriage return. (RUN_COMMAND.) */
    fun runCommand(command: String) {
        val s = activeSession ?: return
        s.write(command + CR)
        activeHandle?.meta?.let { it.lastCommand = command; it.lastUsedEpochMs = nowMs() }
        onSessionsChanged?.invoke()
    }

    /** Run several commands in order, each submitted. (RUN_SCRIPT.) */
    fun runScript(lines: List<String>) {
        val s = activeSession ?: return
        lines.filter { it.isNotEmpty() }.forEach { s.write(it + CR) }
        lines.lastOrNull { it.isNotBlank() }?.let { last ->
            activeHandle?.meta?.let { it.lastCommand = last; it.lastUsedEpochMs = nowMs() }
        }
        onSessionsChanged?.invoke()
    }

    /** `cd <dir>` then run [command]. (PROJECT_COMMAND.) */
    fun runInProjectDir(dir: String, command: String) {
        val s = activeSession ?: return
        if (dir.isNotBlank()) s.write("cd " + shellQuote(dir) + CR)
        if (command.isNotBlank()) {
            s.write(command + CR)
            activeHandle?.meta?.let { it.lastCommand = command }
        }
        activeHandle?.meta?.workingDir = dir
        onSessionsChanged?.invoke()
    }

    /** Send a special key's byte sequence. (SPECIAL_KEY.) */
    fun sendSpecialKey(key: SpecialKey) {
        activeSession?.write(key.sequence)
    }

    /** Raw write used by the keyboard/hardware-key bridge. */
    fun writeRaw(text: String) {
        activeSession?.write(text)
    }

    /**
     * POSIX single-quote a path. Built from char codes (39 = quote, 92 = backslash)
     * to avoid any escape ambiguity: a literal quote becomes the classic '\'' dance.
     */
    private fun shellQuote(path: String): String {
        val q = 39.toChar().toString()              // '
        val esc = q + 92.toChar() + q + q           // '\''
        return q + path.replace(q, esc) + q
    }

    // ---------------------------------------------------------------------
    // Persistence
    // ---------------------------------------------------------------------

    fun persist() {
        sessionStore.save(handles.map { it.meta })
    }

    /** Called by the session client when output arrives, to refresh metadata. */
    fun markActivity(session: TerminalSession) {
        handles.firstOrNull { it.session === session }?.meta?.let { meta ->
            meta.isRunning = session.isRunning
            meta.lastUsedEpochMs = nowMs()
        }
    }

    fun markFinished(session: TerminalSession) {
        handles.firstOrNull { it.session === session }?.meta?.isRunning = false
        onSessionsChanged?.invoke()
    }

    companion object {
        private const val TRANSCRIPT_ROWS = 2000
    }
}
