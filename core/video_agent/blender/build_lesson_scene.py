"""
core/video_agent/blender/build_lesson_scene.py — educational two-character
scene with AUDIO-DRIVEN lip-sync.

Run by Blender (headless):

    blender -b --factory-startup --python build_lesson_scene.py -- \
        --out lesson.blend --direction direction.json --visemes visemes.json

`direction.json` is the whole film: scenes (environment + cast placement),
camera shots and per-character action cues. `visemes.json` is produced by
core/video_agent/lipsync.py from the REAL narration audio, so the mouth
moves with the voice instead of on a timer.

Honest limits (also written into the manifest by the pipeline):
  * the characters are articulated joint-pivot rigs — no armature/skinning;
  * the mouth is a procedural shape set (8 visemes), not a blend-shape
    face rig: consonants that share a mouth shape look identical;
  * blink, brow and head motion are procedural, not mocap.
"""
from __future__ import annotations

import json
import math
import os
import sys

import bpy  # noqa: F401  (Blender provides it)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_character_dance_scene as rig            # noqa: E402

FPS = 24
RES_W, RES_H = 720, 1280

# the two environments live at different Y so one scene can hold both
ENV_ORIGIN = {"playground": (0.0, 0.0), "classroom": (0.0, -14.0)}

# mouth sphere scale per viseme (x = width, y = depth, z = opening)
MOUTH = {
    # z is the OPENING: REST is a thin closed line, not a small oval
    "REST": (1.48, 0.34, 0.14),
    "MBP":  (1.46, 0.34, 0.13),
    "FV":   (1.44, 0.40, 0.46),
    "TH":   (1.38, 0.46, 0.72),
    "L":    (1.62, 0.46, 0.86),
    "CH":   (1.40, 0.55, 1.00),
    "KG":   (1.36, 0.62, 1.18),
    "A":    (1.50, 0.72, 1.52),
    "E":    (1.90, 0.50, 1.00),
    "O":    (1.12, 0.76, 1.40),
    "U":    (1.04, 0.70, 1.24),
}
OPEN = {"REST": 0.0, "MBP": 0.0, "FV": 0.18, "TH": 0.3, "L": 0.35, "CH": 0.45,
        "KG": 0.55, "E": 0.6, "O": 0.75, "U": 0.55, "A": 1.0}


def _f(t: float) -> int:
    return max(1, int(round(t * FPS)) + 1)


def reset_scene():
    """`--factory-startup` still hands us the default Cube, Camera and
    Light. The Cube sits at the world origin — inside the playground set —
    and filled the bottom half of every playground shot."""
    for obj in list(bpy.data.objects):        # noqa: F821
        bpy.data.objects.remove(obj, do_unlink=True)   # noqa: F821
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.lights,
                 bpy.data.cameras):
        for block in list(coll):
            if block.users == 0:
                coll.remove(block)


def freeze_stages(chars):
    """Placement keys must be CONSTANT. run_cues() calls finish_smoothing(),
    which sets every fcurve on `{name}_*` to SINE — including the stage —
    and the characters then glided from the classroom to the playground
    across the cut instead of teleporting."""
    for ch in chars:
        act = ch["stage"].animation_data.action if ch["stage"].animation_data \
            else None
        for fc in (act.fcurves if act else []):
            for kp in fc.keyframe_points:
                kp.interpolation = "CONSTANT"


# ── environments ────────────────────────────────────────────────────────────

def build_classroom(origin):
    """A warm, colourful classroom: the expert teaches here."""
    ox, oy = origin
    M = rig.MAT
    rig.add_cube("Class_floor", 1.0, (ox, oy - 1.4, -0.05),
                 M["class_floor"], scale=(7.0, 9.0, 0.1))
    rig.add_cube("Class_back", 1.0, (ox, oy + 3.2, 1.8), M["class_wall"],
                 scale=(7.0, 0.12, 3.6))
    rig.add_cube("Class_sideL", 1.0, (ox - 3.4, oy - 1.4, 1.8),
                 M["class_wall2"], scale=(0.12, 9.0, 3.6))
    rig.add_cube("Class_sideR", 1.0, (ox + 3.4, oy - 1.4, 1.8),
                 M["class_wall2"], scale=(0.12, 9.0, 3.6))
    # chalkboard + frame
    rig.add_cube("Board_frame", 1.0, (ox, oy + 3.05, 1.55), M["wood_dark"],
                 scale=(2.35, 0.08, 1.05))
    rig.add_cube("Board", 1.0, (ox, oy + 2.98, 1.55), M["board"],
                 scale=(2.2, 0.05, 0.95))
    # chalk marks: a few thin bars read as handwriting at 720p
    # every mark must sit INSIDE the board (1.1 x 0.475 half-extents): the
    # first set put two bars on the wall above and below it, which read as
    # white sticks floating in mid-air
    for i, (dx, dz, w) in enumerate(((-0.72, 0.30, 0.50), (-0.35, 0.30, 0.40),
                                     (-0.62, 0.10, 0.85), (0.30, 0.10, 0.55),
                                     (-0.50, -0.12, 0.70), (0.15, -0.32, 0.95),
                                     (0.60, -0.32, 0.30))):
        rig.add_cube(f"Chalk{i}", 1.0, (ox + dx, oy + 2.92, 1.55 + dz),
                     M["chalk"], scale=(w, 0.02, 0.030))
    # teacher desk + books + plant
    rig.add_cube("Desk", 1.0, (ox + 1.55, oy + 1.1, 0.42), M["desk"],
                 scale=(1.1, 0.5, 0.06))
    for i, sx in enumerate((-0.45, 0.45)):
        rig.add_cube(f"Desk_leg{i}", 1.0, (ox + 1.55 + sx, oy + 1.1, 0.2),
                     M["wood_dark"], scale=(0.05, 0.05, 0.42))
    for i, (dx, dz, col) in enumerate(((-0.35, 0.475, "book_r"),
                                       (-0.15, 0.512, "book_b"),
                                       (-0.25, 0.549, "book_y"))):
        rig.add_cube(f"Book{i}", 1.0, (ox + 1.55 + dx, oy + 1.0, dz), M[col],
                     scale=(0.22, 0.16, 0.035))
    rig.add_cyl("Plant_pot", 0.1, 0.16, (ox + 2.2, oy + 1.0, 0.52), M["pot"])
    rig.add_sphere("Plant", 0.16, (ox + 2.2, oy + 1.0, 0.72), M["leaf"],
                   scale=(1.0, 1.0, 1.2), seg=10, ring=8)
    # rug + pupil desk (the boy sits here in scene 1)
    rig.add_cyl("Rug", 1.5, 0.02, (ox - 0.6, oy - 0.4, 0.011), M["rug"],
                verts=32)
    rig.add_cube("Pupil_desk", 1.0, (ox - 1.6, oy + 0.5, 0.36), M["desk"],
                 scale=(0.55, 0.35, 0.05))
    for i, sx in enumerate((-0.4, 0.4)):
        rig.add_cube(f"Pupil_leg{i}", 1.0, (ox - 1.6 + sx, oy + 0.5, 0.18),
                     M["wood_dark"], scale=(0.04, 0.04, 0.36))
    # ceiling: without it the portrait camera sees open sky over the walls
    rig.add_cube("Class_ceiling", 1.0, (ox, oy - 1.4, 3.62),
                 M["class_ceiling"], scale=(7.0, 9.0, 0.1))
    # window with sky
    rig.add_cube("Window", 1.0, (ox - 3.3, oy - 0.6, 1.9), M["skywin"],
                 scale=(0.05, 1.0, 1.0))
    rig.add_cube("Window_frame", 1.0, (ox - 3.28, oy - 0.6, 1.9), M["wood"],
                 scale=(0.06, 1.06, 0.06))


def build_classroom_lights(origin):
    """The playground lights are 14 m away from the classroom — it would be
    pitch black. Local key/fill/rim for the room."""
    ox, oy = origin

    def lamp(name, kind, loc, energy, color, size=3.0):
        data = bpy.data.lights.new(name, kind)      # noqa: F821
        data.energy = energy
        data.color = color
        if kind == "AREA":
            data.size = size
        obj = bpy.data.objects.new(name, data)      # noqa: F821
        obj.location = loc
        bpy.context.collection.objects.link(obj)    # noqa: F821
        return obj

    lamp("Class_key", "AREA", (ox - 1.5, oy - 1.9, 2.85), 155,
         (1.0, 0.94, 0.86), 4.2)
    lamp("Class_fill", "AREA", (ox + 2.1, oy - 1.2, 2.35), 70,
         (0.80, 0.88, 1.0), 3.0)
    # the rim sits behind the cast and points AT their backs. Pointing it
    # straight down (the default) burned a white hole in the back wall.
    rim = lamp("Class_rim", "AREA", (ox, oy + 2.5, 2.5), 95,
               (1.0, 0.90, 0.72), 2.6)
    rim.rotation_euler = (math.radians(-44), 0.0, 0.0)
    # An AREA light only emits from one side (downward by default), so the
    # top of the back wall received nothing and read as a grey band above a
    # sharp shadow line. Wash it.
    wash = lamp("Class_wash", "AREA", (ox, oy - 2.4, 3.45), 350,
                (1.0, 0.95, 0.88), 5.0)
    wash.rotation_euler = (math.radians(89), 0.0, 0.0)


def build_scene_materials():
    rig.build_materials()
    M = rig.MAT
    M["class_floor"] = rig.make_mat("class_floor", (0.62, 0.47, 0.33),
                                    rough=0.8)
    M["class_wall"] = rig.make_mat("class_wall", (0.93, 0.87, 0.72), rough=0.9)
    M["class_wall2"] = rig.make_mat("class_wall2", (0.88, 0.80, 0.66),
                                    rough=0.9)
    M["class_ceiling"] = rig.make_mat("class_ceiling", (0.95, 0.93, 0.88),
                                      rough=0.95,
                                      emission=(0.95, 0.93, 0.88),
                                      strength=0.85)
    M["board"] = rig.make_mat("board", (0.09, 0.24, 0.18), rough=0.85)
    M["chalk"] = rig.make_mat("chalk", (0.95, 0.95, 0.90), rough=0.9)
    M["desk"] = rig.make_mat("desk", (0.72, 0.52, 0.30), rough=0.6)
    M["wood"] = rig.make_mat("wood", (0.48, 0.32, 0.18), rough=0.7)
    M["wood_dark"] = rig.make_mat("wood_dark", (0.32, 0.21, 0.12), rough=0.7)
    M["rug"] = rig.make_mat("rug", (0.55, 0.22, 0.26), rough=0.9)
    M["pot"] = rig.make_mat("pot", (0.75, 0.42, 0.28), rough=0.7)
    M["book_r"] = rig.make_mat("book_r", (0.72, 0.16, 0.18), rough=0.6)
    M["book_b"] = rig.make_mat("book_b", (0.16, 0.30, 0.62), rough=0.6)
    M["book_y"] = rig.make_mat("book_y", (0.92, 0.76, 0.18), rough=0.6)
    M["expert_shirt"] = rig.make_mat("expert_shirt", (0.94, 0.93, 0.90),
                                     rough=0.85)
    M["skywin"] = rig.make_mat("skywin", (0.45, 0.70, 0.95), rough=1.0,
                               emission=(0.45, 0.70, 0.95), strength=0.7)


# ── face: lip-sync, blinks, brows ───────────────────────────────────────────

def _key_scale(obj, frame, sx, sy, sz):
    obj.scale = (sx, sy, sz)
    obj.keyframe_insert("scale", frame=frame)


def apply_visemes(ch: dict, track: dict, duration: float | None = None,
                  blink_phase: float = 0.0, log=print) -> dict:
    """Key the mouth (and a little head/brow life) from the viseme track."""
    s = ch.get("scale", 1.0)
    mouth = ch["mouth"]
    cavity = ch.get("cavity")
    head = ch["head"]
    brows = ch.get("brows") or []
    base = 0.032 * s

    # every speaker starts shut and ends shut, whether or not they talk in
    # the first/last scene
    rest = MOUTH["REST"]
    _key_scale(mouth, 1, *rest)
    if cavity is not None:
        _key_scale(cavity, 1, rest[0] * 0.90, 0.60, rest[2] * 0.84)

    events = track.get("events") or []
    if events:
        first = _f(float(events[0]["t0"]))
        if first > 2:
            # hold the shut mouth right up to the first phoneme instead of
            # interpolating from frame 1 to it
            _key_scale(mouth, first - 1, *rest)
            if cavity is not None:
                _key_scale(cavity, first - 1, rest[0] * 0.90, 0.60,
                           rest[2] * 0.84)
    head_base_x = float(head.rotation_euler.x)
    next_nod = 0
    for i, ev in enumerate(events):
        f0 = _f(float(ev["t0"]))
        f1 = _f(float(ev["t1"]))
        shape = str(ev.get("shape") or "REST")
        nxt = events[i + 1]["t0"] if i + 1 < len(events) else None
        mx, my, mz = MOUTH.get(shape, MOUTH["REST"])
        # the mesh is a 0.032*s sphere; scale is in units of its radius
        _key_scale(mouth, f0, mx, my, mz)
        if f1 > f0:
            _key_scale(mouth, f1, mx, my, mz)      # hold the shape
        if cavity is not None:
            # the dark interior sits just in front of the lip sphere and is
            # scaled slightly smaller, so an open mouth reads as a dark
            # oval inside a red rim instead of a flat red blob
            cx, cz = mx * 0.90, mz * 0.84
            _key_scale(cavity, f0, cx, 0.60, cz)
            if f1 > f0:
                _key_scale(cavity, f1, cx, 0.60, cz)
        # a small nod on the wide-open vowels keeps the head alive. It is
        # rate-limited (>=0.45 s apart) and returns to the pose the cue
        # system left, so it never fights a `look` cue.
        if shape in ("A", "O") and f0 >= next_nod:
            next_nod = f0 + int(0.45 * FPS)
            head.rotation_euler.x = head_base_x + math.radians(-1.8)
            head.keyframe_insert("rotation_euler", frame=f0)
            head.rotation_euler.x = head_base_x
            head.keyframe_insert("rotation_euler", frame=f0 + 3)
        for b in brows:
            z = b.location.z
            lift = 0.006 * s if shape in ("A", "E") else 0.0
            b.location.z = z + lift
            b.keyframe_insert("location", frame=f0)
            b.location.z = z
            b.keyframe_insert("location", frame=f0 + 4)
        # a real pause between phrases: close the mouth instead of holding
        # the last vowel until the next word (that is the classic
        # "talking with nobody speaking" tell)
        if nxt is None or float(nxt) > float(ev["t1"]) + 0.05:
            rest = MOUTH["REST"]
            _key_scale(mouth, f1 + 1, *rest)
            if cavity is not None:
                _key_scale(cavity, f1 + 1, rest[0] * 0.90, 0.60,
                           rest[2] * 0.84)

    for obj in [mouth] + ([cavity] if cavity is not None else []):
        act = obj.animation_data.action if obj.animation_data else None
        for fc in (act.fcurves if act else []):
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"

    # BLINKS: deterministic, ~every 3.2 s with a small variation, so the
    # face is never frozen between sentences. The eyeball (white + pupil) is
    # squashed vertically for two frames — no eyelid geometry needed.
    eyes = ch.get("eyes") or []
    i = 0
    if eyes:
        t = 0.8 + blink_phase
        end_t = float(duration if duration is not None
                      else (track.get("total_s") or 0.0)) + 1.0
        while t < end_t:
            f0 = _f(t)
            for white, pupil in eyes:
                for eye in (white, pupil):
                    _key_scale(eye, f0, 1.0, 1.0, 1.0)
                    _key_scale(eye, f0 + 1, 1.0, 1.0, 0.12)
                    _key_scale(eye, f0 + 3, 1.0, 1.0, 1.0)
            i += 1
            t += 2.6 + ((i * 37) % 11) * 0.12        # 2.6 … 3.8 s
    log(f"[lesson] visemes keyed: {len(events)} shapes, "
        f"{i} blinks")
    return {"visemes": len(events), "blinks": i}


# ── camera + direction ──────────────────────────────────────────────────────

def coverage_distance(cov_v: float, lens: float) -> float:
    """Distance that yields `cov_v` metres of VERTICAL coverage.

    sensor_fit=HORIZONTAL maps the 36 mm sensor to the 720 px width, so the
    vertical coverage is 16/9 of the horizontal one:
        vertical = d * 36/lens * (1280/720)   ->   d = cov * lens / 64
    `cov` is the frame HEIGHT in metres.
    """
    return cov_v * lens / 64.0


def build_camera():
    cam_data = bpy.data.cameras.new("Cam")           # noqa: F821
    cam_data.lens = 55
    cam_data.sensor_fit = "HORIZONTAL"
    cam_data.sensor_width = 36.0
    cam_data.clip_start = 0.05
    cam = bpy.data.objects.new("Cam", cam_data)      # noqa: F821
    bpy.context.scene.collection.objects.link(cam)   # noqa: F821
    target = bpy.data.objects.new("CamTarget", None)  # noqa: F821
    bpy.context.scene.collection.objects.link(target)
    con = cam.constraints.new("TRACK_TO")
    con.target = target
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"
    return cam, target


def _focus_point(focus, cast_pos, origin):
    ox, oy = origin
    if focus in cast_pos:
        x, y = cast_pos[focus]
        return (x + ox, y + oy)
    if focus == "two" and len(cast_pos) >= 2:
        pts = list(cast_pos.values())
        return (sum(p[0] for p in pts) / len(pts) + ox,
                sum(p[1] for p in pts) / len(pts) + oy)
    if focus == "board":
        return (ox, oy + 2.95)
    return (ox, oy)


def direct_camera(cam, target, scenes):
    """One keyed setup per shot. The camera is placed FROM the cast, not
    from an absolute point: aiming at (cx, oy) while the actor stands 0.6 m
    behind it is what produced a frame full of chalkboard."""
    for scene in scenes:
        env = str(scene.get("env"))
        origin = ENV_ORIGIN.get(env, (0.0, 0.0))
        cast = scene.get("cast") or {}
        cast_pos = {k: (float(v.get("x", 0.0)), float(v.get("y", 0.0)))
                    for k, v in cast.items()}
        for shot in scene.get("shots") or []:
            t0, t1 = float(shot["t0"]), float(shot["t1"])
            lens = float(shot.get("lens", 55))
            aim_z = float(shot.get("aim_z", 1.02))
            side = math.radians(float(shot.get("side", 0.0)))
            focus = str(shot.get("focus", "two"))
            fx, fy = _focus_point(focus, cast_pos, origin)
            # a single-character shot still catches the other actor at the
            # frame edge when they stand 0.6 m apart. Slide the whole setup
            # AWAY from the other actor (aim_dx) so the frame edge falls
            # between them instead of slicing a body in half.
            fx += float(shot.get("aim_dx", 0.0))
            cov = float(shot.get("cov", 2.6))
            if focus == "two" and len(cast_pos) >= 2:
                xs = [p[0] for p in cast_pos.values()]
                sep = max(xs) - min(xs)
                # Both bodies + a little air must fit ACROSS the frame. In a
                # 9:16 frame the horizontal field is 9/16 of the vertical one,
                # so every metre of width costs 1.78 m of height — keep the
                # margin tight (0.52 m total) or the shot becomes all ceiling.
                # sep is centre-to-centre; add a body width (~0.44 m for
                # two people) and 0.26 m of air
                cov = max(cov, (sep + 0.92) * (16.0 / 9.0))
            dist = coverage_distance(cov, lens)
            cam.data.lens = lens

            def place(t, scale, fx=fx, fy=fy, dist=dist, aim_z=aim_z,
                      side=side):
                d = dist * scale
                cam.location = (fx + math.sin(side) * d,
                                fy - math.cos(side) * d,
                                aim_z + 0.10)
                target.location = (fx, fy, aim_z)
                f = _f(t)
                cam.keyframe_insert("location", frame=f)
                target.keyframe_insert("location", frame=f)
                cam.data.keyframe_insert("lens", frame=f)

            place(t0, 1.0)                              # a slow push-in
            place(t0 + (t1 - t0) * 0.55, 0.955)         # inside the shot
            place(t1, 1.0)

    act = cam.animation_data.action if cam.animation_data else None
    for fc in (act.fcurves if act else []):
        for kp in fc.keyframe_points:
            kp.interpolation = "SINE"
    act = target.animation_data.action if target.animation_data else None
    for fc in (act.fcurves if act else []):
        for kp in fc.keyframe_points:
            kp.interpolation = "CONSTANT"      # aim snaps at cuts


def make_stage(char):
    """A second empty above the rig root.

    Placement and performance used to share the root: `idle()` keys the
    root's location every 1/12 s, so it overwrote the scene position and the
    character ended up standing in the wrong room (and off-frame). The stage
    owns WHERE the character is, the root owns WHAT it does."""
    stage = rig.pivot(f"{char['name']}_stage", (0.0, 0.0, 0.0), None)
    rig.parent_to(char["root"], stage)
    char["stage"] = stage
    return stage


def place_cast(char, cast_entry, scene_env, t0):
    """Teleport a character into a scene. Both environments live in ONE
    Blender scene, so a cut is a hard cut: CONSTANT keys on the stage."""
    ox, oy = ENV_ORIGIN.get(scene_env, (0.0, 0.0))
    x = float(cast_entry.get("x", 0.0)) + ox
    y = float(cast_entry.get("y", 0.0)) + oy
    stage = char["stage"]
    stage.location = (x, y, 0.0)
    stage.keyframe_insert("location", frame=_f(t0))
    act = stage.animation_data.action if stage.animation_data else None
    for fc in (act.fcurves if act else []):
        for kp in fc.keyframe_points:
            kp.interpolation = "CONSTANT"
    return x


def hide_cast(char, t0):
    """Park a character that is not in this scene far below the floor."""
    stage = char["stage"]
    stage.location = (0.0, 0.0, -60.0)
    stage.keyframe_insert("location", frame=_f(t0))


def talk_gestures(ch, t0, t1):
    """Teaching gestures: one hand rises, opens and settles — no mouth keys."""
    a = rig.Anim(ch)
    arm = ch["arms"]["R"]
    elbow = arm["elbow"]
    t = t0
    while t <= t1 + 0.05:
        ph = 2 * math.pi * 0.55 * (t - t0)
        a.rot(arm["shoulder"], t, z=math.radians(5 + 3 * math.sin(ph)),
              x=math.radians(-32 + 9 * math.sin(ph / 2)))
        a.rot(elbow, t, x=math.radians(-46 - 10 * math.sin(ph / 2)))
        a.rot(ch["neck"], t, z=math.radians(3.5 * math.sin(ph / 3)))
        t += 1 / 12.0


def run_cues(char, cues, who, log=print):
    """Map the direction's action names onto the rig's action library."""
    action = rig.Anim(char)
    for cue in cues:
        if str(cue.get("who")) != who:
            continue
        name = str(cue.get("action") or "idle")
        t0, t1 = float(cue["t0"]), float(cue["t1"])
        fn = getattr(action, name, None)
        if fn is None:
            log(f"[lesson] unknown action '{name}' — skipped")
            continue
        try:
            if name in ("walk", "run"):
                fn(t0, t1, x_from=float(cue.get("x_from", 0.0)),
                   x_to=float(cue.get("x_to", 0.0)))
            elif name in ("idle", "dance_sidestep"):
                fn(t0, t1, base_x=float(cue.get("base_x", 0.0)))
            elif name in ("wave", "point"):
                fn(t0, t1, side=str(cue.get("side", "R")))
            elif name == "look":
                fn(t0, t1, yaw_deg=float(cue.get("yaw", 0.0)),
                   pitch_deg=float(cue.get("pitch", 0.0)))
            elif name == "clap":
                fn(t0, t1, claps=int(cue.get("claps", 4)))
            elif name == "bounce":
                fn(t0, t1, hops=int(cue.get("hops", 3)))
            elif name == "talk":
                # NOT rig.Anim.talk: that keys a 4 Hz mouth flap which would
                # overwrite the audio-driven visemes. Gestures only.
                talk_gestures(char, t0, t1)
            else:
                fn(t0, t1)
        except TypeError as exc:                      # bad cue → keep going
            log(f"[lesson] cue {name} {t0}-{t1} failed: {exc}")
    rig.finish_smoothing(char)


# ── main ────────────────────────────────────────────────────────────────────

def main():
    global RES_W, RES_H, FPS
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    opts = {"--out": "lesson.blend", "--direction": "", "--visemes": "",
            "--res": "720x1280", "--fps": "24"}
    for i, a in enumerate(argv):
        if a in opts and i + 1 < len(argv):
            opts[a] = argv[i + 1]
    RES_W, RES_H = map(int, opts["--res"].split("x"))
    FPS = int(opts["--fps"])

    direction = json.loads(open(opts["--direction"], encoding="utf-8").read())
    scenes = direction.get("scenes") or []
    duration = max((float(s.get("t1", 0)) for s in scenes), default=10.0)

    reset_scene()
    build_scene_materials()
    envs = {str(s.get("env")) for s in scenes}
    if "playground" in envs:
        rig.build_playground()
    if "classroom" in envs:
        build_classroom(ENV_ORIGIN["classroom"])
        build_classroom_lights(ENV_ORIGIN["classroom"])
    rig.build_lights()

    expert = rig.build_character(
        "Expert",
        {"skin": "expert_skin", "hair": "expert_hair", "hair_style": "wavy",
         "top": "expert_blazer", "top2": "expert_vest",
         "legs": "expert_trousers", "legs2": "expert_trousers",
         "shoe": "shoe", "glasses": True, "beard": True, "scale": 1.12,
         "fore": "expert_blazer", "shirt": "expert_shirt"},
        x=0.0, y=0.0)
    make_stage(expert)
    boy = rig.build_character(
        "Boy",
        {"skin": "boy_skin", "hair": "boy_hair", "hair_style": "short",
         "top": "boy_hoodie", "legs": "boy_bottoms",
         "legs2": "boy_bottoms", "shoe": "shoe", "scale": 0.72,
         "fore": "boy_hoodie", "torso_h": 0.92},
        x=0.0, y=0.0)
    make_stage(boy)

    cam, target = build_camera()
    bpy.context.scene.camera = cam

    for scene in scenes:
        env = str(scene.get("env"))
        cast = scene.get("cast") or {}
        t0 = float(scene.get("t0", 0.0))
        for key, char in (("expert", expert), ("boy", boy)):
            if key in cast:
                place_cast(char, cast[key], env, t0)
            else:
                hide_cast(char, t0)

    # one action pass per character over the whole timeline
    all_cues = [c for s in scenes for c in (s.get("cues") or [])]
    run_cues(expert, all_cues, "expert")
    run_cues(boy, all_cues, "boy")
    freeze_stages((expert, boy))

    direct_camera(cam, target, scenes)

    lip = {}
    if opts["--visemes"] and os.path.isfile(opts["--visemes"]):
        doc = json.loads(open(opts["--visemes"], encoding="utf-8").read())
        tracks = doc.get("tracks") or {}
        if not tracks and "frames" in doc:        # single-speaker track
            tracks = {str(direction.get("speaker") or "expert"): doc}
        for n, (who, track) in enumerate(sorted(tracks.items())):
            ch = expert if who == "expert" else boy
            lip[who] = apply_visemes(ch, track, duration=duration,
                                     blink_phase=0.0 if n == 0 else 1.1)

    rig.set_render_settings(RES_W, RES_H, FPS, duration)
    world = bpy.context.scene.world            # noqa: F821
    if world and world.use_nodes:
        bg = world.node_tree.nodes.get("Background")
        if bg:
            bg.inputs[0].default_value = (0.60, 0.70, 0.84, 1.0)
            bg.inputs[1].default_value = 0.34
    sun = bpy.data.objects.get("Sun")          # noqa: F821
    if sun and sun.type == "LIGHT":
        sun.data.energy = 2.7
        sun.data.color = (1.0, 0.95, 0.86)
    bpy.ops.wm.save_as_mainfile(filepath=opts["--out"], compress=True)  # noqa
    print("SCENE_BUILT", opts["--out"], "frames", int(FPS * duration),
          "speakers", {k: v.get("visemes") for k, v in lip.items()},
          "blinks", {k: v.get("blinks") for k, v in lip.items()})


if __name__ == "__main__":
    main()
