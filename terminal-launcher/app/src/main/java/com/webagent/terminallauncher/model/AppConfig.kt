package com.webagent.terminallauncher.model

import com.webagent.terminallauncher.terminal.TermuxEnvironment
import kotlinx.serialization.Serializable

/**
 * Basic, deliberately limited theme customization (per the product brief --
 * "basic theme customization only"). Dark is the default and only ships with a
 * couple of safe knobs: an accent colour and the terminal font size.
 */
@Serializable
data class ThemeConfig(
    /** One of the named accents in [AccentColor]. */
    val accent: String = AccentColor.GREEN.id,
    /** Terminal text size in scaled pixels. */
    val fontSizeSp: Int = 14,
    /** Light mode is opt-in; dark is the product default. */
    val lightMode: Boolean = false
)

enum class AccentColor(val id: String, val hex: Long) {
    GREEN("green", 0xFF3DDC84),
    CYAN("cyan", 0xFF35C5F0),
    AMBER("amber", 0xFFF0A935),
    MAGENTA("magenta", 0xFFE060C0);

    companion object {
        fun fromId(id: String): AccentColor = entries.firstOrNull { it.id == id } ?: GREEN
    }
}

/**
 * The full persisted application configuration. Serialized to JSON by
 * [com.webagent.terminallauncher.store.ConfigStore].
 *
 * [ignoreUnknownKeys] on the decoder means older/newer config files load without
 * crashing -- forward/backward compatible for a non-technical user who never
 * touches the file by hand.
 */
@Serializable
data class AppConfig(
    /** Starting directory for new sessions and for PROJECT_COMMAND shortcuts. */
    var defaultDirectory: String = TermuxEnvironment.DEFAULT_HOME,

    /** Optional command run automatically when a new session starts (e.g. "claude"). */
    var startupCommand: String = "",

    var theme: ThemeConfig = ThemeConfig(),

    /** Footer shortcut rows. Row 0 renders inline in the footer; rest stack above. */
    val rows: MutableList<ShortcutRow> = mutableListOf()
) {
    companion object {
        /**
         * First-run defaults: one row of the seven built-in special keys, plus a
         * couple of genuinely useful command shortcuts so the footer isn't empty.
         */
        fun default(): AppConfig {
            val defaultRow = ShortcutRow(
                label = "Keys",
                shortcuts = SpecialKey.DEFAULTS
                    .map { Shortcut.specialKey(it) }
                    .toMutableList()
            )
            val handyRow = ShortcutRow(
                label = "Handy",
                shortcuts = mutableListOf(
                    Shortcut.command("ls", "ls -la"),
                    Shortcut.command("clear", "clear"),
                    Shortcut.insert("sudo", "sudo ")
                )
            )
            return AppConfig(
                rows = mutableListOf(defaultRow, handyRow)
            )
        }
    }
}
