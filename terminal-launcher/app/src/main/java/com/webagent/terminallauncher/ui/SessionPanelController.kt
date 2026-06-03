package com.webagent.terminallauncher.ui

import android.app.AlertDialog
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.ImageButton
import android.widget.TextView
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.webagent.terminallauncher.R
import com.webagent.terminallauncher.terminal.TerminalManager

/**
 * Drives the slide-over session panel: lists sessions with live metadata and
 * exposes switch / rename / duplicate / close / new actions.
 */
class SessionPanelController(
    private val panelRoot: View,
    private val manager: TerminalManager,
    private val onSwitch: (String) -> Unit,
    private val onNew: () -> Unit,
    private val onDuplicate: (String) -> Unit
) {
    private val recycler: RecyclerView = panelRoot.findViewById(R.id.session_list)
    private val adapter = SessionAdapter()

    init {
        recycler.layoutManager = LinearLayoutManager(panelRoot.context)
        recycler.adapter = adapter
        panelRoot.findViewById<View>(R.id.session_new).setOnClickListener { onNew() }
        panelRoot.findViewById<ImageButton>(R.id.session_close)?.let { /* wired by host */ }
    }

    fun refresh() {
        adapter.submit(manager.sessions, manager.activeHandle?.meta?.id)
    }

    private inner class SessionAdapter : RecyclerView.Adapter<SessionVH>() {
        private var items: List<TerminalManager.Handle> = emptyList()
        private var activeId: String? = null

        fun submit(handles: List<TerminalManager.Handle>, active: String?) {
            items = handles.toList()
            activeId = active
            notifyDataSetChanged()
        }

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): SessionVH {
            val v = LayoutInflater.from(parent.context)
                .inflate(R.layout.item_session, parent, false)
            return SessionVH(v)
        }

        override fun getItemCount(): Int = items.size

        override fun onBindViewHolder(holder: SessionVH, position: Int) {
            holder.bind(items[position], items[position].meta.id == activeId)
        }
    }

    private inner class SessionVH(itemView: View) : RecyclerView.ViewHolder(itemView) {
        private val root: View = itemView.findViewById(R.id.item_root)
        private val dot: View = itemView.findViewById(R.id.item_status_dot)
        private val name: TextView = itemView.findViewById(R.id.item_name)
        private val path: TextView = itemView.findViewById(R.id.item_path)
        private val lastCmd: TextView = itemView.findViewById(R.id.item_lastcmd)
        private val meta: TextView = itemView.findViewById(R.id.item_meta)
        private val rename: ImageButton = itemView.findViewById(R.id.item_rename)
        private val duplicate: ImageButton = itemView.findViewById(R.id.item_duplicate)
        private val close: ImageButton = itemView.findViewById(R.id.item_close)

        fun bind(handle: TerminalManager.Handle, isActive: Boolean) {
            val m = handle.meta
            name.text = m.name
            path.text = m.workingDir.ifBlank { "-" }
            lastCmd.text = if (m.lastCommand.isBlank()) "(no command yet)" else "$ " + m.lastCommand
            val running = m.isRunning && handle.session.isRunning
            dot.setBackgroundResource(if (running) R.drawable.dot_running else R.drawable.dot_idle)
            meta.text = (if (running) "running" else "idle") + "  ·  " + relativeTime(m.lastUsedEpochMs)
            root.setBackgroundResource(
                if (isActive) R.drawable.bg_session_active else R.drawable.bg_shortcut
            )

            root.setOnClickListener { onSwitch(m.id); refresh() }
            duplicate.setOnClickListener { onDuplicate(m.id) }
            close.setOnClickListener { confirmClose(m.id, m.name) }
            rename.setOnClickListener { promptRename(m.id, m.name) }
        }
    }

    private fun promptRename(id: String, current: String) {
        val ctx = panelRoot.context
        val input = EditText(ctx).apply {
            setText(current)
            setSelection(text.length)
        }
        AlertDialog.Builder(ctx)
            .setTitle("Rename session")
            .setView(input)
            .setPositiveButton(R.string.dlg_save) { _, _ ->
                manager.rename(id, input.text.toString())
                refresh()
            }
            .setNegativeButton(R.string.dlg_cancel, null)
            .show()
    }

    private fun confirmClose(id: String, name: String) {
        AlertDialog.Builder(panelRoot.context)
            .setTitle("Close \"$name\"?")
            .setMessage("This ends the session and any program running in it.")
            .setPositiveButton("Close") { _, _ ->
                manager.close(id)
                refresh()
            }
            .setNegativeButton(R.string.dlg_cancel, null)
            .show()
    }

    private fun relativeTime(epochMs: Long): String {
        if (epochMs <= 0L) return "just now"
        val diff = System.currentTimeMillis() - epochMs
        return when {
            diff < 60_000 -> "just now"
            diff < 3_600_000 -> "${diff / 60_000}m ago"
            diff < 86_400_000 -> "${diff / 3_600_000}h ago"
            else -> "${diff / 86_400_000}d ago"
        }
    }
}
