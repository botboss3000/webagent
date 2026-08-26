package com.webagent.terminallauncher.terminal

import android.util.Log
import android.view.KeyEvent
import android.view.MotionEvent
import com.termux.terminal.TerminalSession
import com.termux.view.TerminalView
import com.termux.view.TerminalViewClient

/**
 * Input/gesture policy for the [TerminalView]. Defined by the Termux
 * `terminal-view` library; see the version note in [TerminalSessionClientImpl].
 *
 * Behaviour:
 *  - Pinch-to-zoom adjusts terminal font size within sane bounds.
 *  - Hardware keyboard, arrows, Tab, Esc, Enter, Backspace and Ctrl-combos are
 *    left to the TerminalView's built-in translation (we return false), which
 *    already encodes them correctly for the PTY.
 *  - Single tap focuses the terminal so the soft keyboard (when shown) targets it.
 */
class TerminalViewClientImpl(
    private val terminalView: TerminalView
) : TerminalViewClient {

    /** Notified when the user single-taps the terminal (used to grab focus). */
    var onSingleTap: (() -> Unit)? = null

    private var textSizeSp = 14f

    fun setTextSizeSp(sp: Float) {
        textSizeSp = sp.coerceIn(MIN_SP, MAX_SP)
        terminalView.setTextSize(spToPx(textSizeSp))
    }

    override fun onScale(scale: Float): Float {
        val newSize = (textSizeSp * scale).coerceIn(MIN_SP, MAX_SP)
        if (newSize != textSizeSp) {
            textSizeSp = newSize
            terminalView.setTextSize(spToPx(textSizeSp))
        }
        return textSizeSp
    }

    override fun onSingleTapUp(e: MotionEvent?) {
        terminalView.requestFocus()
        onSingleTap?.invoke()
    }

    override fun shouldBackButtonBeMappedToEscape(): Boolean = false
    override fun shouldEnforceCharBasedInput(): Boolean = true
    override fun shouldUseCtrlSpaceWorkaround(): Boolean = false
    override fun isTerminalViewSelected(): Boolean = true
    override fun copyModeChanged(copyMode: Boolean) { /* no-op */ }

    override fun onKeyDown(keyCode: Int, e: KeyEvent?, session: TerminalSession?): Boolean {
        // Let TerminalView translate hardware keys into PTY bytes.
        return false
    }

    override fun onKeyUp(keyCode: Int, e: KeyEvent?): Boolean = false
    override fun onLongPress(event: MotionEvent?): Boolean = false

    // No on-screen virtual modifier toggles in this build (Ctrl/Alt/Fn handled
    // by hardware keyboard or the Ctrl+C/Esc/Tab shortcut buttons instead).
    override fun readControlKey(): Boolean = false
    override fun readAltKey(): Boolean = false
    override fun readShiftKey(): Boolean = false
    override fun readFnKey(): Boolean = false

    override fun onCodePoint(codePoint: Int, ctrlDown: Boolean, session: TerminalSession?): Boolean {
        // false -> default handling writes the code point to the PTY.
        return false
    }

    override fun onEmulatorSet() { /* no-op */ }

    override fun logError(tag: String?, message: String?) { Log.e(tag ?: TAG, message ?: "") }
    override fun logWarn(tag: String?, message: String?) { Log.w(tag ?: TAG, message ?: "") }
    override fun logInfo(tag: String?, message: String?) { Log.i(tag ?: TAG, message ?: "") }
    override fun logDebug(tag: String?, message: String?) { Log.d(tag ?: TAG, message ?: "") }
    override fun logVerbose(tag: String?, message: String?) { Log.v(tag ?: TAG, message ?: "") }
    override fun logStackTraceWithMessage(tag: String?, message: String?, e: Exception?) {
        Log.e(tag ?: TAG, message ?: "", e)
    }
    override fun logStackTrace(tag: String?, e: Exception?) { Log.e(tag ?: TAG, "", e) }

    private fun spToPx(sp: Float): Int =
        (sp * terminalView.resources.displayMetrics.scaledDensity).toInt()

    companion object {
        private const val TAG = "TerminalLauncher"
        private const val MIN_SP = 8f
        private const val MAX_SP = 30f
    }
}
