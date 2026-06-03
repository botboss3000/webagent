package com.webagent.terminallauncher.store

import android.content.Context
import com.webagent.terminallauncher.model.SessionMeta
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import java.io.File

/**
 * Persists the list of session metadata so sessions (names, last dir, last
 * command, order) survive an app restart. Live processes are NOT persisted --
 * see [SessionMeta] and the README.
 */
class SessionStore(context: Context) {

    private val file = File(context.filesDir, FILE_NAME)
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    private val serializer = ListSerializer(SessionMeta.serializer())

    fun load(): MutableList<SessionMeta> {
        if (!file.exists()) return mutableListOf()
        return runCatching {
            json.decodeFromString(serializer, file.readText()).toMutableList()
        }.getOrElse { mutableListOf() }
    }

    fun save(sessions: List<SessionMeta>) {
        val tmp = File(file.path + ".tmp")
        tmp.writeText(json.encodeToString(serializer, sessions))
        if (!tmp.renameTo(file)) {
            file.writeText(tmp.readText())
            tmp.delete()
        }
    }

    companion object {
        private const val FILE_NAME = "sessions.json"
    }
}
