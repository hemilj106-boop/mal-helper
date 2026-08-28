package com.himel.hanimebridge

import android.app.Activity
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONObject
import java.net.URL
import javax.net.ssl.HttpsURLConnection

class MainActivity : Activity() {

    companion object {
        private const val BRIDGE_URL = "https://himel106j-hanime-sync.onrender.com/bridge/session"
        private val LOGIN_URLS = listOf(
            "https://hanime1.me/login",
            "https://hanimeone.me/login"
        )
        private val ALLOWED_HOSTS = setOf("hanime1.me", "hanimeone.me")
    }

    private lateinit var apiKeyInput: EditText
    private lateinit var statusText: TextView
    private lateinit var webView: WebView
    private lateinit var sendButton: Button
    private var activeBaseUrl = "https://hanime1.me"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WebView.setWebContentsDebuggingEnabled(false)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(16), dp(16), dp(12))
            setBackgroundColor(Color.WHITE)
        }

        root.addView(TextView(this).apply {
            text = "Hanime Bridge"
            textSize = 24f
            setTextColor(Color.BLACK)
        })

        root.addView(TextView(this).apply {
            text = "Login inside Hanime's real page. This app never asks for or sends your Hanime password to our server."
            textSize = 13f
            setTextColor(Color.DKGRAY)
            setPadding(0, dp(6), 0, dp(12))
        })

        apiKeyInput = EditText(this).apply {
            hint = "Helper API key"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            isSingleLine = true
        }
        root.addView(apiKeyInput, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT
        ))

        val openButton = Button(this).apply { text = "Open Hanime Login" }
        root.addView(openButton)

        val alternateButton = Button(this).apply { text = "Try Alternate Hanime Login" }
        root.addView(alternateButton)

        sendButton = Button(this).apply {
            text = "Send Logged-in Session to Helper"
            isEnabled = false
        }
        root.addView(sendButton)

        statusText = TextView(this).apply {
            text = "1) Enter Helper API key.  2) Open login.  3) Sign in normally."
            textSize = 13f
            setTextColor(Color.DKGRAY)
            setPadding(0, dp(8), 0, dp(8))
        }
        root.addView(statusText)

        webView = WebView(this)
        root.addView(webView, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            0,
            1f
        ))

        configureWebView()

        openButton.setOnClickListener { startLogin(0) }
        alternateButton.setOnClickListener { startLogin(1) }
        sendButton.setOnClickListener { sendSession() }

        setContentView(root)
    }

    private fun configureWebView() {
        val cookieManager = CookieManager.getInstance()
        cookieManager.setAcceptCookie(true)
        cookieManager.setAcceptThirdPartyCookies(webView, true)

        with(webView.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            useWideViewPort = true
            loadWithOverviewMode = true
            setSupportZoom(true)
            builtInZoomControls = true
            displayZoomControls = false
            allowFileAccess = false
            allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
            userAgentString = "Mozilla/5.0 (Linux; Android 13; M2103K19G) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36"
        }

        webView.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                if (!url.isNullOrBlank()) inspectSession(url)
            }

            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?
            ) {
                super.onReceivedError(view, request, error)
                if (request?.isForMainFrame == true) {
                    status("Page error: ${error?.description ?: "unknown"}. Try the alternate login button.", true)
                }
            }
        }
    }

    private fun startLogin(index: Int) {
        if (apiKeyInput.text.toString().trim().isEmpty()) {
            status("Enter the Helper API key first. Do not enter your Hanime password here.", true)
            return
        }
        val url = LOGIN_URLS[index.coerceIn(0, LOGIN_URLS.lastIndex)]
        activeBaseUrl = url.substringBefore("/login")
        sendButton.isEnabled = false
        status("Loading Hanime login… Sign in on the website below.")
        webView.visibility = View.VISIBLE
        webView.loadUrl(url)
    }

    private fun inspectSession(url: String) {
        val uri = runCatching { Uri.parse(url) }.getOrNull() ?: return
        val host = uri.host?.lowercase() ?: return
        if (host !in ALLOWED_HOSTS) return

        activeBaseUrl = "https://$host"
        val cookies = CookieManager.getInstance().getCookie(activeBaseUrl)
            ?: CookieManager.getInstance().getCookie(url)
            ?: return

        if (looksAuthenticated(cookies)) {
            sendButton.isEnabled = true
            status("Login session detected ✅  Tap “Send Logged-in Session to Helper”.")
        } else if (!url.contains("/login", ignoreCase = true)) {
            status("Page loaded, but I have not detected a login session yet. If you are logged in, open your profile/account page and wait a moment.")
        }
    }

    private fun looksAuthenticated(cookieHeader: String): Boolean {
        return cookieHeader.split(';').any { part ->
            val name = part.substringBefore('=').trim().lowercase()
            name.contains("session") || name == "laravel_session"
        }
    }

    private fun sendSession() {
        val helperKey = apiKeyInput.text.toString().trim()
        if (helperKey.isEmpty()) {
            status("Helper API key is missing.", true)
            return
        }

        val cookies = CookieManager.getInstance().getCookie(activeBaseUrl)
        if (cookies.isNullOrBlank() || !looksAuthenticated(cookies)) {
            sendButton.isEnabled = false
            status("No logged-in Hanime session found yet. Finish signing in first.", true)
            return
        }

        sendButton.isEnabled = false
        status("Sending the session securely to your private helper…")

        val userAgent = webView.settings.userAgentString ?: ""
        Thread {
            try {
                val connection = (URL(BRIDGE_URL).openConnection() as HttpsURLConnection).apply {
                    requestMethod = "POST"
                    connectTimeout = 20000
                    readTimeout = 30000
                    doOutput = true
                    setRequestProperty("Content-Type", "application/json; charset=utf-8")
                    setRequestProperty("Accept", "application/json")
                    setRequestProperty("X-API-Key", helperKey)
                }

                val payload = JSONObject()
                    .put("base_url", activeBaseUrl)
                    .put("cookie", cookies)
                    .put("user_agent", userAgent)
                    .toString()

                connection.outputStream.use { out ->
                    out.write(payload.toByteArray(Charsets.UTF_8))
                }

                val code = connection.responseCode
                val responseText = runCatching {
                    val stream = if (code in 200..299) connection.inputStream else connection.errorStream
                    stream?.bufferedReader()?.use { it.readText() }.orEmpty()
                }.getOrDefault("")
                connection.disconnect()

                runOnUiThread {
                    if (code in 200..299) {
                        status("Connected ✅ Your Hanime password never left Hanime. You can close this app.")
                    } else {
                        sendButton.isEnabled = true
                        val shortMessage = responseText.take(220).ifBlank { "HTTP $code" }
                        status("Helper rejected the session: $shortMessage", true)
                    }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    sendButton.isEnabled = true
                    status("Could not reach the helper: ${e.javaClass.simpleName}. Try again.", true)
                }
            }
        }.start()
    }

    private fun status(message: String, error: Boolean = false) {
        statusText.text = message
        statusText.setTextColor(if (error) Color.rgb(180, 20, 20) else Color.DKGRAY)
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (::webView.isInitialized && webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }

    override fun onDestroy() {
        if (::webView.isInitialized) {
            webView.stopLoading()
            webView.webViewClient = null
            webView.destroy()
        }
        CookieManager.getInstance().flush()
        super.onDestroy()
    }
}
