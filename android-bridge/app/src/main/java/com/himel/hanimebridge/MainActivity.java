package com.himel.hanimebridge;

import android.app.Activity;
import android.os.Bundle;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.graphics.Color;
import android.text.InputType;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static final String HANIME_ORIGIN = "https://hanime.tv";
    private static final String HELPER_ENDPOINT = "https://himel106j-hanime-sync.onrender.com/bridge/session";
    private WebView webView;
    private EditText apiKeyInput;
    private TextView status;
    private Button syncButton;
    private final ExecutorService executor = Executors.newSingleThreadExecutor();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(16, 12, 16, 12);
        root.setBackgroundColor(Color.WHITE);

        TextView heading = new TextView(this);
        heading.setText("Hanime Bridge");
        heading.setTextSize(22);
        heading.setTextColor(Color.BLACK);
        root.addView(heading);

        TextView instructions = new TextView(this);
        instructions.setText("1. Sign in to Hanime below.\n2. Paste your current helper API key from Render.\n3. Tap Sync only when you are signed in.\n\nThis app uses its own embedded browser; it does not read Chrome cookies.");
        instructions.setTextColor(Color.DKGRAY);
        instructions.setPadding(0, 8, 0, 8);
        root.addView(instructions);

        apiKeyInput = new EditText(this);
        apiKeyInput.setSingleLine(true);
        apiKeyInput.setHint("Render HELPER_API_KEY");
        apiKeyInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        root.addView(apiKeyInput);

        syncButton = new Button(this);
        syncButton.setText("Sync signed-in Hanime account");
        root.addView(syncButton);

        status = new TextView(this);
        status.setText("Not connected. Sign in to Hanime first.");
        status.setTextColor(Color.DKGRAY);
        status.setPadding(0, 6, 0, 8);
        root.addView(status);

        webView = new WebView(this);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setSupportMultipleWindows(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        webView.setWebChromeClient(new WebChromeClient());
        CookieManager.getInstance().setAcceptCookie(true);
        webView.loadUrl(HANIME_ORIGIN + "/login");

        LinearLayout.LayoutParams webParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f);
        root.addView(webView, webParams);
        setContentView(root);

        syncButton.setOnClickListener(v -> syncSession());
    }

    private void syncSession() {
        final String apiKey = apiKeyInput.getText().toString().trim();
        if (apiKey.isEmpty()) {
            status.setText("Enter the helper API key from Render first.");
            return;
        }

        CookieManager manager = CookieManager.getInstance();
        manager.flush();
        final String cookie = manager.getCookie(HANIME_ORIGIN);
        if (cookie == null || cookie.trim().isEmpty() ||
                !(cookie.toLowerCase().contains("session") || cookie.toLowerCase().contains("laravel_session"))) {
            status.setText("No session cookie detected yet. Make sure you completed Hanime sign-in in this app.");
            return;
        }

        syncButton.setEnabled(false);
        status.setText("Verifying your signed-in session securely…");

        executor.execute(() -> {
            HttpURLConnection connection = null;
            try {
                URL url = new URL(HELPER_ENDPOINT);
                connection = (HttpURLConnection) url.openConnection();
                connection.setRequestMethod("POST");
                connection.setConnectTimeout(25000);
                connection.setReadTimeout(35000);
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                connection.setRequestProperty("X-API-Key", apiKey);

                JSONObject body = new JSONObject();
                body.put("base_url", HANIME_ORIGIN);
                body.put("cookie", cookie);
                body.put("user_agent", webView.getSettings().getUserAgentString());

                byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
                try (OutputStream out = connection.getOutputStream()) {
                    out.write(bytes);
                }

                int code = connection.getResponseCode();
                InputStream stream = (code >= 200 && code < 300)
                        ? connection.getInputStream() : connection.getErrorStream();
                StringBuilder response = new StringBuilder();
                if (stream != null) {
                    try (BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8))) {
                        String line;
                        while ((line = reader.readLine()) != null) response.append(line);
                    }
                }

                String message;
                if (code >= 200 && code < 300) {
                    JSONObject result = new JSONObject(response.toString());
                    message = "Connected and verified. " +
                            (result.optString("user_name", "").isEmpty() ? "" : "Account: " + result.optString("user_name"));
                } else {
                    String detail = "The helper returned HTTP " + code + ".";
                    try {
                        JSONObject error = new JSONObject(response.toString());
                        Object d = error.opt("detail");
                        if (d != null) detail = String.valueOf(d);
                    } catch (Exception ignored) { }
                    message = "Not connected: " + detail;
                }

                final String finalMessage = message;
                runOnUiThread(() -> status.setText(finalMessage));
            } catch (Exception e) {
                runOnUiThread(() -> status.setText("Could not reach the helper: " + e.getClass().getSimpleName()));
            } finally {
                if (connection != null) connection.disconnect();
                runOnUiThread(() -> syncButton.setEnabled(true));
            }
        });
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        executor.shutdownNow();
        if (webView != null) webView.destroy();
    }
}
