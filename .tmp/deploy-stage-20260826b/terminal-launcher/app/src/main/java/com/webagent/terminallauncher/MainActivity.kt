package com.webagent.terminallauncher

import android.Manifest
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.ImageButton
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDelegate
import androidx.core.view.WindowCompat
import com.termux.view.TerminalView
import com.webagent.terminallauncher.model.AccentColor
import com.webagent.terminallauncher.model.AppConfig
import com.webagent.terminallauncher.model.Shortcut
import com.webagent.terminallauncher.model.ShortcutType
import com.webagent.terminallauncher.store.ConfigStore
import com.webagent.terminallauncher.store.SessionStore
import com.webagent.terminallauncher.terminal.BootstrapInstaller
import com.webagent.terminallauncher.terminal.TerminalManager
import com.webagent.terminallauncher.terminal.TerminalSessionClientImpl
import com.webagent.terminallauncher.terminal.TerminalViewClientImpl
import com.webagent.terminallauncher.terminal.TermuxEnvironment
import com.webagent.terminallauncher.ui.ConfigPanelController
import com.webagent.terminallauncher.ui.FooterController
import com.webagent.terminallauncher.ui.KeyboardController
import com.webagent.terminallauncher.ui.SessionPanelController
import com.webagent.terminallauncher.ui.SlidePanelController
import com.webagent.terminallauncher.ui.VoiceInputController

/**
 * The single Activity. Opens straight into the terminal (no onboarding, no top
 * bar). Coordinates the terminal backend, footer, the two slide-over panels,
 * voice input and the keyboard toggle.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var config: AppConfig
    private lateinit var configStore: ConfigStore
    private lateinit var sessionStore: SessionStore

    private lateinit var terminalView: TerminalView
    private lateinit var sessionClient: TerminalSessionClientImpl
    private lateinit var viewClient: TerminalViewClientImpl
    private lateinit var manager: TerminalManager

    private lateinit var footer: FooterController
    private lateinit var configPanel: ConfigPanelController
    private lateinit var sessionPanel: SessionPanelController
    private lateinit var configSlide: SlidePanelController
    private lateinit var sessionSlide: SlidePanelController

    private lateinit var keyboard: KeyboardController
    private lateinit var voice: VoiceInputController
    private lateinit var voiceButton: ImageButton

    private var status = TermuxEnvironment.Status.NEEDS_BOOTSTRAP
    private var started = false

    private val micPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) voice.start()
        else toast("Microphone permission is needed for voice input.")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, true)
        setContentView(R.layout.activity_main)

        configStore = ConfigStore(this)
        sessionStore = SessionStore(this)
        config = configStore.load()

        // Keep the day/night delegate in sync with the saved preference. Changing
        // it (in the light-mode toggle) recreates the Activity with the new theme.
        AppCompatDelegate.setDefaultNightMode(
            if (config.theme.lightMode) AppCompatDelegate.MODE_NIGHT_NO
            else AppCompatDelegate.MODE_NIGHT_YES
        )

        status = TermuxEnvironment.status(this)

        bindTerminal()
        bindFooter()
        bindPanels()
        bindVoiceAndKeyboard()
        applyTheme()

        if (status == TermuxEnvironment.Status.READY) {
            startTerminal()
        } else {
            showSetupScreen()
        }
    }

    // ---------------------------------------------------------------------
    // Terminal backend
    // ---------------------------------------------------------------------

    private fun bindTerminal() {
        terminalView = findViewById(R.id.terminal_view)

        sessionClient = TerminalSessionClientImpl(this).apply {
            terminalView = this@MainActivity.terminalView
        }
        viewClient = TerminalViewClientImpl(terminalView)
        terminalView.setTerminalViewClient(viewClient)
        viewClient.onSingleTap = { /* tap focuses; keyboard stays under user's control */ }

        manager = TerminalManager(applicationContext, sessionStore, sessionClient)
        sessionClient.manager = manager
        sessionClient.onUiShouldRefresh = { runOnUiThread { if (started) sessionPanel.refresh() } }
        manager.onSessionsChanged = { runOnUiThread { if (started) sessionPanel.refresh() } }
    }

    private fun startTerminal() {
        findViewById<View>(R.id.setup_screen).visibility = View.GONE
        findViewById<View>(R.id.content).visibility = View.VISIBLE
        started = true

        val saved = sessionStore.load()
        if (saved.isNotEmpty()) {
            manager.restoreSessions(saved, config)
        }
        if (manager.isEmpty) {
            manager.createSession(config) // one default session on first launch
        }
        attachActiveSession()
        footer.render(config)
        sessionPanel.refresh()
        applyTheme()
    }

    private fun attachActiveSession() {
        manager.activeSession?.let { terminalView.attachSession(it) }
        viewClient.setTextSizeSp(config.theme.fontSizeSp.toFloat())
        terminalView.requestFocus()
    }

    // ---------------------------------------------------------------------
    // Footer + shortcut dispatch
    // ---------------------------------------------------------------------

    private fun bindFooter() {
        footer = FooterController(
            layoutInflater,
            findViewById(R.id.row0_container),
            findViewById(R.id.stacked_rows),
            onShortcut = ::dispatchShortcut
        )
        findViewById<ImageButton>(R.id.btn_config).setOnClickListener { openConfig() }
        findViewById<ImageButton>(R.id.btn_sessions).setOnClickListener { openSessions() }
    }

    /** Runs a shortcut against the ACTIVE session only. */
    private fun dispatchShortcut(s: Shortcut) {
        if (!started) return
        when (s.type) {
            ShortcutType.INSERT_TEXT -> manager.insertText(s.text)
            ShortcutType.RUN_COMMAND -> manager.runCommand(s.text)
            ShortcutType.RUN_SCRIPT -> manager.runScript(s.scriptLines)
            ShortcutType.PROJECT_COMMAND -> manager.runInProjectDir(config.defaultDirectory, s.text)
            ShortcutType.SPECIAL_KEY -> s.specialKey?.let { manager.sendSpecialKey(it) }
        }
        terminalView.requestFocus()
    }

    // ---------------------------------------------------------------------
    // Slide-over panels
    // ---------------------------------------------------------------------

    private fun bindPanels() {
        val scrim = findViewById<View>(R.id.scrim)
        val configRoot = findViewById<View>(R.id.config_panel)
        val sessionRoot = findViewById<View>(R.id.session_panel)

        configSlide = SlidePanelController(configRoot, scrim)
        sessionSlide = SlidePanelController(sessionRoot, scrim)

        configPanel = ConfigPanelController(
            panelRoot = configRoot,
            config = config,
            store = configStore,
            onConfigChanged = { footer.render(config) },
            onThemeChanged = { applyTheme() },
            onLightModeToggled = { light ->
                configStore.save(config)
                // This applies the new theme and recreates the Activity for us.
                AppCompatDelegate.setDefaultNightMode(
                    if (light) AppCompatDelegate.MODE_NIGHT_NO else AppCompatDelegate.MODE_NIGHT_YES
                )
            }
        )
        configPanel.bind()

        sessionPanel = SessionPanelController(
            panelRoot = sessionRoot,
            manager = manager,
            onSwitch = { id -> manager.setActive(id); attachActiveSession(); sessionSlide.close() },
            onNew = { manager.createSession(config); attachActiveSession(); footer.render(config); sessionSlide.close() },
            onDuplicate = { id -> manager.duplicate(id, config); attachActiveSession(); sessionPanel.refresh() }
        )

        configRoot.findViewById<ImageButton>(R.id.config_close).setOnClickListener { configSlide.close() }
        sessionRoot.findViewById<ImageButton>(R.id.session_close).setOnClickListener { sessionSlide.close() }
    }

    private fun openConfig() {
        sessionSlide.close()
        configSlide.open()
    }

    private fun openSessions() {
        configSlide.close()
        if (started) sessionPanel.refresh()
        sessionSlide.open()
    }

    // ---------------------------------------------------------------------
    // Voice + keyboard
    // ---------------------------------------------------------------------

    private fun bindVoiceAndKeyboard() {
        voiceButton = findViewById(R.id.btn_voice)
        keyboard = KeyboardController(this, terminalView)

        voice = VoiceInputController(
            context = this,
            onInsert = { text ->
                // Behaves exactly like typed/pasted input into the focused field.
                if (started) manager.insertText(text)
            },
            onError = { msg -> toast(msg) },
            onStateChange = { listening -> updateVoiceButton(listening) }
        )

        voiceButton.setOnClickListener {
            if (voice.isListening) {
                voice.cancel()
            } else {
                ensureMicThenListen()
            }
        }
        findViewById<ImageButton>(R.id.btn_keyboard).setOnClickListener {
            if (started) keyboard.toggle()
        }
    }

    private fun ensureMicThenListen() {
        val granted = checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) voice.start() else micPermission.launch(Manifest.permission.RECORD_AUDIO)
    }

    private fun updateVoiceButton(listening: Boolean) {
        val color = if (listening) getColor(R.color.recording) else getColor(R.color.fg_1)
        voiceButton.imageTintList = ColorStateList.valueOf(color)
    }

    // ---------------------------------------------------------------------
    // Theme
    // ---------------------------------------------------------------------

    private fun applyTheme() {
        if (::viewClient.isInitialized) {
            viewClient.setTextSizeSp(config.theme.fontSizeSp.toFloat())
        }
        // Tint the footer control icons with the chosen accent (visible accent).
        val accent = AccentColor.fromId(config.theme.accent).hex.toInt()
        intArrayOf(R.id.btn_config, R.id.btn_sessions, R.id.btn_keyboard).forEach { id ->
            findViewById<ImageButton>(id)?.imageTintList = ColorStateList.valueOf(accent)
        }
        if (!voice.isListening && ::voiceButton.isInitialized) {
            voiceButton.imageTintList = ColorStateList.valueOf(accent)
        }
        WindowCompat.getInsetsController(window, window.decorView)
            .isAppearanceLightStatusBars = config.theme.lightMode
    }

    // ---------------------------------------------------------------------
    // First-run environment setup (downloads + installs the Termux bootstrap)
    // ---------------------------------------------------------------------

    private fun showSetupScreen() {
        val screen = findViewById<View>(R.id.setup_screen)
        findViewById<View>(R.id.content).visibility = View.GONE
        screen.visibility = View.VISIBLE

        val message = screen.findViewById<TextView>(R.id.setup_message)
        val progress = screen.findViewById<ProgressBar>(R.id.setup_progress)
        val installBtn = screen.findViewById<Button>(R.id.setup_install)
        val retryBtn = screen.findViewById<Button>(R.id.setup_retry)

        message.text = getString(R.string.setup_ready)
        retryBtn.visibility = View.GONE

        val startInstall = {
            installBtn.isEnabled = false
            installBtn.visibility = View.GONE
            retryBtn.visibility = View.GONE
            progress.visibility = View.VISIBLE
            progress.isIndeterminate = true
            runBootstrap(message, progress) {
                installBtn.visibility = View.VISIBLE
                installBtn.isEnabled = true
                retryBtn.visibility = View.VISIBLE
                progress.visibility = View.GONE
            }
        }

        installBtn.setOnClickListener { startInstall() }
        retryBtn.setOnClickListener { startInstall() }
    }

    private fun runBootstrap(
        message: TextView,
        progress: ProgressBar,
        onFailure: () -> Unit
    ) {
        BootstrapInstaller(this).installAsync(object : BootstrapInstaller.Callback {
            override fun onProgress(msg: String, percent: Int) {
                message.text = msg
                if (percent in 0..100) {
                    progress.isIndeterminate = false
                    progress.progress = percent
                } else {
                    progress.isIndeterminate = true
                }
            }

            override fun onComplete() {
                status = TermuxEnvironment.Status.READY
                startTerminal()
            }

            override fun onError(msg: String) {
                message.text = "Setup failed: $msg"
                onFailure()
            }
        })
    }

    // ---------------------------------------------------------------------
    // Lifecycle
    // ---------------------------------------------------------------------

    override fun onPause() {
        super.onPause()
        if (started) manager.persist()
    }

    override fun onDestroy() {
        if (::voice.isInitialized) voice.destroy()
        // Sessions are NOT killed here: background sessions keep running while
        // the app is merely backgrounded. They are torn down by the OS if it
        // reclaims the process (documented limitation).
        super.onDestroy()
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
}
