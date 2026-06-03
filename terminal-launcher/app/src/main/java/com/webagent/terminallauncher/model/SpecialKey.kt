package com.webagent.terminallauncher.model

/**
 * A special terminal key that maps to a raw byte sequence written directly into
 * the PTY input stream of the active session.
 *
 * Sequences are declared as integer byte codes (e.g. 27 = ESC, 3 = Ctrl+C) so
 * the source file contains no literal control characters -- this keeps the file
 * portable and diff-friendly. [sequence] assembles them into the actual String
 * that gets written to the PTY.
 *
 * The model is deliberately data-driven: adding a new key later (F-keys, Insert,
 * Delete, Ctrl+anything, ...) is a one-line addition here plus a label in the
 * editor -- no other code needs to change.
 *
 * Cursor keys are emitted in the "normal" CSI form (ESC [ A). Termux's emulator
 * translates these correctly whether or not the foreground app has requested
 * application-cursor mode, so we do not need to track DECCKM ourselves.
 */
enum class SpecialKey(
    val id: String,
    val label: String,
    private val codes: IntArray
) {
    LEFT("left", "Left", intArrayOf(27, '['.code, 'D'.code)),
    RIGHT("right", "Right", intArrayOf(27, '['.code, 'C'.code)),
    UP("up", "Up", intArrayOf(27, '['.code, 'A'.code)),
    DOWN("down", "Down", intArrayOf(27, '['.code, 'B'.code)),
    CTRL_C("ctrl_c", "Ctrl+C", intArrayOf(3)),
    TAB("tab", "Tab", intArrayOf(9)),
    ESCAPE("escape", "Esc", intArrayOf(27)),

    // --- Easy future additions (kept here so the editor can expose them) ---
    ENTER("enter", "Enter", intArrayOf(13)),
    BACKSPACE("backspace", "Bksp", intArrayOf(127)),
    CTRL_D("ctrl_d", "Ctrl+D", intArrayOf(4)),
    CTRL_Z("ctrl_z", "Ctrl+Z", intArrayOf(26)),
    CTRL_L("ctrl_l", "Ctrl+L", intArrayOf(12)),
    HOME("home", "Home", intArrayOf(27, '['.code, 'H'.code)),
    END("end", "End", intArrayOf(27, '['.code, 'F'.code)),
    PAGE_UP("page_up", "PgUp", intArrayOf(27, '['.code, '5'.code, '~'.code)),
    PAGE_DOWN("page_down", "PgDn", intArrayOf(27, '['.code, '6'.code, '~'.code));

    /** The raw bytes to write into the PTY, as a String. */
    val sequence: String
        get() = buildString { codes.forEach { append(it.toChar()) } }

    companion object {
        fun fromId(id: String?): SpecialKey? = entries.firstOrNull { it.id == id }

        /** Keys offered as built-in defaults in the footer's first row. */
        val DEFAULTS = listOf(ESCAPE, TAB, CTRL_C, UP, DOWN, LEFT, RIGHT)
    }
}
