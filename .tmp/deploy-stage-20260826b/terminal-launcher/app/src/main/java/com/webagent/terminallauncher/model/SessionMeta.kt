package com.webagent.terminallauncher.model

import kotlinx.serialization.Serializable
import java.util.UUID

/**
 * Persisted metadata about a terminal session.
 *
 * IMPORTANT (documented limitation): only metadata survives a process death.
 * The live PTY child process is owned by our process; when Android kills us, the
 * child dies too. On relaunch we restore this metadata and re-open a *fresh*
 * shell per restored session -- we cannot resurrect the previously running
 * program. See README "Session persistence".
 */
@Serializable
data class SessionMeta(
    val id: String = UUID.randomUUID().toString(),
    var name: String,
    var workingDir: String = "",
    var lastCommand: String = "",
    /** Best-effort -- true while the foreground child is producing output. */
    var isRunning: Boolean = false,
    var lastUsedEpochMs: Long = 0L,
    /** Startup command to replay when this session is (re)created. */
    var startupCommand: String = ""
)
