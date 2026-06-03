package com.webagent.terminallauncher.model

import kotlinx.serialization.Serializable
import java.util.UUID

/**
 * The five action types a footer shortcut button can perform.
 *
 * Behaviour contract (enforced in [com.webagent.terminallauncher.terminal.TerminalManager]):
 *  - INSERT_TEXT      -- inserts [text] at the cursor; does NOT submit, no newline.
 *  - RUN_COMMAND      -- writes [text] followed by a carriage return (runs now).
 *  - RUN_SCRIPT       -- writes each line of [scriptLines] followed by CR, in order.
 *  - PROJECT_COMMAND  -- `cd <defaultDirectory>` then runs [text] (both submitted).
 *  - SPECIAL_KEY      -- writes the byte sequence for [specialKeyId].
 *
 * Every action targets the ACTIVE session only.
 */
enum class ShortcutType(val label: String) {
    INSERT_TEXT("Insert text"),
    RUN_COMMAND("Run command"),
    RUN_SCRIPT("Run multi-step script"),
    PROJECT_COMMAND("Run in project dir"),
    SPECIAL_KEY("Special key")
}

/**
 * A single configurable footer button.
 *
 * One flat class (rather than a sealed hierarchy) keeps JSON persistence simple
 * and forgiving across config versions: unknown future fields can be ignored and
 * missing ones fall back to defaults.
 */
@Serializable
data class Shortcut(
    val id: String = UUID.randomUUID().toString(),
    val label: String,
    val type: ShortcutType,

    /** Payload for INSERT_TEXT / RUN_COMMAND / PROJECT_COMMAND. */
    val text: String = "",

    /** Payload for RUN_SCRIPT -- executed line by line. */
    val scriptLines: List<String> = emptyList(),

    /** Payload for SPECIAL_KEY -- references [SpecialKey.id]. */
    val specialKeyId: String? = null
) {
    val specialKey: SpecialKey?
        get() = SpecialKey.fromId(specialKeyId)

    companion object {
        fun specialKey(key: SpecialKey): Shortcut =
            Shortcut(label = key.label, type = ShortcutType.SPECIAL_KEY, specialKeyId = key.id)

        fun insert(label: String, text: String): Shortcut =
            Shortcut(label = label, type = ShortcutType.INSERT_TEXT, text = text)

        fun command(label: String, command: String): Shortcut =
            Shortcut(label = label, type = ShortcutType.RUN_COMMAND, text = command)
    }
}

/**
 * A horizontal row of shortcut buttons. The footer renders row index 0 inline;
 * any additional rows stack above the footer.
 */
@Serializable
data class ShortcutRow(
    val id: String = UUID.randomUUID().toString(),
    val label: String = "",
    val shortcuts: MutableList<Shortcut> = mutableListOf()
)
