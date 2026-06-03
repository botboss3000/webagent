package com.webagent.terminallauncher.ui

import android.view.LayoutInflater
import android.view.View
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.TextView
import com.webagent.terminallauncher.R
import com.webagent.terminallauncher.model.AppConfig
import com.webagent.terminallauncher.model.Shortcut

/**
 * Renders the configurable shortcut rows into the footer.
 *
 * Layout rules (matching the brief):
 *  - Row index 0 renders inline in the main footer ([row0Container]).
 *  - Rows 1..n stack ABOVE the footer in [stackedContainer]; row 1 sits closest
 *    to the footer, higher indices stack upward.
 *  - Each row scrolls horizontally so any number of buttons fits on any screen.
 *
 * Clicking a button forwards the [Shortcut] to [onShortcut]; the manager decides
 * what the action does. Buttons act on the ACTIVE session only.
 */
class FooterController(
    private val inflater: LayoutInflater,
    private val row0Container: LinearLayout,
    private val stackedContainer: LinearLayout,
    private val onShortcut: (Shortcut) -> Unit
) {
    fun render(config: AppConfig) {
        row0Container.removeAllViews()
        stackedContainer.removeAllViews()

        val rows = config.rows
        if (rows.isEmpty()) return

        // Row 0 -> inline footer.
        rows[0].shortcuts.forEach { row0Container.addView(makeButton(it)) }

        // Rows 1..n -> stacked above, row 1 nearest the footer.
        // Add highest index first so the vertical container ends with row 1 on bottom.
        for (i in rows.indices.reversed()) {
            if (i == 0) continue
            stackedContainer.addView(makeRow(rows[i].shortcuts))
        }
    }

    private fun makeRow(shortcuts: List<Shortcut>): View {
        val scroll = HorizontalScrollView(stackedContainer.context).apply {
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.bottomMargin = dp(4) }
            isHorizontalScrollBarEnabled = false
            overScrollMode = View.OVER_SCROLL_NEVER
        }
        val inner = LinearLayout(stackedContainer.context).apply {
            orientation = LinearLayout.HORIZONTAL
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            )
        }
        shortcuts.forEach { inner.addView(makeButton(it)) }
        scroll.addView(inner)
        return scroll
    }

    private fun makeButton(shortcut: Shortcut): TextView {
        val view = inflater.inflate(R.layout.item_shortcut_button, row0Container, false) as TextView
        view.text = shortcut.label
        view.setOnClickListener { onShortcut(shortcut) }
        return view
    }

    private fun dp(value: Int): Int =
        (value * stackedContainer.resources.displayMetrics.density).toInt()
}
