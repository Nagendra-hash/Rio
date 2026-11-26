"""The HUD, split out of ui.py in Mark LVII.

ui.py is still the entry point and still exports every name it always did,
so main.py, plugins and anything written against Mark LVI keep working.

The Qt video environment is set here, in the package __init__, because every
route into the HUD comes through this module first — `from gui.window import
MainWindow` runs this file before it runs window.py — and these variables must
be in place before the multimedia plugin loads.
"""
import os

# Before any Qt import: Qt's video backend prints signed stream URLs otherwise.
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")

# ── Video decoding policy: hardware when it works, software when it must ─────
#
# Hardware decoding is deliberately LEFT AT QT'S DEFAULT (auto). Qt probes the
# hardware device types FFmpeg advertises, uses one when the codec is supported,
# and falls back to software at decoder creation when it is not. Disabling that
# globally — which an earlier revision did with
# QT_FFMPEG_DECODING_HW_DEVICE_TYPES="" — also threw away hardware decoding for
# H.264 and VP9 on machines where it works, so it is gone.
#
# The AV1 problem is NOT a hardware-decoding problem and cannot be fixed here.
# The FFmpeg bundled with PyQt6 6.12 ships no software AV1 decoder: there is no
# libdav1d and no libaom-av1 in the bundle, and FFmpeg's native `av1` decoder is
# a hardware-only front end. With no working AV1 hardware it logs
#
#   [av1 @ 0x..] Your platform doesn't support hardware accelerated AV1 decoding.
#   [av1 @ 0x..] Failed to get pixel format.
#   [av1 @ 0x..] Get current frame error
#
# once per frame and produces no picture. No environment variable changes that,
# because there is no software decoder to fall back to. Setting the variable to
# "" only silences the attempt; the file still does not play.
#
# So AV1 is handled before Qt sees it: core/media_prep.py asks ffprobe what the
# file contains, converts AV1 to H.264 once (cached), and hands Qt the H.264
# copy. Playable codecs go straight to the player untouched, so they keep both
# their hardware decoding and their zero-latency start.
#
# QT_LOGGING_RULES cannot hide FFmpeg's stderr either: unless QT_FFMPEG_DEBUG is
# set, Qt passes every FFmpeg message straight to av_log_default_callback.
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
