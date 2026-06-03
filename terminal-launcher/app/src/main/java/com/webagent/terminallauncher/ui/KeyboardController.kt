package com.webagent.terminallauncher.ui

import android.content.Context
import android.view.View
import android.view.inputmethod.InputMethodManager

/**
 * Show/hide toggle for the Android soft keyboard, targeting the terminal view.
 *
 * Tap once -> show; tap again -> hide. Nothing more (no custom input bar, no
 * voice coupling). Input from the soft keyboard flows through the TerminalView's
 * own InputConnection straight to the active PTY.
 *
 * We track the desired state locally because Android exposes no fully reliable
 * "is the IME visible" query across all versions; this gives a predictable toggle.
 */
class KeyboardController(
    private val context: Context,
    private val targetView: View
) {
    private val imm get() =
        context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager

    var isShown: Boolean = false
        private set

    fun toggle() {
        if (isShown) hide() else show()
    }

    fun show() {
        targetView.requestFocus()
        imm.showSoftInput(targetView, InputMethodManager.SHOW_IMPLICIT)
        isShown = true
    }

    fun hide() {
        imm.hideSoftInputFromWindow(targetView.windowToken, 0)
        isShown = false
    }
}
