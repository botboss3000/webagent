package com.webagent.terminallauncher.terminal

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.util.Log
import com.termux.terminal.TerminalSession
import com.termux.terminal.TerminalSessionClient
import com.termux.view.TerminalView

/**
 * Bridges a Termux [TerminalSession] back into our app: refreshes the view when
 * output arrives, mirrors copy/paste through the Android clipboard, and keeps
 * [TerminalManager] metadata current.
 *
 * NOTE ON LIBRARY VERSIONS: this interface is defined by the Termux
 * `terminal-emulator` library. The method set below matches the v0.118 release
 * pinned in build.gradle.kts. If you bump the library and the compiler reports a
 * missing/extra override, align the overrides here with that version's
 * `TerminalSessionClient.java` -- this is the single place to do it.
 */
class TerminalSessionClientImpl(
    private val context: Context
) : TerminalSessionClient {

    /** Set by the Activity; the currently attached view (may be null pre-attach). */
    var terminalView: TerminalView? = null

    /** Set after construction to break the manager<->client cycle. */
    var manager: TerminalManager? = null

    /** UI refresh hook (titles, running indicator, session panel). */
    var onUiShouldRefresh: (() -> Unit)? = null

    private val clipboard get() =
        context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager

    override fun onTextChanged(changedSession: TerminalSession) {
        // Only repaint if the changed session is the one on screen.
        if (terminalView?.currentSession === changedSession) {
            terminalView?.onScreenUpdated()
        }
        manager?.markActivity(changedSession)
        onUiShouldRefresh?.invoke()
    }

    override fun onTitleChanged(changedSession: TerminalSession) {
        onUiShouldRefresh?.invoke()
    }

    override fun onSessionFinished(finishedSession: TerminalSession) {
        manager?.markFinished(finishedSession)
        onUiShouldRefresh?.invoke()
    }

    override fun onCopyTextToClipboard(session: TerminalSession, text: String?) {
        if (text.isNullOrEmpty()) return
        clipboard.setPrimaryClip(ClipData.newPlainText("terminal", text))
    }

    override fun onPasteTextFromClipboard(session: TerminalSession?) {
        val clip = clipboard.primaryClip ?: return
        if (clip.itemCount == 0) return
        val text = clip.getItemAt(0).coerceToText(context)?.toString() ?: return
        // Paste behaves like typed input: straight into the PTY, no newline added.
        session?.write(text)
    }

    override fun onBell(session: TerminalSession) {
        // Quietly ignore the bell -- a non-technical user does not want beeps.
    }

    override fun onColorsChanged(session: TerminalSession) {
        if (terminalView?.currentSession === session) terminalView?.onScreenUpdated()
    }

    override fun onTerminalCursorStateChange(state: Boolean) { /* no-op */ }

    override fun setTerminalShellPid(session: TerminalSession, pid: Int) { /* tracked by OS */ }

    /** Return null to keep the emulator's default cursor style. */
    override fun getTerminalCursorStyle(): Int? = null

    // --- Logging: route Termux's internal logs to Logcat under one tag. ---
    override fun logError(tag: String?, message: String?) { Log.e(orTag(tag), message ?: "") }
    override fun logWarn(tag: String?, message: String?) { Log.w(orTag(tag), message ?: "") }
    override fun logInfo(tag: String?, message: String?) { Log.i(orTag(tag), message ?: "") }
    override fun logDebug(tag: String?, message: String?) { Log.d(orTag(tag), message ?: "") }
    override fun logVerbose(tag: String?, message: String?) { Log.v(orTag(tag), message ?: "") }
    override fun logStackTraceWithMessage(tag: String?, message: String?, e: Exception?) {
        Log.e(orTag(tag), message ?: "", e)
    }
    override fun logStackTrace(tag: String?, e: Exception?) { Log.e(orTag(tag), "", e) }

    private fun orTag(tag: String?) = tag ?: TAG

    companion object {
        private const val TAG = "TerminalLauncher"
    }
}
