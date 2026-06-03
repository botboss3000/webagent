package com.webagent.terminallauncher.terminal

import android.content.Context
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.system.Os
import android.util.Log
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.zip.ZipInputStream

/**
 * Installs the Termux Linux environment ("bootstrap") into this app's private
 * files dir on first run.
 *
 * This is what makes the FORK build self-contained: because this app's
 * applicationId is `com.termux`, its private dir is exactly
 * `/data/data/com.termux/files`, which is the path the prebuilt Termux binaries
 * have baked into their shebangs and rpaths. So we can extract the official
 * prebuilt bootstrap unchanged and everything resolves correctly.
 *
 * The logic mirrors Termux's own `TermuxInstaller`:
 *   1. Pick the bootstrap for the device CPU ABI.
 *   2. Download the official prebuilt zip.
 *   3. Extract into a staging dir, recreating the symlinks listed in SYMLINKS.txt.
 *   4. Mark binaries executable, then atomically rename staging -> usr.
 *
 * Executing these binaries from the app's home dir requires the app to keep
 * `targetSdkVersion 28` (see build.gradle.kts) -- on target 29+ Android's SELinux
 * policy blocks exec from the writable data dir. This is the same reason Termux
 * itself targets 28 and is distributed via F-Droid rather than the Play Store.
 */
class BootstrapInstaller(private val context: Context) {

    interface Callback {
        /** [percent] is -1 when indeterminate. */
        fun onProgress(message: String, percent: Int)
        fun onComplete()
        fun onError(message: String)
    }

    private val main = Handler(Looper.getMainLooper())

    fun installAsync(callback: Callback) {
        Thread({
            try {
                install { msg, pct -> main.post { callback.onProgress(msg, pct) } }
                main.post { callback.onComplete() }
            } catch (t: Throwable) {
                Log.e(TAG, "Bootstrap install failed", t)
                main.post { callback.onError(t.message ?: "Unknown error during setup.") }
            }
        }, "bootstrap-installer").start()
    }

    private fun install(progress: (String, Int) -> Unit) {
        val arch = bootstrapArch()
        val prefix = File(context.filesDir, "usr")
        val staging = File(context.filesDir, "usr-staging")
        val home = File(context.filesDir, "home")

        deleteRecursively(staging)
        deleteRecursively(prefix)
        staging.mkdirs()
        home.mkdirs()

        val url = "$BOOTSTRAP_BASE/$BOOTSTRAP_VERSION/bootstrap-$arch.zip"
        val zipFile = File(context.cacheDir, "bootstrap-$arch.zip")
        progress("Downloading Linux environment ($arch)...", -1)
        download(url, zipFile, progress)

        progress("Unpacking...", -1)
        val symlinks = ArrayList<Pair<String, String>>() // target -> linkPath
        ZipInputStream(zipFile.inputStream().buffered()).use { zin ->
            var entry = zin.nextEntry
            val buffer = ByteArray(64 * 1024)
            while (entry != null) {
                val name = entry.name
                if (name == "SYMLINKS.txt") {
                    val text = zin.readBytes().toString(Charsets.UTF_8)
                    parseSymlinks(text, staging, symlinks)
                } else if (entry.isDirectory) {
                    File(staging, name).mkdirs()
                } else {
                    val out = File(staging, name)
                    out.parentFile?.mkdirs()
                    FileOutputStream(out).use { fos ->
                        var n = zin.read(buffer)
                        while (n != -1) {
                            fos.write(buffer, 0, n)
                            n = zin.read(buffer)
                        }
                    }
                }
                zin.closeEntry()
                entry = zin.nextEntry
            }
        }
        zipFile.delete()

        progress("Linking and setting permissions...", -1)
        symlinks.forEach { (target, linkPath) ->
            val link = File(linkPath)
            link.parentFile?.mkdirs()
            runCatching { link.delete() }
            runCatching { Os.symlink(target, link.absolutePath) }
                .onFailure { Log.w(TAG, "symlink failed: $linkPath -> $target") }
        }
        markExecutable(staging)

        if (!staging.renameTo(prefix)) {
            throw IllegalStateException("Could not finalize environment (rename failed).")
        }
        progress("Environment ready.", 100)
    }

    /** SYMLINKS.txt lines are `target<U+2190>linkPath`, link paths relative to usr/. */
    private fun parseSymlinks(text: String, staging: File, out: MutableList<Pair<String, String>>) {
        val arrow = 0x2190.toChar() // a 'leftwards arrow' separates target from link
        text.lines().forEach { line ->
            if (line.isBlank()) return@forEach
            val parts = line.split(arrow)
            if (parts.size == 2) {
                val target = parts[0]
                val linkPath = File(staging, parts[1]).absolutePath
                out.add(target to linkPath)
            }
        }
    }

    private fun markExecutable(dir: File) {
        dir.walkTopDown().forEach { f ->
            if (f.isFile) {
                runCatching { f.setReadable(true, false) }
                runCatching { f.setExecutable(true, false) }
            }
        }
    }

    private fun download(urlStr: String, dest: File, progress: (String, Int) -> Unit) {
        val conn = (URL(urlStr).openConnection() as HttpURLConnection).apply {
            instanceFollowRedirects = true
            connectTimeout = 30_000
            readTimeout = 30_000
        }
        try {
            conn.connect()
            if (conn.responseCode !in 200..299) {
                throw IllegalStateException(
                    "Download failed (HTTP ${conn.responseCode}). Check your connection, " +
                        "or update the bootstrap version in BootstrapInstaller."
                )
            }
            val total = conn.contentLength.toLong()
            conn.inputStream.use { input ->
                FileOutputStream(dest).use { output ->
                    val buffer = ByteArray(64 * 1024)
                    var downloaded = 0L
                    var n = input.read(buffer)
                    while (n != -1) {
                        output.write(buffer, 0, n)
                        downloaded += n
                        if (total > 0) {
                            val pct = ((downloaded * 100) / total).toInt()
                            progress("Downloading Linux environment... $pct%", pct)
                        }
                        n = input.read(buffer)
                    }
                }
            }
        } finally {
            conn.disconnect()
        }
    }

    private fun bootstrapArch(): String {
        // Map the primary supported ABI to the Termux bootstrap arch name.
        for (abi in Build.SUPPORTED_ABIS) {
            when (abi) {
                "arm64-v8a" -> return "aarch64"
                "armeabi-v7a" -> return "arm"
                "x86_64" -> return "x86_64"
                "x86" -> return "i686"
            }
        }
        throw IllegalStateException("Unsupported CPU ABI: ${Build.SUPPORTED_ABIS.joinToString()}")
    }

    private fun deleteRecursively(file: File) {
        if (file.exists()) file.deleteRecursively()
    }

    companion object {
        private const val TAG = "BootstrapInstaller"

        /** Official Termux prebuilt bootstraps. */
        private const val BOOTSTRAP_BASE =
            "https://github.com/termux/termux-packages/releases/download"

        /**
         * The bootstrap release tag to fetch. Update this to the latest tag at
         * https://github.com/termux/termux-packages/releases (look for a release
         * named `bootstrap-...` that has `bootstrap-aarch64.zip` etc. attached).
         */
        const val BOOTSTRAP_VERSION = "bootstrap-2024.12.06-r1+apt-android-7"

        fun isInstalled(context: Context): Boolean =
            File(context.filesDir, "usr/bin/bash").exists() ||
                File(context.filesDir, "usr/bin/sh").exists()
    }
}
