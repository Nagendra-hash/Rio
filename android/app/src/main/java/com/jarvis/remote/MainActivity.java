package com.jarvis.remote;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.ViewGroup;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.Toast;

import java.io.InputStream;
import java.security.MessageDigest;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.util.Locale;

import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;

/**
 * JARVIS Remote — a minimal WebView shell around the JARVIS dashboard PWA.
 *
 * Design goals:
 *  - No androidx / external deps: tiny build, works on a low-RAM box.
 *  - Mic via onPermissionRequest (voice chat over /ws/phone-audio).
 *  - File upload via onShowFileChooser (dashboard upload endpoint).
 *  - Pin-trusts the make_certs.sh CA bundled at res/raw/jarvis_ca.pem and
 *    remembers the first leaf cert it accepted (TOFU), so an unknown signer
 *    is flagged instead of silently trusted.
 *  - Floating URL button to change the dashboard URL (LAN IPs move).
 */
public class MainActivity extends Activity {

    static final String PREFS = "jarvis_remote";
    static final String KEY_URL = "dashboard_url";
    private static final String KEY_TOFU = "tofu_fp";
    /** Paired device token — scraped from localStorage after login, used by BackgroundLinkService. */
    static final String KEY_TOKEN = "device_token";
    /** User wants the link to survive reboots. */
    static final String KEY_AUTOLINK = "autolink";

    /** Extras for token/URL restore (adb, backup-QR flow): am start --es device_token … */
    static final String EXTRA_DEVICE_TOKEN = "device_token";
    static final String EXTRA_DASHBOARD_URL = "dashboard_url";
    private static boolean sPendingInject = false;

    private static final int REQ_MIC = 1;
    private static final int REQ_FILE = 2;
    private static final int REQ_NOTIF = 3;

    private WebView web;
    private ValueCallback<Uri[]> fileCallback;

    /** Trust anchors from the bundled JARVIS local CA (null if not bundled). */
    private X509TrustManager trustManager;

    @SuppressLint({"SetJavaScriptEnabled", "MissingPermission"})
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // mic up-front so voice works on first use
        requestPermissions(new String[]{android.Manifest.permission.RECORD_AUDIO}, REQ_MIC);

        buildTrustManager();

        // Token/URL restore via intent extras (adb `am start --es device_token …`):
        // store, then inject into the page's localStorage on first resume so the
        // dashboard's own silent device-login takes over.
        Intent in = getIntent();
        if (in != null) {
            String tok = in.getStringExtra(EXTRA_DEVICE_TOKEN);
            String url = in.getStringExtra(EXTRA_DASHBOARD_URL);
            SharedPreferences p = getSharedPreferences(PREFS, MODE_PRIVATE);
            if (tok != null && !tok.trim().isEmpty()) {
                p.edit().putString(KEY_TOKEN, tok.trim()).apply();
                sPendingInject = true;
            }
            if (url != null && !url.trim().isEmpty()) {
                p.edit().putString(KEY_URL, url.trim()).apply();
            }
        }

        String url = getSharedPreferences(PREFS, MODE_PRIVATE)
                .getString(KEY_URL, getString(R.string.dashboard_url));

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);

        web.setWebViewClient(new PinningWebViewClient());
        web.setWebChromeClient(new ChromeClient());

        FrameLayout root = new FrameLayout(this);
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        Button urlBtn = new Button(this);
        urlBtn.setText("URL");
        urlBtn.setAlpha(0.35f);
        urlBtn.setTextSize(11f);
        FrameLayout.LayoutParams bp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.TOP | Gravity.END);
        bp.setMargins(0, dp(8), dp(8), 0);
        root.addView(urlBtn, bp);
        urlBtn.setOnClickListener(v -> promptForUrl());

        Button linkBtn = new Button(this);
        linkBtn.setText(BackgroundLinkService.isRunning() ? "Link ✓" : "Link");
        linkBtn.setAlpha(0.35f);
        linkBtn.setTextSize(11f);
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.TOP | Gravity.START);
        lp.setMargins(dp(8), dp(8), 0, 0);
        root.addView(linkBtn, lp);
        linkBtn.setOnClickListener(v -> {
            toggleLink();
            linkBtn.setText(BackgroundLinkService.isRunning() ? "Link ✓" : "Link");
        });

        setContentView(root);

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(url);
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    /** Start/stop the always-on background link (foreground service). */
    private void toggleLink() {
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        if (BackgroundLinkService.isRunning()) {
            startService(new Intent(this, BackgroundLinkService.class)
                    .setAction(BackgroundLinkService.ACTION_STOP));
            prefs.edit().putBoolean(KEY_AUTOLINK, false).apply();
            Toast.makeText(this, "Background link stopped", Toast.LENGTH_SHORT).show();
            return;
        }
        prefs.edit().putBoolean(KEY_AUTOLINK, true).apply();   // also restart after boot
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(
                "android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, REQ_NOTIF);
            Toast.makeText(this, "Allow notifications, then tap Link again",
                    Toast.LENGTH_LONG).show();
            return;
        }
        requestIgnoreBatteryOptimizations();
        if (prefs.getString(KEY_TOKEN, "").isEmpty()) {
            Toast.makeText(this, "Log in once — the app grabs the pairing token, then tap Link",
                    Toast.LENGTH_LONG).show();
        }
        startForegroundService(new Intent(this, BackgroundLinkService.class)
                .setAction(BackgroundLinkService.ACTION_START));
        Toast.makeText(this, "Background link starting…", Toast.LENGTH_SHORT).show();
    }

    /**
     * Ask the OS to never throttle this app's network/timers in the background
     * (Doze / App Standby would otherwise kill the link minutes after minimize).
     * One-time system dialog; safe to call repeatedly.
     */
    private void requestIgnoreBatteryOptimizations() {
        android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
        if (pm == null || pm.isIgnoringBatteryOptimizations(getPackageName())) return;
        try {
            startActivity(new Intent(
                    android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName())));
        } catch (Exception e) {
            // OEM without the dialog — Link still works, just with more reconnects
        }
    }

    static void toast(Context c, String msg) {
        Toast.makeText(c, msg, Toast.LENGTH_LONG).show();
    }

    private void promptForUrl() {
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        final EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setText(prefs.getString(KEY_URL, getString(R.string.dashboard_url)));
        new AlertDialog.Builder(this)
                .setTitle("Dashboard URL")
                .setMessage("Example: https://192.168.29.172:8000/")
                .setView(input)
                .setPositiveButton("Connect", (d, w) -> {
                    String u = input.getText().toString().trim();
                    if (!u.isEmpty()) {
                        if (!u.startsWith("http")) u = "https://" + u;
                        prefs.edit().putString(KEY_URL, u).apply();
                        web.loadUrl(u);
                    }
                })
                .setNegativeButton("Cancel", null)
                .show();
    }

    // ── cert trust ──────────────────────────────────────────────────────────

    private void buildTrustManager() {
        try {
            CertificateFactory cf = CertificateFactory.getInstance("X.509");
            X509Certificate ca;
            try (InputStream is = getResources().openRawResource(R.raw.jarvis_ca)) {
                ca = (X509Certificate) cf.generateCertificate(is);
            } catch (Exception missing) {
                trustManager = null; // CA not bundled — TOFU pin alone protects us
                return;
            }

            java.security.KeyStore ks = java.security.KeyStore.getInstance(
                    java.security.KeyStore.getDefaultType());
            ks.load(null, null);
            ks.setCertificateEntry("jarvis-ca", ca);

            TrustManagerFactory tmf = TrustManagerFactory.getInstance(
                    TrustManagerFactory.getDefaultAlgorithm());
            tmf.init(ks);
            trustManager = (X509TrustManager) tmf.getTrustManagers()[0];
        } catch (Exception e) {
            trustManager = null;
        }
    }

    private static String sha256(Certificate cert) {
        try {
            byte[] d = MessageDigest.getInstance("SHA-256").digest(cert.getEncoded());
            StringBuilder sb = new StringBuilder();
            for (byte b : d) sb.append(String.format(Locale.US, "%02x", b));
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }

    private class PinningWebViewClient extends WebViewClient {
        @Override
        public void onPageFinished(WebView view, String url) {
            // Token restore: inject into localStorage from THIS page's context
            // (correct origin — onResume runs before the https document exists,
            // which silently stores nothing). Then one reload lets the page's
            // own _deviceLogin flow take over.
            if (sPendingInject) {
                sPendingInject = false;
                String tok = getSharedPreferences(PREFS, MODE_PRIVATE)
                        .getString(KEY_TOKEN, "");
                if (!tok.isEmpty()) {
                    view.evaluateJavascript(
                            "try{localStorage.setItem('jarvis_device_token','" + tok
                                    + "');'ok'}catch(e){'err'}",
                            v -> view.postDelayed(view::reload, 300));
                    return;   // scrape on the next finish, post-reload
                }
            }
            // Grab the paired device token from localStorage (login.html stores
            // it after QR/auto-login). The background service uses it for /ws.
            view.evaluateJavascript(
                    "(function(){try{return localStorage.getItem('jarvis_device_token')||''}"
                            + "catch(e){return ''}})()",
                    value -> {
                        if (value == null) return;
                        String v = value.replaceAll("^\"|\"$", "");
                        if (!v.isEmpty()) {
                            getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                                    .putString(KEY_TOKEN, v).apply();
                        }
                    });
        }

        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler,
                                       android.net.http.SslError error) {
            X509Certificate leaf = error.getCertificate() != null
                    ? error.getCertificate().getX509Certificate() : null;
            if (leaf != null && validate(leaf)) {
                handler.proceed();
                return;
            }
            new AlertDialog.Builder(MainActivity.this)
                    .setTitle("Untrusted certificate")
                    .setMessage("This server's certificate is not signed by the JARVIS "
                            + "local CA known to this app. If you re-ran make_certs.sh or "
                            + "the LAN IP changed, verify the dashboard first, then trust.")
                    .setPositiveButton("Trust this cert (TOFU)", (d, w) -> {
                        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                                .putString(KEY_TOFU, sha256(leaf)).apply();
                        handler.proceed();
                    })
                    .setNegativeButton("Block", (d, w) -> handler.cancel())
                    .setOnCancelListener(d -> handler.cancel())
                    .show();
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
            Uri u = req.getUrl();
            String scheme = u.getScheme() == null ? "" : u.getScheme();
            // keep the shell inside the dashboard; push external links to the browser
            if (scheme.equals("http") || scheme.equals("https")) return false;
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, u));
            } catch (Exception ignored) { }
            return true;
        }
    }

    /** True if the leaf matches the saved TOFU fingerprint or chains to the CA. */
    private boolean validate(X509Certificate leaf) {
        try {
            String saved = getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_TOFU, null);
            if (saved != null && saved.equals(sha256(leaf))) return true;
            if (trustManager != null) {
                trustManager.checkServerTrusted(new X509Certificate[]{leaf}, "RSA");
                return true;
            }
        } catch (Exception ignored) { }
        return false;
    }

    // ── chrome client: mic + file upload ────────────────────────────────────

    private class ChromeClient extends WebChromeClient {
        @Override
        public void onPermissionRequest(final PermissionRequest request) {
            runOnUiThread(() -> {
                for (String r : request.getResources()) {
                    if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)
                            || PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) {
                        request.grant(request.getResources());
                        return;
                    }
                }
                request.deny();
            });
        }

        @Override
        public boolean onShowFileChooser(WebView wv, ValueCallback<Uri[]> cb,
                                         FileChooserParams params) {
            if (fileCallback != null) fileCallback.onReceiveValue(null);
            fileCallback = cb;
            try {
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("*/*");
                startActivityForResult(
                        Intent.createChooser(i, "Send to JARVIS"), REQ_FILE);
            } catch (Exception e) {
                fileCallback = null;
                Toast.makeText(MainActivity.this, "No file picker available",
                        Toast.LENGTH_SHORT).show();
                return false;
            }
            return true;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILE && fileCallback != null) {
            Uri[] out = data != null && data.getData() != null
                    ? new Uri[]{data.getData()} : null;
            fileCallback.onReceiveValue(out);
            fileCallback = null;
        } else {
            super.onActivityResult(requestCode, resultCode, data);
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (code == REQ_NOTIF) {
            Toast.makeText(this, "Now tap Link to start the background connection",
                    Toast.LENGTH_LONG).show();
            return;
        }
        if (code == REQ_MIC && (results.length == 0
                || results[0] != PackageManager.PERMISSION_GRANTED)) {
            Toast.makeText(this, "Mic denied — voice input disabled",
                    Toast.LENGTH_LONG).show();
        }
    }

    // ── navigation ──────────────────────────────────────────────────────────

    @Override
    protected void onPause() {
        if (web != null) web.onPause();   // pause JS timers with the activity
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) {
            web.onResume();
            // WebView never delivers visibilitychange on resume, so the page's
            // own reconnect-on-foreground handler can't fire. Replicate it:
            // dead socket → immediate reconnect; live socket → 3s pong probe,
            // force-closed on silence (page then reconnects itself).
            web.evaluateJavascript(
                    "try{"
                    + "if(typeof _wsConnect==='function'){"
                    + "if(!ws||ws.readyState>1){_wsTries=0;_wsConnect();}"
                    + "else{var s=ws;"
                    + "var pr=setTimeout(function(){if(ws===s&&s.readyState===1){try{s.close();}catch(_){}}},3000);"
                    + "s.addEventListener('message',function(){clearTimeout(pr);},{once:true});"
                    + "try{_wsSend({type:'ping'});}catch(_){}}"
                    + "}else{document.dispatchEvent(new Event('visibilitychange'));}"
                    + "}catch(e){}",
                    null);
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.loadUrl("about:blank");
            ((ViewGroup) web.getParent()).removeView(web);
            web.destroy();
        }
        super.onDestroy();
    }
}
