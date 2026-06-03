package com.webagent.terminallauncher

import android.app.Application
import androidx.appcompat.app.AppCompatDelegate
import com.webagent.terminallauncher.store.ConfigStore

/**
 * Applies the persisted day/night choice before any Activity inflates, so the
 * app opens straight into the user's chosen theme with no flash. Dark is the
 * product default; light is opt-in via Settings.
 */
class TerminalLauncherApp : Application() {
    override fun onCreate() {
        super.onCreate()
        val config = ConfigStore(this).load()
        AppCompatDelegate.setDefaultNightMode(
            if (config.theme.lightMode) AppCompatDelegate.MODE_NIGHT_NO
            else AppCompatDelegate.MODE_NIGHT_YES
        )
    }
}
