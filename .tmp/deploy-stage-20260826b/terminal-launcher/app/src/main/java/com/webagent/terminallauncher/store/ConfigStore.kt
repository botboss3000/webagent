package com.webagent.terminallauncher.store

import android.content.Context
import com.webagent.terminallauncher.model.AppConfig
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File

/**
 * Loads and saves [AppConfig] as JSON in the app's private files dir.
 *
 * Writes are atomic (temp file + rename) so a crash mid-save can never leave a
 * truncated config that would brick the footer on next launch.
 */
class ConfigStore(context: Context) {

    private val file = File(context.filesDir, FILE_NAME)
    private val json = Json {
        prettyPrint = true
        ignoreUnknownKeys = true
        encodeDefaults = true
    }

    fun load(): AppConfig {
        if (!file.exists()) {
            val fresh = AppConfig.default()
            save(fresh)
            return fresh
        }
        return runCatching { json.decodeFromString<AppConfig>(file.readText()) }
            .getOrElse {
                // Corrupt config: back it up and fall back to defaults rather than crash.
                runCatching { file.copyTo(File(file.path + ".corrupt"), overwrite = true) }
                AppConfig.default().also { save(it) }
            }
    }

    fun save(config: AppConfig) {
        val tmp = File(file.path + ".tmp")
        tmp.writeText(json.encodeToString(config))
        if (!tmp.renameTo(file)) {
            // Fallback for filesystems where rename-over-existing fails.
            file.writeText(tmp.readText())
            tmp.delete()
        }
    }

    companion object {
        private const val FILE_NAME = "app_config.json"
    }
}
