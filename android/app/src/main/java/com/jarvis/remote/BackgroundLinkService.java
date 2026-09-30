package com.jarvis.remote;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.util.Base64;
import android.util.Log;
import org.json.JSONObject;

import java.io.InputStream;
import java.net.URI;
import java.security.KeyStore;
import java.security.SecureRandom;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.util.Locale;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;

/**
 * BackgroundLinkService — the "always on" piece of JARVIS Remote.
 *
 * Holds a WebSocket to the dashboard (/ws?token=…&role=phone) for as long as
 * the user keeps it enabled, surviving app swipe-away as a foreground service
 * with an ongoing notification (quick actions: Stop, Ping JARVIS).
 *
 * Link protocol matches dashboard/server.py /ws:
 *   client → {"type":"hello","role":"phone"}   announce
 *   client → {"type":"ping"}                   liveness probe
 *   server → {"type":"pong"}
 *   client → {"type":"command","text":"…"}     quick actions
 * Server pings: the web app relies on pong timeouts; the service does the same
 * (missed pongs back off and reconnect).
 */
public class BackgroundLinkService extends Service {

    static final String CHANNEL_ID = "jarvis_link";
    static final int NOTIF_ID = 42;

    static final String ACTION_START = "com.jarvis.remote.START_LINK";
    static final String ACTION_STOP  = "com.jarvis.remote.STOP_LINK";
    static final String ACTION_PING  = "com.jarvis.remote.PING";

    private static final long[] BACKOFF_MS = {2_000, 5_000, 10_000, 30_000, 60_000};
    private static final long PONG_TIMEOUT_MS = 12_000;
    private static final long PING_EVERY_MS = 25_000;

    private static volatile boolean sRunning = false;

    private HandlerThread thread;
    private Handler handler;
    private WebSocket ws;               // android.WebSocket wrapper, below
    private int attempt;
    private String base;                // https://host:port
    private String token;
    private boolean ponged;

    /** True while the user wants the background link (survives restarts). */
    public static boolean isRunning() { return sRunning; }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            sRunning = false;
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
            return START_NOT_STICKY;
        }
        if (ACTION_PING.equals(action)) {
            if (sRunning) sendJson("{\"type\":\"ping\"}");
            return START_STICKY;
        }
        sRunning = true;

        SharedPreferences p = getSharedPreferences(MainActivity.PREFS, MODE_PRIVATE);
        base  = p.getString(MainActivity.KEY_URL, "");
        // MainActivity falls back to the baked-in default when the pref is
        // unset (it's only persisted by the URL dialog) — the service must too.
        if (base == null || base.trim().isEmpty()) {
            base = getString(R.string.dashboard_url);
        }
        token = p.getString(MainActivity.KEY_TOKEN, "");
        Log.i("JARVIS", "link onStart action=" + action + " url=" + base
                + " tokenSet=" + !token.isEmpty());
        if (token.isEmpty()) {
            MainActivity.toast(this, "Open JARVIS once to finish pairing, then retry.");
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
            return START_NOT_STICKY;
        }

        startForeground(NOTIF_ID, buildNotification("connecting…"));
        Log.i("JARVIS", "link foreground started");
        if (thread == null) {
            thread = new HandlerThread("jarvis-link");
            thread.start();
            handler = new Handler(thread.getLooper());
        }
        handler.removeCallbacksAndMessages(null);
        attempt = 0;
        // Network must never run on the main thread — post the first attempt
        // onto the worker looper like every retry.
        handler.post(this::connectLoop);
        // If the system kills us, it restarts the service (user asked for always-on)
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        sRunning = false;
        if (handler != null) handler.removeCallbacksAndMessages(null);
        closeSocket();
        if (thread != null) thread.quitSafely();
        super.onDestroy();
    }

    // ── notification ────────────────────────────────────────────────────────

    private Notification buildNotification(String state) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm.getNotificationChannel(CHANNEL_ID) == null) {
            NotificationChannel ch = new NotificationChannel(CHANNEL_ID,
                    "JARVIS link", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("Keeps the phone connected to JARVIS in the background");
            nm.createNotificationChannel(ch);
        }

        PendingIntent content = PendingIntent.getActivity(this, 0,
                new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);

        PendingIntent stop = PendingIntent.getService(this, 1,
                new Intent(this, BackgroundLinkService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);

        PendingIntent ping = PendingIntent.getService(this, 2,
                new Intent(this, BackgroundLinkService.class).setAction(ACTION_PING),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);

        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        return b.setSmallIcon(android.R.drawable.stat_notify_sync_noanim)
                .setContentTitle("JARVIS link active")
                .setContentText(state + " · " + hostOf(base))
                .setContentIntent(content)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .addAction(0, "Ping JARVIS", ping)
                .addAction(0, "Stop", stop)
                .build();
    }

    private static String hostOf(String url) {
        try { return new URI(url).getHost(); } catch (Exception e) { return url; }
    }

    // ── connection loop ─────────────────────────────────────────────────────

    private void connectLoop() {
        if (!sRunning) return;
        closeSocket();
        try {
            ws = openSocket();
        } catch (Exception e) {
            Log.i("JARVIS", "connect error: " + e);
            scheduleRetry();
        }
    }

    /** Exchange the paired device token for a fresh session token. */
    private String deviceLogin() throws Exception {
        java.net.URL u = new URI(base).resolve("/api/device-login").toURL();
        HttpsURLConnection c = (HttpsURLConnection) u.openConnection();
        SSLContext ctx = buildSsl();              // trust the bundled JARVIS CA
        if (ctx != null) c.setSSLSocketFactory(ctx.getSocketFactory());
        c.setRequestMethod("POST");
        c.setRequestProperty("Content-Type", "application/json");
        c.setDoOutput(true);
        c.setConnectTimeout(8000);
        c.setReadTimeout(8000);
        try (java.io.OutputStream os = c.getOutputStream()) {
            os.write(("{\"device_token\":\"" + token + "\"}").getBytes("UTF-8"));
        }
        int code = c.getResponseCode();
        Log.i("JARVIS", "device-login HTTP " + code);
        String body;
        try (InputStream is = code >= 400 ? c.getErrorStream() : c.getInputStream();
             java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream()) {
            byte[] buf = new byte[4096];
            int n;
            while (is != null && (n = is.read(buf)) != -1) bo.write(buf, 0, n);
            body = bo.toString("UTF-8");
        }
        if (code != 200) throw new Exception("device-login HTTP " + code);
        JSONObject o = new JSONObject(body);
        if (!o.optBoolean("ok") || o.optString("token").isEmpty())
            throw new Exception("device-login rejected (re-pair needed)");
        return o.getString("token");
    }

    /**
     * TLS context anchored on the bundled JARVIS local CA (res/raw/jarvis_ca.pem)
     * — the dashboard's cert is self-signed via make_certs.sh, so the default
     * system anchors would reject it. Falls back to system defaults if the
     * CA resource is missing.
     */
    private SSLContext buildSsl() {
        try {
            CertificateFactory cf = CertificateFactory.getInstance("X.509");
            X509Certificate ca;
            try (InputStream is = getResources().openRawResource(R.raw.jarvis_ca)) {
                ca = (X509Certificate) cf.generateCertificate(is);
            }
            KeyStore ks = KeyStore.getInstance(KeyStore.getDefaultType());
            ks.load(null, null);
            ks.setCertificateEntry("jarvis-ca", ca);
            TrustManagerFactory tmf = TrustManagerFactory.getInstance(
                    TrustManagerFactory.getDefaultAlgorithm());
            tmf.init(ks);
            SSLContext ctx = SSLContext.getInstance("TLS");
            ctx.init(null, tmf.getTrustManagers(), new SecureRandom());
            return ctx;
        } catch (Exception e) {
            return null;   // no bundled CA — rely on system anchors (will fail on self-signed)
        }
    }

    private WebSocket openSocket() throws Exception {
        URI https = new URI(base);
        boolean tls = "https".equalsIgnoreCase(https.getScheme());
        int port = https.getPort() > 0 ? https.getPort() : (tls ? 443 : 80);
        String wsScheme = tls ? "wss" : "ws";

        // /ws only accepts session tokens minted THIS boot. Exchange the
        // paired device token for a fresh session first — exactly what the
        // web app's _silentRepair does after a JARVIS restart.
        String session = deviceLogin();
        URI wsUri = new URI(wsScheme, null, https.getHost(), port, "/ws",
                "token=" + session + "&role=phone", null);

        SSLContext ssl = null;
        if (tls) {
            ssl = buildSsl();
        }

        WebSocket socket = new WebSocket(wsUri, ssl);
        socket.listener = new WebSocket.Listener() {
            @Override public void onOpen() {
                Log.i("JARVIS", "ws OPEN");
                attempt = 0;
                sendJson("{\"type\":\"hello\",\"role\":\"phone\"}");
                schedulePing();
                updateNotif("connected to " + hostOf(base));
            }
            @Override public void onMessage(String msg) {
                try {
                    JSONObject o = new JSONObject(msg);
                    if ("pong".equals(o.optString("type"))) {
                        ponged = true;
                    }
                } catch (Exception ignored) { }
            }
            @Override public void onClosed() { scheduleRetry(); }
            @Override public void onFailure(String reason) {
                Log.i("JARVIS", "ws FAILURE: " + reason);
                scheduleRetry();
            }
        };
        socket.connect();
        return socket;
    }

    private void scheduleRetry() {
        closeSocket();
        if (!sRunning) return;
        long delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
        attempt++;
        updateNotif("reconnecting in " + delay / 1000 + "s…");
        handler.postDelayed(this::connectLoop, delay);
    }

    /** Used by quick actions / future integrations to fire a text command. */
    void sendQuickCommand(String text) { quickCommand(text); }

    private void schedulePing() {
        handler.postDelayed(() -> {
            if (!sRunning || ws == null || !ws.isOpen()) return;
            ponged = false;
            sendJson("{\"type\":\"ping\"}");
            handler.postDelayed(() -> {
                if (!sRunning) return;
                if (ponged) schedulePing();      // healthy — keep the cycle going
                else scheduleRetry();            // missed pong — reconnect
            }, PONG_TIMEOUT_MS);
        }, PING_EVERY_MS);
    }

    private void sendJson(String s) {
        WebSocket w = ws;
        if (w != null && w.isOpen()) w.send(s);
    }

    private void closeSocket() {
        WebSocket w = ws;
        ws = null;
        if (w != null) w.close();
    }

    private void updateNotif(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm.getNotificationChannel(CHANNEL_ID) == null) return; // not started yet
        try {
            nm.notify(NOTIF_ID, buildNotification(text));  // refresh with current state
        } catch (Exception ignored) { }
    }

    private void quickCommand(String text) {
        sendJson("{\"type\":\"command\",\"text\":\"" + text.replace("\"", "'") + "\"}");
    }

    // ── raw WebSocket over android httpstack ────────────────────────────────
    // Hand-rolled RFC6455 client on a plain socket — no okhttp dependency (the
    // whole app is deliberately dependency-free).

    static class WebSocket {
        interface Listener {
            void onOpen();
            void onMessage(String msg);
            void onClosed();
            void onFailure(String reason);
        }

        private final URI uri;
        private final SSLContext ssl;
        private java.net.Socket sock;
        private InputStream in;
        private java.io.OutputStream out;
        private Thread reader;
        volatile Listener listener;
        private volatile boolean open;

        WebSocket(URI uri, SSLContext ssl) { this.uri = uri; this.ssl = ssl; }

        boolean isOpen() { return open; }

        void connect() throws Exception {
            boolean tls = "wss".equalsIgnoreCase(uri.getScheme());
            int port = uri.getPort() > 0 ? uri.getPort() : (tls ? 443 : 80);
            if (tls) {
                javax.net.ssl.SSLSocketFactory f;
                if (ssl != null) {
                    f = ssl.getSocketFactory();
                } else {
                    f = HttpsURLConnection.getDefaultSSLSocketFactory();
                }
                sock = f.createSocket(uri.getHost(), port);
            } else {
                sock = new java.net.Socket(uri.getHost(), port);
            }
            sock.setTcpNoDelay(true);

            String key = Base64.encodeToString(randomKey(), Base64.NO_WRAP);
            String path = uri.getRawPath()
                    + (uri.getRawQuery() != null ? "?" + uri.getRawQuery() : "");
            String req = "GET " + path + " HTTP/1.1\r\n"
                    + "Host: " + uri.getHost() + ":" + port + "\r\n"
                    + "Upgrade: websocket\r\n"
                    + "Connection: Upgrade\r\n"
                    + "Sec-WebSocket-Key: " + key + "\r\n"
                    + "Sec-WebSocket-Version: 13\r\n\r\n";
            out = sock.getOutputStream();
            out.write(req.getBytes("UTF-8"));
            out.flush();

            String resp = readHttpHeaders(in = sock.getInputStream());
            if (!resp.contains(" 101 ")) throw new Exception("handshake failed: " + firstLine(resp));
            open = true;

            reader = new Thread(this::readLoop, "ws-reader");
            reader.setDaemon(true);
            reader.start();
        }

        private static byte[] randomKey() {
            byte[] b = new byte[16];
            new SecureRandom().nextBytes(b);
            return b;
        }

        private static String firstLine(String r) {
            int i = r.indexOf('\n');
            return i < 0 ? r : r.substring(0, i).trim();
        }

        private static String readHttpHeaders(InputStream is) throws Exception {
            StringBuilder sb = new StringBuilder();
            int c;
            while ((c = is.read()) != -1) {
                sb.append((char) c);
                if (sb.length() > 16 * 1024) throw new Exception("header overflow");
                String s = sb.toString();
                if (s.endsWith("\r\n\r\n")) return s;
            }
            throw new Exception("closed during handshake");
        }

        void send(String text) {
            try {
                byte[] payload = text.getBytes("UTF-8");
                boolean masked = true;
                byte[] mask = new byte[4];
                new SecureRandom().nextBytes(mask);
                java.io.ByteArrayOutputStream h = new java.io.ByteArrayOutputStream();
                h.write(0x81); // FIN + text frame
                if (payload.length < 126) {
                    h.write(0x80 | payload.length);
                } else if (payload.length < 65536) {
                    h.write(0x80 | 126);
                    h.write((payload.length >> 8) & 0xFF);
                    h.write(payload.length & 0xFF);
                } else {
                    h.write(0x80 | 127);
                    for (int i = 7; i >= 0; i--) h.write((payload.length >> (8 * i)) & 0xFF);
                }
                h.write(mask);
                for (int i = 0; i < payload.length; i++)
                    h.write(payload[i] ^ mask[i % 4]);
                synchronized (out) { h.writeTo(out); out.flush(); }
            } catch (Exception e) {
                open = false;
                Listener l = listener;
                if (l != null) l.onFailure(e.toString());
            }
        }

        void close() {
            open = false;
            try { sock.close(); } catch (Exception ignored) { }
        }

        private void readLoop() {
            try {
                java.io.ByteArrayOutputStream frag = new java.io.ByteArrayOutputStream();
                while (open) {
                    int b1 = in.read();
                    if (b1 == -1) break;
                    int b2 = in.read();
                    int opcode = b1 & 0x0F;
                    boolean maskedSrv = (b2 & 0x80) != 0;
                    long len = b2 & 0x7F;
                    if (len == 126) {
                        len = ((in.read() & 0xFFL) << 8) | (in.read() & 0xFFL);
                    } else if (len == 127) {
                        len = 0;
                        for (int i = 0; i < 8; i++) len = (len << 8) | (in.read() & 0xFFL);
                    }
                    byte[] maskKey = null;
                    if (maskedSrv) {
                        maskKey = new byte[4];
                        readFully(in, maskKey);
                    }
                    byte[] payload = new byte[(int) len];
                    readFully(in, payload);
                    if (maskKey != null) {
                        for (int i = 0; i < payload.length; i++)
                            payload[i] ^= maskKey[i % 4];
                    }
                    if (opcode == 0x1) { // text
                        frag.reset();
                        frag.write(payload);
                        Listener l = listener;
                        if (l != null) l.onMessage(new String(frag.toByteArray(), "UTF-8"));
                    } else if (opcode == 0x8) { // close
                        break;
                    } else if (opcode == 0x9) { // ping → pong
                        sendRawPong(payload);
                    }
                }
            } catch (Exception ignored) { }
            open = false;
            Listener l = listener;
            if (l != null) l.onClosed();
        }

        private void sendRawPong(byte[] payload) {
            try {
                byte[] mask = new byte[4];
                new SecureRandom().nextBytes(mask);
                java.io.ByteArrayOutputStream h = new java.io.ByteArrayOutputStream();
                h.write(0x8A);
                h.write(0x80 | payload.length);
                h.write(mask);
                for (int i = 0; i < payload.length; i++) h.write(payload[i] ^ mask[i % 4]);
                synchronized (out) { h.writeTo(out); out.flush(); }
            } catch (Exception ignored) { }
        }

        private static void readFully(InputStream is, byte[] buf) throws Exception {
            int off = 0;
            while (off < buf.length) {
                int n = is.read(buf, off, buf.length - off);
                if (n < 0) throw new Exception("eof");
                off += n;
            }
        }
    }
}
