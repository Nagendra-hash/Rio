"""gui/music_popup.py — a small in-app music popup, modelled on the clipboard panel.

This is the surface the JioSaavn (and any other) music plays on *inside* the RIO
window instead of opening Chrome. It deliberately does NOT reuse the video
player: it owns its own QMediaPlayer + QAudioOutput, so playing music can never
touch the video decoder, the video item, or the video/sound sync timer. The two
surfaces share only the Qt toolkit, which is the same framework the clipboard
panel already uses.

The widget is a child of the main window's central widget — the exact "popup"
architecture ClipboardPanel uses (a panel you position over the HUD, not a new
top-level window, so there is no new windowing system and closing it can never
take the app down with it). A single instance is created once; repeated music
commands reuse it, so commands never stack duplicate popups.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui.style import hud_font
from gui.theme import C, HAVE_VIDEO

# QtNetwork and QtMultimedia are separate shared libraries; if either is missing
# the panel still builds (labels + buttons), it just cannot fetch artwork or
# play audio — the same graceful-degradation rule the video surface follows.
try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

    _HAVE_AUDIO = True
except Exception:  # noqa: BLE001
    QAudioOutput = QMediaPlayer = None
    _HAVE_AUDIO = False


class MusicPanel(QWidget):
    """A compact, always-there music player: artwork, title/artist, status and
    Play / Pause / Stop / Mute-Unmute controls.

    It is driven from other threads through MainWindow signals (the same
    pattern as the video/clipboard panels) and reports back only via the
    status label and the ``state_changed`` signal, which the window uses to keep
    the panel visible while something is playing and to answer "is it on?".
    """

    state_changed = pyqtSignal(bool)  # True while a source is loaded (playing or paused)

    _W = 336

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            MusicPanel {{
                background: rgba(0, 8, 14, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)

        self._net = QNetworkAccessManager(self)
        self._art_reply: QNetworkReply | None = None
        self._muted = False
        self._title = ""
        self._artist = ""

        # ── audio engine (its own — nothing about the video player is touched) ─
        if _HAVE_AUDIO:
            self._audio = QAudioOutput()
            self._audio.setMuted(False)
            self._player = QMediaPlayer()
            self._player.setAudioOutput(self._audio)
            self._player.playbackStateChanged.connect(self._on_state)
            self._player.errorOccurred.connect(self._on_error)
        else:
            self._player = None
            self._audio = None

        self._build()
        self.hide()

    # ── layout, styled like ClipboardPanel ───────────────────────────────────
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 9)
        lay.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        head = QLabel("♪  MUSIC")
        head.setFont(hud_font(7, QFont.Weight.Bold))
        head.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(head)
        self._status = QLabel("Stopped")
        self._status.setFont(hud_font(7))
        self._status.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._status)
        hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(16, 16)
        x_btn.setFont(hud_font(8))
        x_btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # Closing the panel only hides it — it never stops the app or the song.
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        mid = QHBoxLayout()
        mid.setSpacing(8)
        self._art = QLabel()
        self._art.setFixedSize(56, 56)
        self._art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._art.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px; "
            f"color: {C.TEXT_DIM}; font-size: 20px;"
        )
        self._art.setText("♪")
        mid.addWidget(self._art)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self._title_lbl = QLabel("No song loaded")
        self._title_lbl.setFont(hud_font(9, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        self._title_lbl.setWordWrap(True)
        self._sub_lbl = QLabel("")
        self._sub_lbl.setFont(hud_font(7))
        self._sub_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._sub_lbl.setWordWrap(True)
        texts.addWidget(self._title_lbl)
        texts.addWidget(self._sub_lbl)
        mid.addLayout(texts, 1)
        lay.addLayout(mid)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        _bs = (
            f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
            f"border: 1px solid {C.BORDER}; border-radius: 2px; min-height: 22px; }}"
            f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}"
        )
        self._b_play = QPushButton("▶  Play")
        self._b_pause = QPushButton("⏸  Pause")
        self._b_stop = QPushButton("⏹  Stop")
        self._b_mute = QPushButton("🔇  Mute")
        for b in (self._b_play, self._b_pause, self._b_stop, self._b_mute):
            b.setFont(hud_font(7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            btn_row.addWidget(b)
        self._b_play.clicked.connect(lambda: self.control("play"))
        self._b_pause.clicked.connect(lambda: self.control("pause"))
        self._b_stop.clicked.connect(lambda: self.control("stop"))
        self._b_mute.clicked.connect(self._toggle_mute)
        lay.addLayout(btn_row)

    # ── public, thread-safe entry points (called on the Qt thread via signals) ─
    def play(self, stream: str, title: str = "", artist: str = "",
             artwork: str = "", start: bool = True) -> None:
        """Load a song (stream URL) and optionally start it. Reuses the panel."""
        self._title = title
        self._artist = artist
        self._title_lbl.setText(title or "No song loaded")
        self._sub_lbl.setText(self._sub_text(artist, ""))
        self._fetch_artwork(artwork)
        if stream and self._player is not None:
            if self.is_active():
                self._player.stop()
            self._player.setSource(QUrl(stream))
            self._rearm_error()
            if start:
                self._player.play()
        self.show()
        self.raise_()
        self.state_changed.emit(self.is_active())

    def control(self, action: str, value: bool = False) -> None:
        """play / resume / pause / stop / mute / unmute — the popup's own knobs."""
        if action in ("play", "resume"):
            if self._player is not None and self.is_active():
                self._player.play()
        elif action == "pause":
            if self._player is not None and self.is_active():
                self._player.pause()
        elif action == "stop":
            if self._player is not None:
                self._player.stop()
        elif action == "mute":
            self._set_muted(True)
        elif action == "unmute":
            self._set_muted(False)

    # ── status reporting ──────────────────────────────────────────────────────
    def is_active(self) -> bool:
        return self._player is not None and bool(self._player.source().toString())

    def status(self) -> str:
        if self._player is None:
            return "Stopped"
        st = self._player.playbackState()
        if QMediaPlayer is None:
            return "Stopped"
        if not self.is_active():
            return "Stopped"
        if st == QMediaPlayer.PlaybackState.PlayingState:
            return "Playing"
        if st == QMediaPlayer.PlaybackState.PausedState:
            return "Paused"
        return "Stopped"

    def now_playing(self) -> str:
        if not self.is_active():
            return ""
        return f"{self._title}" + (f" — {self._artist}" if self._artist else "")

    # ── internals ─────────────────────────────────────────────────────────────
    def _sub_text(self, artist: str, extra: str) -> str:
        parts = [p for p in (artist, extra) if p]
        return " · ".join(parts)

    def _toggle_mute(self):
        self._set_muted(not self._muted)

    def _set_muted(self, muted: bool):
        self._muted = bool(muted)
        if self._audio is not None:
            self._audio.setMuted(self._muted)
        self._b_mute.setText("🔊  Unmute" if self._muted else "🔇  Mute")

    def _fetch_artwork(self, url: str):
        if not url:
            return
        self._art.setText("♪")
        try:
            req = QNetworkRequest(QUrl(url))
            req.setHeader(QNetworkRequest.Header.UserAgent, "JioSaavnRIO/1.0")
            self._art_reply = self._net.get(req)
            self._art_reply.finished.connect(self._art_finished)
        except Exception:
            self._art_reply = None

    def _art_finished(self):
        try:
            reply = self._art_reply
            self._art_reply = None
            if reply is None or reply.error() != QNetworkReply.NetworkError.NoError:
                if reply is not None:
                    reply.deleteLater()
                return
            pm = QPixmap()
            data = reply.readAll()
            if pm.loadFromData(data):
                self._art.setPixmap(pm.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))
            reply.deleteLater()
        except Exception:
            self._art_reply = None

    def _on_state(self, _state):
        self._status.setText(self.status())
        self.state_changed.emit(self.is_active())

    def _on_error(self, *_a):
        self._status.setText("Error")


    def _rearm_error(self):
        """errorOccurred auto-disconnects after each emission; re-arm it."""
        try:
            if self._player is not None:
                self._player.errorOccurred.connect(self._on_error)
        except Exception:
            pass

    def closeEvent(self, _e):
        # The panel is a child widget; closing it only hides it. It never quits
        # the application or stops the song.
        super().closeEvent(_e)

