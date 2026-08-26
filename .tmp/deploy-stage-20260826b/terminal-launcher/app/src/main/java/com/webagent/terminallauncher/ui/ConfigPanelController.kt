package com.webagent.terminallauncher.ui

import android.app.AlertDialog
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.text.Editable
import android.text.TextWatcher
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.HorizontalScrollView
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.Spinner
import android.widget.TextView
import com.google.android.material.materialswitch.MaterialSwitch
import com.webagent.terminallauncher.R
import com.webagent.terminallauncher.model.AccentColor
import com.webagent.terminallauncher.model.AppConfig
import com.webagent.terminallauncher.model.Shortcut
import com.webagent.terminallauncher.model.ShortcutRow
import com.webagent.terminallauncher.model.ShortcutType
import com.webagent.terminallauncher.model.SpecialKey
import com.webagent.terminallauncher.store.ConfigStore

/**
 * Drives the slide-over settings panel.
 *
 * Everything edits the SAME [AppConfig] instance the rest of the app holds; each
 * change is persisted immediately (no separate Save button -- friendlier for a
 * non-technical user) and then surfaced through callbacks:
 *   - [onConfigChanged]  -> re-render footer shortcuts.
 *   - [onThemeChanged]   -> apply font size / accent / light-mode live.
 *
 * The shortcut editor supports the full set required by the brief: add / remove
 * / reorder shortcuts AND add / remove / reorder rows, plus editing every action
 * type (insert text, run command, multi-step script, project command, special key).
 */
class ConfigPanelController(
    private val panelRoot: View,
    private val config: AppConfig,
    private val store: ConfigStore,
    private val onConfigChanged: () -> Unit,
    private val onThemeChanged: () -> Unit,
    private val onLightModeToggled: (Boolean) -> Unit
) {
    private val ctx = panelRoot.context

    private val defaultDir: EditText = panelRoot.findViewById(R.id.config_default_dir)
    private val startupCmd: EditText = panelRoot.findViewById(R.id.config_startup_cmd)
    private val accentContainer: LinearLayout = panelRoot.findViewById(R.id.config_accent_container)
    private val fontValue: TextView = panelRoot.findViewById(R.id.config_font_value)
    private val rowsEditor: LinearLayout = panelRoot.findViewById(R.id.config_rows_editor)
    private val lightSwitch: MaterialSwitch = panelRoot.findViewById(R.id.config_light_mode)

    fun bind() {
        defaultDir.setText(config.defaultDirectory)
        startupCmd.setText(config.startupCommand)
        defaultDir.addTextChangedListener(simpleWatcher {
            config.defaultDirectory = defaultDir.text.toString(); persist(notifyFooter = false)
        })
        startupCmd.addTextChangedListener(simpleWatcher {
            config.startupCommand = startupCmd.text.toString(); persist(notifyFooter = false)
        })

        renderAccents()
        renderFontValue()
        panelRoot.findViewById<ImageButton>(R.id.config_font_minus).setOnClickListener { stepFont(-1) }
        panelRoot.findViewById<ImageButton>(R.id.config_font_plus).setOnClickListener { stepFont(+1) }

        lightSwitch.isChecked = config.theme.lightMode
        lightSwitch.setOnCheckedChangeListener { _, checked ->
            updateTheme(lightMode = checked)
            onLightModeToggled(checked)
        }

        panelRoot.findViewById<Button>(R.id.config_add_row).setOnClickListener {
            config.rows.add(ShortcutRow(label = "Row ${config.rows.size + 1}"))
            persist()
            renderRows()
        }

        renderRows()
    }

    // ---------------------------------------------------------------------
    // Appearance
    // ---------------------------------------------------------------------

    private fun renderAccents() {
        accentContainer.removeAllViews()
        AccentColor.entries.forEach { accent ->
            val selected = config.theme.accent == accent.id
            val size = dp(34)
            val swatch = View(ctx).apply {
                layoutParams = LinearLayout.LayoutParams(size, size).also { it.marginEnd = dp(10) }
                background = GradientDrawable().apply {
                    shape = GradientDrawable.OVAL
                    setColor(accent.hex.toInt())
                    if (selected) setStroke(dp(3), Color.WHITE)
                }
                setOnClickListener {
                    updateTheme(accent = accent.id)
                    renderAccents()
                }
            }
            accentContainer.addView(swatch)
        }
    }

    private fun renderFontValue() {
        fontValue.text = "${config.theme.fontSizeSp}sp"
    }

    private fun stepFont(delta: Int) {
        val next = (config.theme.fontSizeSp + delta).coerceIn(8, 30)
        updateTheme(fontSize = next)
        renderFontValue()
    }

    /** ThemeConfig is a data class (immutable); rebuild it then persist + notify. */
    private fun updateTheme(
        accent: String = config.theme.accent,
        fontSize: Int = config.theme.fontSizeSp,
        lightMode: Boolean = config.theme.lightMode
    ) {
        config.theme = config.theme.copy(accent = accent, fontSizeSp = fontSize, lightMode = lightMode)
        persist(notifyFooter = false)
        onThemeChanged()
    }

    // ---------------------------------------------------------------------
    // Shortcut rows editor
    // ---------------------------------------------------------------------

    private fun renderRows() {
        rowsEditor.removeAllViews()
        config.rows.forEachIndexed { index, row -> rowsEditor.addView(buildRowEditor(index, row)) }
    }

    private fun buildRowEditor(index: Int, row: ShortcutRow): View {
        val container = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            background = ctx.getDrawable(R.drawable.bg_shortcut)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).also { it.bottomMargin = dp(8) }
            setPadding(dp(10), dp(8), dp(6), dp(8))
        }

        // --- header: title + reorder/delete controls ---
        val header = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        val title = TextView(ctx).apply {
            text = if (index == 0) "${row.label.ifBlank { "Row 1" }}  (footer)" else row.label.ifBlank { "Row ${index + 1}" }
            setTextColor(ctx.getColor(R.color.fg_1))
            textSize = 14f
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
            setOnClickListener { promptRenameRow(index) }
        }
        header.addView(title)
        header.addView(iconButton(R.drawable.ic_arrow_up, "Move row up", enabled = index > 0) {
            swapRows(index, index - 1)
        })
        header.addView(iconButton(R.drawable.ic_arrow_down, "Move row down", enabled = index < config.rows.size - 1) {
            swapRows(index, index + 1)
        })
        header.addView(iconButton(R.drawable.ic_delete, "Remove row", enabled = config.rows.size > 1) {
            confirmRemoveRow(index)
        })
        container.addView(header)

        // --- chips: existing shortcuts + an add button ---
        val scroll = HorizontalScrollView(ctx).apply {
            isHorizontalScrollBarEnabled = false
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            ).also { it.topMargin = dp(8) }
        }
        val chips = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL }
        row.shortcuts.forEachIndexed { sIdx, sc ->
            chips.addView(shortcutChip(sc.label) { promptEditShortcut(index, sIdx) })
        }
        chips.addView(addChip { promptEditShortcut(index, null) })
        scroll.addView(chips)
        container.addView(scroll)
        return container
    }

    private fun shortcutChip(label: String, onClick: () -> Unit): TextView {
        val v = LayoutInflater.from(ctx)
            .inflate(R.layout.item_shortcut_button, rowsEditor, false) as TextView
        v.text = label
        v.setOnClickListener { onClick() }
        return v
    }

    private fun addChip(onClick: () -> Unit): TextView {
        val v = LayoutInflater.from(ctx)
            .inflate(R.layout.item_shortcut_button, rowsEditor, false) as TextView
        v.text = "+"
        v.setTextColor(ctx.getColor(R.color.accent_green))
        v.setOnClickListener { onClick() }
        return v
    }

    private fun swapRows(a: Int, b: Int) {
        if (a !in config.rows.indices || b !in config.rows.indices) return
        config.rows.add(b, config.rows.removeAt(a))
        persist()
        renderRows()
    }

    private fun promptRenameRow(index: Int) {
        val input = EditText(ctx).apply { setText(config.rows[index].label) }
        AlertDialog.Builder(ctx)
            .setTitle("Row name")
            .setView(input)
            .setPositiveButton(R.string.dlg_save) { _, _ ->
                config.rows[index] = config.rows[index].copy(label = input.text.toString())
                persist()
                renderRows()
            }
            .setNegativeButton(R.string.dlg_cancel, null)
            .show()
    }

    private fun confirmRemoveRow(index: Int) {
        AlertDialog.Builder(ctx)
            .setTitle("Remove this row?")
            .setMessage("All shortcut buttons in it will be removed.")
            .setPositiveButton(R.string.dlg_delete) { _, _ ->
                config.rows.removeAt(index)
                persist()
                renderRows()
            }
            .setNegativeButton(R.string.dlg_cancel, null)
            .show()
    }

    // ---------------------------------------------------------------------
    // Add / edit a single shortcut
    // ---------------------------------------------------------------------

    private fun promptEditShortcut(rowIndex: Int, shortcutIndex: Int?) {
        val existing = shortcutIndex?.let { config.rows[rowIndex].shortcuts.getOrNull(it) }
        val view = LayoutInflater.from(ctx).inflate(R.layout.dialog_edit_shortcut, null)

        val labelField = view.findViewById<EditText>(R.id.dlg_label)
        val typeSpinner = view.findViewById<Spinner>(R.id.dlg_type)
        val textContainer = view.findViewById<View>(R.id.dlg_text_container)
        val textField = view.findViewById<EditText>(R.id.dlg_text)
        val scriptContainer = view.findViewById<View>(R.id.dlg_script_container)
        val scriptField = view.findViewById<EditText>(R.id.dlg_script)
        val specialContainer = view.findViewById<View>(R.id.dlg_special_container)
        val specialSpinner = view.findViewById<Spinner>(R.id.dlg_special)

        val types = ShortcutType.entries
        typeSpinner.adapter = simpleSpinner(types.map { it.label })
        val keys = SpecialKey.entries.toList()
        specialSpinner.adapter = simpleSpinner(keys.map { it.label })

        fun showFor(type: ShortcutType) {
            textContainer.visibility = if (type == ShortcutType.INSERT_TEXT ||
                type == ShortcutType.RUN_COMMAND || type == ShortcutType.PROJECT_COMMAND
            ) View.VISIBLE else View.GONE
            scriptContainer.visibility = if (type == ShortcutType.RUN_SCRIPT) View.VISIBLE else View.GONE
            specialContainer.visibility = if (type == ShortcutType.SPECIAL_KEY) View.VISIBLE else View.GONE
        }

        // Prefill from existing shortcut.
        if (existing != null) {
            labelField.setText(existing.label)
            typeSpinner.setSelection(types.indexOf(existing.type).coerceAtLeast(0))
            textField.setText(existing.text)
            scriptField.setText(existing.scriptLines.joinToString(System.lineSeparator()))
            existing.specialKey?.let { specialSpinner.setSelection(keys.indexOf(it).coerceAtLeast(0)) }
        }
        showFor(existing?.type ?: ShortcutType.RUN_COMMAND)

        typeSpinner.onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: android.widget.AdapterView<*>?, v: View?, pos: Int, id: Long) =
                showFor(types[pos])
            override fun onNothingSelected(p: android.widget.AdapterView<*>?) {}
        }

        val builder = AlertDialog.Builder(ctx)
            .setTitle(if (existing == null) R.string.cfg_add_shortcut else R.string.dlg_save)
            .setView(view)
            .setPositiveButton(R.string.dlg_save) { _, _ ->
                val type = types[typeSpinner.selectedItemPosition]
                val label = labelField.text.toString().ifBlank {
                    when (type) {
                        ShortcutType.SPECIAL_KEY -> keys[specialSpinner.selectedItemPosition].label
                        else -> textField.text.toString().take(12).ifBlank { "btn" }
                    }
                }
                val newShortcut = Shortcut(
                    id = existing?.id ?: java.util.UUID.randomUUID().toString(),
                    label = label,
                    type = type,
                    text = textField.text.toString(),
                    scriptLines = scriptField.text.toString()
                        .split(System.lineSeparator()).map { it.trimEnd() }.filter { it.isNotEmpty() },
                    specialKeyId = keys[specialSpinner.selectedItemPosition].id
                )
                val list = config.rows[rowIndex].shortcuts
                if (shortcutIndex != null && shortcutIndex in list.indices) list[shortcutIndex] = newShortcut
                else list.add(newShortcut)
                persist()
                renderRows()
            }
            .setNegativeButton(R.string.dlg_cancel, null)

        if (existing != null) {
            builder.setNeutralButton(R.string.dlg_delete) { _, _ ->
                config.rows[rowIndex].shortcuts.removeAt(shortcutIndex!!)
                persist()
                renderRows()
            }
        }
        builder.show()
    }

    // ---------------------------------------------------------------------
    // helpers
    // ---------------------------------------------------------------------

    private fun persist(notifyFooter: Boolean = true) {
        store.save(config)
        if (notifyFooter) onConfigChanged()
    }

    private fun simpleSpinner(items: List<String>): ArrayAdapter<String> =
        ArrayAdapter(ctx, android.R.layout.simple_spinner_dropdown_item, items)

    private fun iconButton(icon: Int, desc: String, enabled: Boolean, onClick: () -> Unit): ImageButton {
        return ImageButton(ctx).apply {
            layoutParams = LinearLayout.LayoutParams(dp(36), dp(36))
            setImageResource(icon)
            background = ctx.getDrawable(R.drawable.bg_footer_button)
            setPadding(dp(7), dp(7), dp(7), dp(7))
            contentDescription = desc
            imageTintList = android.content.res.ColorStateList.valueOf(ctx.getColor(R.color.fg_2))
            isEnabled = enabled
            alpha = if (enabled) 1f else 0.3f
            setOnClickListener { onClick() }
        }
    }

    private fun dp(v: Int): Int = (v * ctx.resources.displayMetrics.density).toInt()

    private fun simpleWatcher(after: () -> Unit) = object : TextWatcher {
        override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
        override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
        override fun afterTextChanged(s: Editable?) = after()
    }
}
