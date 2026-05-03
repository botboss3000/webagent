package com.webagent.app;

import android.annotation.SuppressLint;
import android.graphics.Bitmap;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.View;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ProgressBar;
import android.widget.RelativeLayout;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends AppCompatActivity {

    private static final String TAG = "webAgent";
    private static final int PORT = 8000;
    private static final String APP_URL = "http://127.0.0.1:" + PORT + "/ui/";
    private static final String HEALTH_URL = "http://127.0.0.1:" + PORT + "/health";

    private WebView webView;
    private RelativeLayout loadingLayout;
    private TextView loadingText;
    private ExecutorService executor = Executors.newSingleThreadExecutor();
    private Handler mainHandler = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webView = findViewById(R.id.webview);
        showLoadingOverlay();
        setupWebView();

        executor.execute(() -> {
            startPython();
            waitForServer();
            mainHandler.post(() -> {
                loadingText.setText("Loading interface...");
                webView.loadUrl(APP_URL);
            });
        });
    }

    private void showLoadingOverlay() {
        loadingLayout = new RelativeLayout(this);
        loadingLayout.setLayoutParams(new RelativeLayout.LayoutParams(
                RelativeLayout.LayoutParams.MATCH_PARENT,
                RelativeLayout.LayoutParams.MATCH_PARENT));
        loadingLayout.setBackgroundColor(0xFF0d0d1a);

        ProgressBar spinner = new ProgressBar(this, null, android.R.attr.progressBarStyleLarge);
        spinner.getIndeterminateDrawable().setTint(0xFF7dcfff);
        RelativeLayout.LayoutParams sp = new RelativeLayout.LayoutParams(
                RelativeLayout.LayoutParams.WRAP_CONTENT,
                RelativeLayout.LayoutParams.WRAP_CONTENT);
        sp.addRule(RelativeLayout.CENTER_IN_PARENT);
        spinner.setLayoutParams(sp);

        loadingText = new TextView(this);
        loadingText.setText("Starting webAgent...");
        loadingText.setTextColor(0xFFc0caf5);
        loadingText.setTextSize(16);
        RelativeLayout.LayoutParams lt = new RelativeLayout.LayoutParams(
                RelativeLayout.LayoutParams.WRAP_CONTENT,
                RelativeLayout.LayoutParams.WRAP_CONTENT);
        lt.addRule(RelativeLayout.CENTER_HORIZONTAL);
        lt.addRule(RelativeLayout.BELOW, spinner.getId());
        lt.setMargins(0, 32, 0, 0);
        loadingText.setLayoutParams(lt);

        loadingLayout.addView(spinner);
        loadingLayout.addView(loadingText);
        addContentView(loadingLayout, new RelativeLayout.LayoutParams(
                RelativeLayout.LayoutParams.MATCH_PARENT,
                RelativeLayout.LayoutParams.MATCH_PARENT));
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void setupWebView() {
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(true);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setAllowFileAccessFromFileURLs(true);
        s.setAllowUniversalAccessFromFileURLs(true);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                loadingLayout.setVisibility(View.GONE);
            }
        });
        webView.setWebChromeClient(new WebChromeClient());
    }

    private void startPython() {
        try {
            mainHandler.post(() -> loadingText.setText("Booting Python..."));
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(this));
            }
            Python py = Python.getInstance();
            py.getModule("server").callAttr("start_server", PORT);
            Log.i(TAG, "Python server started");
        } catch (Exception e) {
            Log.e(TAG, "Python start failed", e);
            mainHandler.post(() -> {
                loadingText.setText("Error: " + e.getMessage());
                Toast.makeText(this, "Python error: " + e.getMessage(), Toast.LENGTH_LONG).show();
            });
        }
    }

    private void waitForServer() {
        long deadline = System.currentTimeMillis() + 15000;
        while (System.currentTimeMillis() < deadline) {
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(HEALTH_URL).openConnection();
                c.setConnectTimeout(1000);
                c.setReadTimeout(1000);
                if (c.getResponseCode() == 200) {
                    Log.i(TAG, "Server ready");
                    return;
                }
                c.disconnect();
            } catch (IOException ignored) {}
            try { Thread.sleep(500); } catch (InterruptedException e) { return; }
        }
        mainHandler.post(() -> {
            loadingText.setText("Server failed to start");
            Toast.makeText(this, "Server timeout", Toast.LENGTH_LONG).show();
        });
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        try {
            if (Python.isStarted())
                Python.getInstance().getModule("server").callAttr("stop_server");
        } catch (Exception e) {
            Log.e(TAG, "Stop error", e);
        }
        executor.shutdownNow();
    }
}
