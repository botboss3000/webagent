package com.webagent.terminallauncher.ui

import android.view.View
import android.view.ViewGroup

/**
 * Slides a panel in/out from the LEFT edge, OVER the terminal -- it never pushes
 * or resizes the terminal content (the panel is a sibling drawn on top, and the
 * terminal keeps its full bounds underneath).
 *
 * A shared scrim dims the terminal and closes the panel when tapped.
 */
class SlidePanelController(
    private val panel: View,
    private val scrim: View
) {
    var isOpen: Boolean = false
        private set

    /** Called when this panel finishes closing (so the host can drop the scrim). */
    var onClosed: (() -> Unit)? = null

    init {
        panel.visibility = View.GONE
        // Park the panel just off the left edge once its width is known.
        panel.post { panel.translationX = -panelWidth().toFloat() }
        scrim.setOnClickListener { close() }
    }

    private fun panelWidth(): Int {
        val w = panel.width
        if (w > 0) return w
        val lp = panel.layoutParams
        return if (lp != null && lp.width > 0 && lp.width != ViewGroup.LayoutParams.MATCH_PARENT)
            lp.width else panel.resources.displayMetrics.run { (widthPixels * 0.82f).toInt() }
    }

    fun open() {
        if (isOpen) return
        isOpen = true
        panel.visibility = View.VISIBLE
        panel.translationX = -panelWidth().toFloat()
        scrim.visibility = View.VISIBLE
        scrim.alpha = 0f
        scrim.animate().alpha(1f).setDuration(DURATION).start()
        panel.animate()
            .translationX(0f)
            .setDuration(DURATION)
            .start()
    }

    fun close() {
        if (!isOpen) return
        isOpen = false
        scrim.animate().alpha(0f).setDuration(DURATION)
            .withEndAction { scrim.visibility = View.GONE }
            .start()
        panel.animate()
            .translationX(-panelWidth().toFloat())
            .setDuration(DURATION)
            .withEndAction {
                panel.visibility = View.GONE
                onClosed?.invoke()
            }
            .start()
    }

    companion object {
        private const val DURATION = 220L
    }
}
