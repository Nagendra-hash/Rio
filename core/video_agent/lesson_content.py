"""
core/video_agent/lesson_content.py — the script and the direction for the
RIO lesson video.

This file is CONTENT, not machinery: edit it to write a new lesson. The
pipeline (pipeline_lesson.py) turns it into audio, a viseme track, a Blender
scene, a render and a subtitled MP4.

Structure
---------
SCENES is a list of scenes. Each scene has
    env     "classroom" | "playground"        (the two built sets)
    lines   [(speaker, text), ...]            narration, spoken in order
    cast    {"expert": {x, y}, "boy": {x, y}} where they stand (metres)
    shots   camera setups as FRACTIONS of the scene:
            {focus, from, to, cov, lens, aim_z, side}
              focus  "expert" | "boy" | "two" | "board"
              cov    frame HEIGHT in metres (2.2 = full body with margin)
              side   camera angle offset in degrees
    cues    performance over a LINE RANGE:
            {who, action, from, to, ...action kwargs}
            actions come from the rig: idle, walk, wave, point, look,
            clap, bounce, talk, celebrate, laugh

`talk` is gesture-only here — the mouth is driven by the real audio
(core/video_agent/lipsync.py), never by a timer.
"""
from __future__ import annotations

TITLE = "The Gap Between the Event and the Feeling"

VOICES = {"expert": "am_michael", "boy": "am_puck"}

SCENES: list[dict] = [
    {
        "env": "classroom",
        "cast": {"expert": {"x": 0.22, "y": 1.25},
                 "boy": {"x": -0.40, "y": 1.40}},
        "lines": [
            ("expert", "Think about the last time something small made you "
                       "feel something very big."),
            ("expert", "Maybe a friend forgot your name, or a drawing did "
                       "not come out the way you planned."),
            ("expert", "In that moment, the feeling arrived first. The "
                       "explanation came second."),
            ("boy", "So the feeling is not the problem?"),
            ("expert", "No. The feeling is information. The problem is what "
                       "we tell ourselves about it."),
        ],
        "shots": [
            {"focus": "two", "lines": [0, 2], "aim_z": 1.10, "lens": 45},
            {"focus": "boy", "lines": [3, 3], "cov": 1.4, "aim_z": 0.72, "lens": 65, "side": -20},
            {"focus": "expert", "lines": [4, 4], "cov": 1.8, "aim_z": 1.26, "lens": 65, "side": 20},
        ],
        "cues": [
            {"who": "expert", "action": "talk", "from": 0, "to": 2},
            {"who": "boy", "action": "idle", "from": 0, "to": 2},
            {"who": "boy", "action": "look", "from": 2, "to": 3,
             "yaw": 24.0},
            {"who": "expert", "action": "talk", "from": 3, "to": 4},
            {"who": "boy", "action": "idle", "from": 4, "to": 4},
        ],
    },
    {
        "env": "classroom",
        "cast": {"expert": {"x": 0.15, "y": 1.35},
                 "boy": {"x": -0.47, "y": 1.55}},
        "lines": [
            ("expert", "Here is the first idea. Your thoughts are guesses, "
                       "not facts."),
            ("expert", "When something happens, your mind fills in the "
                       "missing pieces very quickly."),
            ("expert", "It guesses what it means, what the other person "
                       "intended, and what will happen next."),
            ("expert", "Most of the time the guess is useful. Sometimes it "
                       "is wrong, and it still feels completely true."),
            ("expert", "A thought that feels certain is not the same as a "
                       "thought that is correct."),
        ],
        "shots": [
            {"focus": "expert", "lines": [0, 1], "cov": 1.85, "aim_z": 1.24, "lens": 60, "side": 20},
            {"focus": "two", "lines": [2, 3], "aim_z": 1.10, "lens": 45},
            {"focus": "expert", "lines": [4, 4], "cov": 1.55, "aim_z": 1.3, "lens": 75, "side": 20},
        ],
        "cues": [
            {"who": "expert", "action": "talk", "from": 0, "to": 4},
            {"who": "expert", "action": "point", "from": 1, "to": 2,
             "side": "R"},
            {"who": "boy", "action": "idle", "from": 0, "to": 4},
            {"who": "boy", "action": "look", "from": 1, "to": 3,
             "yaw": 20.0},
        ],
    },
    {
        "env": "classroom",
        "cast": {"expert": {"x": 0.22, "y": 1.30},
                 "boy": {"x": -0.40, "y": 1.45}},
        "lines": [
            ("expert", "The second idea is this. Feelings are signals, not "
                       "instructions."),
            ("boy", "What is the difference?"),
            ("expert", "A signal tells you something needs attention. An "
                       "instruction tells you what to do right now."),
            ("expert", "When you are nervous before a test, the nervous "
                       "feeling is a signal that this matters to you."),
            ("expert", "It is not an order to run away. You can listen to "
                       "the signal without obeying the instruction."),
        ],
        "shots": [
            {"focus": "two", "lines": [0, 0], "aim_z": 0.9, "lens": 45},
            {"focus": "boy", "lines": [1, 1], "cov": 1.35, "aim_z": 0.66, "lens": 65, "side": -20},
            {"focus": "expert", "lines": [2, 4], "cov": 1.75, "aim_z": 1.24, "lens": 65, "side": 20},
        ],
        "cues": [
            {"who": "expert", "action": "talk", "from": 0, "to": 0},
            {"who": "boy", "action": "idle", "from": 0, "to": 0},
            {"who": "boy", "action": "talk", "from": 1, "to": 1},
            {"who": "expert", "action": "talk", "from": 2, "to": 4},
            {"who": "boy", "action": "idle", "from": 2, "to": 4},
            {"who": "boy", "action": "look", "from": 2, "to": 4,
             "yaw": 18.0},
        ],
    },
    {
        "env": "playground",
        "cast": {"expert": {"x": 0.58, "y": 0.35},
                 "boy": {"x": -0.04, "y": 0.35}},
        "lines": [
            ("expert", "The third idea is the smallest one, and the hardest "
                       "to practise. It is the pause."),
            ("boy", "How long is the pause?"),
            ("expert", "Long enough to breathe once. That is all."),
            ("expert", "In that one breath the feeling is still there, but "
                       "you are no longer only reacting."),
            ("expert", "You get to choose the next small thing you do."),
        ],
        "shots": [
            {"focus": "two", "lines": [0, 0], "aim_z": 0.92, "lens": 45},
            {"focus": "boy", "lines": [1, 1], "cov": 1.5, "aim_z": 0.7, "lens": 70, "side": -20},
            {"focus": "expert", "lines": [2, 4], "cov": 1.9, "aim_z": 1.15, "lens": 55, "side": 20},
        ],
        "cues": [
            {"who": "expert", "action": "talk", "from": 0, "to": 0},
            {"who": "boy", "action": "idle", "from": 0, "to": 0},
            {"who": "boy", "action": "talk", "from": 1, "to": 1},
            {"who": "expert", "action": "talk", "from": 2, "to": 4},
            {"who": "boy", "action": "bounce", "from": 3, "to": 4,
             "hops": 2},
        ],
    },
    {
        "env": "playground",
        "cast": {"expert": {"x": 0.52, "y": 0.40},
                 "boy": {"x": -0.10, "y": 0.40}},
        "lines": [
            ("expert", "Let us try it together. Notice something you feel "
                       "right now."),
            ("expert", "Maybe your feet on the ground, or the air in your "
                       "chest."),
            ("expert", "Name it quietly, in one or two words. Tight. Warm. "
                       "Buzzy. Calm."),
            ("expert", "Then take one slow breath, and let your shoulders "
                       "drop."),
            ("expert", "That is the whole practice. Notice, name, breathe."),
            ("expert", "You can do it in a classroom, on a bus, or before "
                       "you answer a hard question."),
        ],
        "shots": [
            {"focus": "two", "lines": [0, 1], "aim_z": 0.94, "lens": 48},
            {"focus": "boy", "lines": [2, 3], "cov": 1.45, "aim_z": 0.72, "lens": 65, "side": -20},
            {"focus": "expert", "lines": [4, 4], "cov": 1.6, "aim_z": 1.2, "lens": 70, "side": 20},
            {"focus": "two", "lines": [5, 5], "aim_z": 1.05, "lens": 48},
        ],
        "cues": [
            {"who": "expert", "action": "talk", "from": 0, "to": 5},
            {"who": "boy", "action": "idle", "from": 0, "to": 2},
            {"who": "boy", "action": "look", "from": 2, "to": 3,
             "yaw": -14.0},
            {"who": "boy", "action": "idle", "from": 3, "to": 5},
        ],
    },
    {
        "env": "classroom",
        "cast": {"expert": {"x": 0.18, "y": 1.28},
                 "boy": {"x": -0.44, "y": 1.42}},
        "lines": [
            ("expert", "So here is what we covered today."),
            ("expert", "Your thoughts are guesses. Your feelings are "
                       "signals. And between the two, you can build a pause."),
            ("expert", "None of this makes difficult feelings disappear."),
            ("expert", "It makes them easier to carry, because you are "
                       "carrying them on purpose."),
            ("boy", "And if I forget?"),
            ("expert", "Then you begin again. That is not failure. That is "
                       "the practice."),
        ],
        "shots": [
            {"focus": "two", "lines": [0, 1], "aim_z": 0.94, "lens": 48},
            {"focus": "expert", "lines": [2, 3], "cov": 1.7, "aim_z": 1.24, "lens": 65, "side": 20},
            {"focus": "boy", "lines": [4, 4], "cov": 1.45, "aim_z": 0.72, "lens": 65, "side": -20},
            {"focus": "expert", "lines": [5, 5], "cov": 1.7, "aim_z": 1.24, "lens": 65, "side": 20},
        ],
        "cues": [
            {"who": "expert", "action": "talk", "from": 0, "to": 3},
            {"who": "boy", "action": "idle", "from": 0, "to": 3},
            {"who": "boy", "action": "talk", "from": 4, "to": 4},
            {"who": "expert", "action": "talk", "from": 5, "to": 5},
            {"who": "boy", "action": "idle", "from": 5, "to": 5},
            {"who": "expert", "action": "wave", "from": 5, "to": 5,
             "side": "R"},
        ],
    },
]


def word_count() -> int:
    return sum(len(text.split()) for s in SCENES for _, text in s["lines"])
