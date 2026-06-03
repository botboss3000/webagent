package com.webagent.terminallauncher.ui

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

/**
 * Wraps Android speech recognition for the mic button.
 *
 * Contract (per product brief):
 *  - Recognized speech is handed to [onInsert], which writes it into the ACTIVE
 *    terminal session exactly like typed/pasted input.
 *  - We NEVER auto-submit and NEVER append a newline. The user edits/submits.
 *  - Because the text goes into the PTY input stream, it lands wherever the
 *    foreground program's cursor is -- a shell prompt OR a focused field inside
 *    a TUI app. That is precisely the requested behaviour.
 *
 * Partial results: Android's partial hypotheses are frequently revised, so
 * inserting them live would duplicate/garble terminal input. We therefore insert
 * only the FINAL result (documented in the README). [onPartial] is still surfaced
 * for an on-screen "listening..." hint, but is not written to the terminal.
 *
 * All failure modes (no recognizer, permission denied, cancelled, nothing heard,
 * offline model missing) are reported through [onError] with a friendly message.
 */
class VoiceInputController(
    private val context: Context,
    private val onInsert: (String) -> Unit,
    private val onError: (String) -> Unit,
    private val onStateChange: (listening: Boolean) -> Unit,
    private val onPartial: (String) -> Unit = {}
) {
    private var recognizer: SpeechRecognizer? = null
    private var listening = false

    val isListening: Boolean get() = listening

    fun isAvailable(): Boolean = SpeechRecognizer.isRecognitionAvailable(context)

    fun start() {
        if (listening) return
        if (!isAvailable()) {
            onError("Speech recognition isn't available on this device. Install or enable a speech service (e.g. Google) and try again.")
            return
        }

        val rec = SpeechRecognizer.createSpeechRecognizer(context)
        recognizer = rec
        rec.setRecognitionListener(listener)

        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, false)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
        }
        runCatching { rec.startListening(intent) }
            .onFailure { onError("Couldn't start listening: ${it.message ?: "unknown error"}") }
    }

    fun cancel() {
        runCatching { recognizer?.cancel() }
        teardown()
    }

    fun destroy() {
        runCatching { recognizer?.destroy() }
        recognizer = null
        listening = false
    }

    private fun teardown() {
        listening = false
        onStateChange(false)
        runCatching { recognizer?.destroy() }
        recognizer = null
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {
            listening = true
            onStateChange(true)
        }

        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}

        override fun onPartialResults(partialResults: Bundle?) {
            val text = firstHypothesis(partialResults)
            if (!text.isNullOrEmpty()) onPartial(text)
        }

        override fun onResults(results: Bundle?) {
            val text = firstHypothesis(results)
            if (text.isNullOrBlank()) {
                onError("No speech recognized. Try again.")
            } else {
                // Insert as-is; no trailing newline, no auto-submit.
                onInsert(text)
            }
            teardown()
        }

        override fun onEvent(eventType: Int, params: Bundle?) {}

        override fun onError(error: Int) {
            onError(friendlyError(error))
            teardown()
        }
    }

    private fun firstHypothesis(bundle: Bundle?): String? =
        bundle?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()

    private fun friendlyError(code: Int): String = when (code) {
        SpeechRecognizer.ERROR_AUDIO -> "Microphone error. Check the mic and try again."
        SpeechRecognizer.ERROR_CLIENT -> "Speech was cancelled."
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS ->
            "Microphone permission is required for voice input."
        SpeechRecognizer.ERROR_NETWORK,
        SpeechRecognizer.ERROR_NETWORK_TIMEOUT ->
            "Network needed for speech recognition and it's unavailable. An offline language pack would let this work offline."
        SpeechRecognizer.ERROR_NO_MATCH -> "Didn't catch that. Try again."
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "Speech recognizer is busy. Try again in a moment."
        SpeechRecognizer.ERROR_SERVER -> "Speech server error. Try again."
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "No speech detected."
        else -> "Voice input failed (code $code)."
    }
}
