"""Which stream yt-dlp resolution picks for the HUD.

The HUD's Qt/FFmpeg cannot software-decode AV1 at all, so a 4K AV1 stream is
worth less than a 720p H.264 one. These pin the ordering that makes that true,
and the fact that AV1 is still chosen when it is the only thing on offer (the
alternative would be claiming nothing is playable).

TWO MODULES, because this repo carries two copies: `actions/video_player.py`
(the maintained file) and `actions/Mark-LVII_actions_video_player.py` (the
LVII port, which action discovery loads first). Both must rank codecs the same
way or the running assistant and the tests would disagree.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "actions" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module", params=[
    ("actions.video_player", None),
    ("live_video_player", "Mark-LVII_actions_video_player.py"),
])
def rank(request):
    name, filename = request.param
    if filename is None:
        module = _load(name, "video_player.py")
    else:
        module = _load(name, filename)
    return module._codec_rank


def test_h264_is_first(rank):
    assert rank({"vcodec": "avc1.640028"}) == 0
    assert rank({"vcodec": "h264"}) == 0


def test_vp9_is_second(rank):
    assert rank({"vcodec": "vp9"}) == 1
    assert rank({"vcodec": "vp09.00.10.08"}) == 1


def test_av1_is_last(rank):
    assert rank({"vcodec": "av01.0.05M.08"}) == 3
    assert rank({"vcodec": "av1"}) == 3


def test_unknown_codecs_sit_between_vp9_and_av1(rank):
    assert rank({"vcodec": "theora"}) == 2
    assert rank({"vcodec": None}) == 2


def test_the_ranking_actually_reorders_candidates(rank):
    formats = [
        {"vcodec": "av01.0.05M.08", "tbr": 9000},
        {"vcodec": "vp9", "tbr": 2000},
        {"vcodec": "avc1.640028", "tbr": 1500},
    ]
    ranked = sorted(formats, key=rank)
    assert [f["vcodec"] for f in ranked] == ["avc1.640028", "vp9", "av01.0.05M.08"]
