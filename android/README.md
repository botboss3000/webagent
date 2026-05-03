# webAgent Android

APK wrapper for [webAgent](https://github.com/YOUR_USER/webAgent).

Embeds Python FastAPI backend + web UI as a native Android app.

## Download APK

**Actions tab** → latest build → download **webAgent-APK** artifact.

Side-load on phone (enable "Install from unknown sources").

## How It Works

1. Tap icon → Python boots inside APK (Chaquopy)
2. uvicorn serves webAgent on `127.0.0.1:8000`
3. WebView loads the UI
4. All logic runs locally (AI needs internet for OpenRouter)

## Requirements

- Android 7.0+ (API 24)
- Internet for AI calls
- ~80MB storage
