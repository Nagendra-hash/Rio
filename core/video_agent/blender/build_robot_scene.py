# build_robot_scene.py — "The Little Robot's Adventure" (Blender 3.6, bpy).
#
# Builds the complete 3D scene: an expressive blue-and-white robot in a
# futuristic laboratory, with a 15-second keyframed story in three shots,
# and saves a .blend ready for frame rendering. Runs headless:
#
#   blender -b --factory-startup --python build_robot_scene.py -- \
#       --out /path/robot.blend [--ssr 1|0]
#
# Everything is REAL 3D geometry (meshes, articulated limb chains, animated
# rig via keyframes on joint rotations) — no images, no slides, no fakes.
# Light geometry + Eevee-friendly settings so a 2C/4T laptop with an Intel
# HD 4400 can render it.
from __future__ import annotations

import math
import sys

import bpy                                          # noqa: E402
from mathutils import Matrix

FPS = 24
DURATION_S = 15.0
END_FRAME = int(FPS * DURATION_S)

# story landmarks (seconds)
S2_START, S2_END = 5.0, 9.6          # walk window
TOUCH_S = 11.1                       # robot touches the sphere
SMILE_S = 12.6

SPHERE_POS = (2.6, 0.0, 1.35)
ROBOT_STOP_X = 1.95                  # where the walk ends (arm's reach away)


def seconds(t: float) -> int:
    """Story time (s) → frame number at the build fps."""
    return max(1, int(round(t * FPS)) + 1)


# ── materials ────────────────────────────────────────────────────────────────

def make_mat(name, base=(0.9, 0.9, 0.92), metallic=0.85, rough=0.3,
             emission=None, strength=0.0, alpha=1.0):
    mat = bpy.data.materials.new(name)          # noqa: F821
    mat.use_nodes = True
    node = mat.node_tree.nodes.get("Principled BSDF")
    node.inputs["Base Color"].default_value = (*base, 1.0)
    node.inputs["Metallic"].default_value = metallic
    node.inputs["Roughness"].default_value = rough
    if alpha < 1.0:
        node.inputs["Alpha"].default_value = alpha
        mat.blend_method = "BLEND"
    if emission is not None:
        node.inputs["Emission"].default_value = (*emission, 1.0)
        node.inputs["Emission Strength"].default_value = strength
    return mat


MAT = {}
def build_materials():
    MAT["white"] = make_mat("bot_white", (0.86, 0.89, 0.94), 0.75, 0.28)
    MAT["blue"] = make_mat("bot_blue", (0.08, 0.28, 0.85), 0.85, 0.22)
    MAT["dark"] = make_mat("bot_dark", (0.05, 0.06, 0.08), 0.9, 0.4)
    MAT["eye"] = make_mat("bot_eye", (0, 0, 0), 0.0, 0.1,
                          (0.1, 0.95, 1.0), 14.0)
    MAT["core"] = make_mat("bot_core", (0, 0, 0), 0.0, 0.1,
                           (0.2, 0.9, 1.0), 8.0)
    MAT["floor"] = make_mat("lab_floor", (0.045, 0.05, 0.09), 0.9, 0.16)
    MAT["wall"] = make_mat("lab_wall", (0.06, 0.065, 0.11), 0.6, 0.5)
    MAT["panel_dark"] = make_mat("lab_panel", (0.03, 0.035, 0.06), 0.8, 0.35)
    MAT["glow_cyan"] = make_mat("lab_glow_cyan", (0, 0, 0), 0, 0.4,
                                (0.15, 0.9, 1.0), 6.0)
    MAT["glow_purple"] = make_mat("lab_glow_purple", (0, 0, 0), 0, 0.4,
                                  (0.55, 0.25, 1.0), 5.0)
    MAT["sphere"] = make_mat("holo_sphere", (0.2, 0.8, 1.0), 0.0, 0.15,
                             (0.25, 0.85, 1.0), 6.0, alpha=0.42)
    MAT["sphere_wire"] = make_mat("holo_wire", (0, 0, 0), 0, 0.2,
                                  (0.35, 0.95, 1.0), 10.0)
    MAT["particle"] = make_mat("holo_particle", (0, 0, 0), 0, 0.3,
                               (0.4, 0.95, 1.0), 9.0)
    MAT["pedestal"] = make_mat("pedestal", (0.07, 0.08, 0.13), 0.85, 0.3)


# ── object helpers ───────────────────────────────────────────────────────────

def add_sphere(name, radius, loc, mat, scale=(1, 1, 1), seg=20, ring=12):
    bpy.ops.mesh.primitive_uv_sphere_add(              # noqa: F821
        radius=radius, location=loc, segments=seg, ring_count=ring)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = scale
    if mat:
        obj.data.materials.append(mat)
    bpy.ops.object.shade_smooth()                      # noqa: F821
    return obj


def add_cyl(name, radius, depth, loc, mat, rot=(0, 0, 0), verts=14,
            origin_top=False):
    bpy.ops.mesh.primitive_cylinder_add(               # noqa: F821
        radius=radius, depth=depth, location=loc, vertices=verts)
    obj = bpy.context.active_object
    obj.name = name
    obj.rotation_euler = rot
    if origin_top:
        # shift the mesh so the object origin sits at the cylinder's TOP —
        # rotation keyframes then pivot exactly at the joint
        obj.data.transform(Matrix.Translation((0, 0, -depth / 2)))
        obj.location = loc
    if mat:
        obj.data.materials.append(mat)
    bpy.ops.object.shade_smooth()                      # noqa: F821
    return obj


def add_box(name, size, loc, mat, rot=(0, 0, 0), scale=(1, 1, 1)):
    bpy.ops.mesh.primitive_cube_add(size=size, location=loc)  # noqa: F821
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = (scale[0] * size / 2 * 0 + scale[0], scale[1], scale[2]) \
        if False else scale
    obj.rotation_euler = rot
    if mat:
        obj.data.materials.append(mat)
    return obj


def parent_to(child, parent, keep_world=True):
    child.parent = parent
    if keep_world:
        child.matrix_parent_inverse = parent.matrix_world.inverted()


def key(obj, path, frame, value=None):
    if value is not None:
        setattr(obj, path, value)
    obj.keyframe_insert(data_path=path, frame=frame)


def smooth_all(obj):
    """Bezier + auto-clamped handles on every fcurve of this object (smooth
    interpolation, mission requirement)."""
    action = obj.animation_data.action if obj.animation_data else None
    if not action:
        return
    for fcu in action.fcurves:
        for kp in fcu.keyframe_points:
            kp.interpolation = "BEZIER"
            kp.handle_left_type = kp.handle_right_type = "AUTO_CLAMPED"


def smooth_mat_anim(mat):
    tree = mat.node_tree.animation_data
    if tree and tree.action:
        for fcu in tree.action.fcurves:
            for kp in fcu.keyframe_points:
                kp.interpolation = "BEZIER"
                kp.handle_left_type = kp.handle_right_type = "AUTO_CLAMPED"


# ── the robot ────────────────────────────────────────────────────────────────

def limb(name, radius, length, loc, mat, parent=None):
    """Cylinder whose origin is at its TOP so rotations pivot at the joint."""
    seg = add_cyl(name, radius, length, loc, mat, origin_top=True)
    if parent:
        parent_to(seg, parent)
    return seg


def build_robot():
    parts = {}
    # torso — the root everything hangs from; walking/bobbing animates it.
    # Robot forward = +x (face/walk direction); limbs offset along ±y.
    torso = add_sphere("Torso", 0.42, (0, 0, 1.02), MAT["white"],
                       scale=(0.68, 0.46, 0.82), seg=24, ring=16)
    parts["torso"] = torso
    chest = add_sphere("ChestCore", 0.09, (0, 0, 0), MAT["core"],
                       scale=(0.5, 0.7, 0.7))
    parent_to(chest, torso)
    chest.location = (0.185, 0, 0.04)

    # head + face (+x is the robot's forward direction)
    neck = add_cyl("Neck", 0.08, 0.14, (0, 0, 0), MAT["dark"])
    parent_to(neck, torso)
    neck.location = (0, 0, 0.38)
    head = add_sphere("Head", 0.28, (0, 0, 0), MAT["white"],
                      scale=(0.95, 0.9, 0.92), seg=24, ring=16)
    parent_to(head, torso)
    head.location = (0.02, 0, 0.66)
    parts["head"] = head
    cap = add_sphere("HeadCap", 0.285, (0, 0, 0), MAT["blue"],
                     scale=(1.0, 0.95, 0.78), seg=24, ring=16)
    parent_to(cap, head)
    cap.location = (0.0, 0, 0.06)
    plate = add_sphere("FacePlate", 0.26, (0, 0, 0), MAT["dark"],
                       scale=(0.42, 0.7, 0.75), seg=20, ring=12)
    parent_to(plate, head)
    plate.location = (0.19, 0, -0.02)

    eyes = []
    for i, y in enumerate((-0.095, 0.095)):
        eye = add_sphere(f"Eye{i+1}", 0.055, (0, 0, 0), MAT["eye"],
                         seg=14, ring=10)
        parent_to(eye, head)
        eye.location = (0.205, y, 0.05)
        eyes.append(eye)
    parts["eyes"] = eyes

    mouth = add_sphere("Mouth", 0.08, (0, 0, 0), MAT["eye"],
                       scale=(0.5, 1.5, 0.3), seg=14, ring=8)
    parent_to(mouth, head)
    mouth.location = (0.205, 0, -0.11)
    mouth.scale = (0.0, 0.0, 0.0)                    # hidden until the smile
    parts["mouth"] = mouth

    antenna = add_cyl("Antenna", 0.016, 0.16, (0, 0, 0), MAT["dark"],
                      origin_top=True)
    parent_to(antenna, head)
    antenna.location = (0.0, 0, 0.42)
    tip = add_sphere("AntennaTip", 0.045, (0, 0, 0), MAT["glow_purple"],
                     seg=12, ring=8)
    parent_to(tip, antenna)
    tip.location = (0, 0, -0.02)
    parts["antenna"] = antenna

    # arms — articulated chains at the ±y sides: shoulder→upper→elbow→
    # forearm→hand (limb origins sit at the joints so rotations pivot there)
    arms = {}
    for side, y in (("L", 0.30), ("R", -0.30)):
        shoulder = add_sphere(f"Shoulder_{side}", 0.085, (0, 0, 0),
                              MAT["dark"], seg=14, ring=10)
        parent_to(shoulder, torso)
        shoulder.location = (0, y * 1.25, 0.26)
        upper = limb(f"UpperArm_{side}", 0.055, 0.27, (0, 0, 0), MAT["white"],
                     parent=torso)
        upper.location = (0.0, y * 1.32, 0.25)
        elbow = add_sphere(f"Elbow_{side}", 0.068, (0, 0, 0), MAT["dark"],
                           seg=12, ring=8)
        parent_to(elbow, upper)
        elbow.location = (0, 0, -0.27)
        fore = limb(f"Forearm_{side}", 0.048, 0.25, (0, 0, 0), MAT["white"],
                    parent=upper)
        fore.location = (0, 0, -0.27)
        hand = add_sphere(f"Hand_{side}", 0.078, (0, 0, 0), MAT["blue"],
                          seg=14, ring=10)
        parent_to(hand, fore)
        hand.location = (0, 0, -0.27)
        arms[side] = (upper, fore)
        upper.rotation_euler = (0.10, -0.08, 0.0)   # rest pose
    parts["arms"] = arms

    # legs — hip→thigh→knee→shin→foot, separated along ±y
    legs = {}
    for side, y in (("L", 0.15), ("R", -0.15)):
        thigh = limb(f"Thigh_{side}", 0.072, 0.34, (0, 0, 0), MAT["blue"],
                     parent=torso)
        thigh.location = (0, y, -0.30)
        knee = add_sphere(f"Knee_{side}", 0.072, (0, 0, 0), MAT["dark"],
                          seg=12, ring=8)
        parent_to(knee, thigh)
        knee.location = (0, 0, -0.34)
        shin = limb(f"Shin_{side}", 0.062, 0.32, (0, 0, 0), MAT["white"],
                    parent=thigh)
        shin.location = (0, 0, -0.34)
        foot = add_box(f"Foot_{side}", 0.16, (0, 0, 0), MAT["blue"],
                       scale=(1.35, 0.9, 0.5))
        parent_to(foot, shin)
        foot.location = (0.03, 0, -0.34)
        legs[side] = (thigh, shin)
    parts["legs"] = legs
    return parts


# ── the laboratory ───────────────────────────────────────────────────────────

def build_lab():
    # metallic floor with reflections (SSR optional, decided by settings)
    floor = add_box("Floor", 24, (0, 0, -0.06), MAT["floor"],
                    scale=(24 / 2 * 0 + 12, 12, 0.06))
    floor.scale = (12, 12, 0.06)

    # emissive floor strips (light-tracks)
    for i in range(5):
        x = -4.0 + i * 2.2
        add_box(f"FloorStrip{i}", 0.1, (x, 0, -0.005), MAT["glow_cyan"],
                scale=(0.05, 7.0, 0.012))

    # back wall + glowing control panels
    add_box("BackWall", 24, (0, -5.6, 2.4), MAT["wall"],
            scale=(12, 0.15, 2.5))
    for i, (px, col) in enumerate(((-4.2, "glow_cyan"), (-1.6, "glow_purple"),
                                   (1.2, "glow_cyan"), (3.8, "glow_purple"))):
        add_box(f"Panel{i}", 1.5, (px, -5.42, 2.2), MAT["panel_dark"],
                scale=(0.75, 0.06, 1.0))
        add_box(f"PanelScreen{i}", 1.1, (px, -5.34, 2.25), MAT[col],
                rot=(0.18, 0, 0), scale=(0.55, 0.02, 0.62))
        add_box(f"PanelTrim{i}", 0.5, (px, -5.30, 1.35), MAT["glow_cyan"],
                scale=(0.35, 0.02, 0.05))

    # columns for depth
    for cx, cy in ((-5.4, -4.6), (5.2, -4.6), (-5.4, 2.4), (5.2, 2.4)):
        add_box(f"Column{cx}{cy}", 0.6, (cx, cy, 2.2), MAT["wall"],
                scale=(0.45, 0.45, 4.4))

    # ceiling pipes
    add_cyl("PipeA", 0.14, 11, (-2.0, -5.0, 4.1), MAT["panel_dark"],
            rot=(0, math.pi / 2, 0), verts=12)
    add_cyl("PipeB", 0.1, 11, (2.6, -5.1, 4.3), MAT["panel_dark"],
            rot=(0, math.pi / 2, 0), verts=12)

    # consoles near the sphere
    for cx, rz in ((1.4, 0.5), (3.6, -0.6)):
        console = add_box(f"Console{cx}", 1.0, (cx, -1.5, 0.55),
                          MAT["panel_dark"], rot=(0, 0, rz),
                          scale=(0.7, 0.5, 0.55))
        add_box(f"ConsoleScreen{cx}", 0.7, (cx, -1.5, 1.0), MAT["glow_cyan"],
                rot=(0.5, 0, rz), scale=(0.55, 0.36, 0.03))

    # holographic sphere + pedestal
    ped = add_cyl("Pedestal", 0.5, 0.18, (SPHERE_POS[0], SPHERE_POS[1],
                                          0.09), MAT["pedestal"], verts=24)
    ring = add_cyl("PedestalRing", 0.56, 0.04, (SPHERE_POS[0], SPHERE_POS[1],
                                                0.19), MAT["glow_cyan"],
                   verts=28)
    ring.name = "PedestalRing"
    sphere = add_sphere("HoloSphere", 0.55, SPHERE_POS, MAT["sphere"],
                        seg=28, ring=20)
    wire = add_sphere("HoloWire", 0.56, SPHERE_POS, MAT["sphere_wire"],
                      seg=14, ring=7)
    wire.scale = (1.01, 1.01, 1.01)
    return {"sphere": sphere, "wire": wire}


# ── lights + camera ──────────────────────────────────────────────────────────

def build_lights():
    def light(name, kind, loc, energy, color, size=2.0):
        bpy.ops.object.light_add(type=kind, location=loc)   # noqa: F821
        obj = bpy.context.active_object
        obj.name = name
        obj.data.energy = energy
        obj.data.color = color
        if kind == "AREA":
            obj.data.size = size
        return obj

    key = light("KeyLight", "AREA", (-2.2, -3.4, 3.2), 220,
                (0.55, 0.7, 1.0), 3.0)
    key.rotation_euler = (0.7, 0.15, 0.35)
    rim = light("RimLight", "POINT", (4.4, 2.6, 2.8), 130, (0.7, 0.35, 1.0))
    fill = light("FillLight", "POINT", (1.2, -3.2, 1.2), 45, (0.35, 0.55, 1.0))
    holo = light("HoloLight", "POINT", (SPHERE_POS[0], SPHERE_POS[1],
                                        SPHERE_POS[2]), 18,
                 (0.3, 0.85, 1.0))
    return {"holo": holo}


def build_camera():
    cam_data = bpy.data.cameras.new("CinCam")            # noqa: F821
    cam_data.lens = 42
    cam = bpy.data.objects.new("CinCam", cam_data)       # noqa: F821
    bpy.context.scene.collection.objects.link(cam)
    cam.location = (0.9, -2.7, 1.55)
    target = bpy.data.objects.new("CamTarget", None)     # noqa: F821
    bpy.context.scene.collection.objects.link(target)
    target.location = (0.3, 0, 1.5)
    con = cam.constraints.new("TRACK_TO")
    con.target = target
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"
    return cam, target


# ── animation ────────────────────────────────────────────────────────────────

def blink(eyes, t):
    f1, f2, f3 = seconds(t), seconds(t + 0.06), seconds(t + 0.13)
    for e in eyes:
        e.scale = (1, 1, 1)
        e.keyframe_insert("scale", frame=f1)
        e.scale = (1, 1, 0.06)
        e.keyframe_insert("scale", frame=f2)
        e.scale = (1, 1, 1)
        e.keyframe_insert("scale", frame=f3)


def animate_scene1(parts):
    """0–5 s: look around curiously, blink twice, wave at the camera."""
    head = parts["head"]
    # curious looks
    for t, rx, rz in ((0.0, 0.0, 0.0), (1.3, -0.12, 0.45),
                      (2.3, 0.08, -0.4), (3.1, 0.0, 0.0)):
        f = seconds(t)
        head.rotation_euler = (rx, 0, rz)
        head.keyframe_insert("rotation_euler", frame=f)
    blink(parts["eyes"], 1.0)
    blink(parts["eyes"], 3.4)

    # wave with the near-side arm (R, at -y): lift it toward the camera
    upper, fore = parts["arms"]["R"]
    for t, rx in ((2.35, 0.10), (2.7, -1.15), (2.95, -0.95), (3.2, -1.3),
                  (3.45, -0.95), (3.7, -1.3), (3.95, -0.95), (4.2, -1.25),
                  (4.5, 0.10)):
        upper.rotation_euler.x = rx
        upper.rotation_euler.y = -0.08
        upper.keyframe_insert("rotation_euler", frame=seconds(t))
    for t, fx in ((2.7, 0.25), (2.95, -0.35), (3.2, 0.25), (3.45, -0.35),
                  (3.7, 0.25), (3.95, -0.35), (4.2, 0.2), (4.5, 0.0)):
        fore.rotation_euler.x = fx
        fore.keyframe_insert("rotation_euler", frame=seconds(t))


def animate_walk(parts):
    """5–9.6 s: natural walk cycle toward the sphere + body bob. Legs and
    arms swing along the walk axis (+x), i.e. rotations about Y."""
    torso = parts["torso"]
    (thigh_l, shin_l) = parts["legs"]["L"]
    (thigh_r, shin_r) = parts["legs"]["R"]
    (upper_l, fore_l) = parts["arms"]["L"]
    (upper_r, fore_r) = parts["arms"]["R"]

    steps = 6
    step_s = (S2_END - S2_START) / steps          # one step ≈ 0.77 s
    for i in range(int(steps * 4) + 1):
        t = S2_START + i * step_s / 4
        phase = 2 * math.pi * (t - S2_START) / (2 * step_s)
        swing = 0.5 if t <= S2_END else max(0.0, 0.5 * (S2_END - t))
        f = seconds(min(t, S2_END))
        # thighs stride, shins bend during the swing-through
        thigh_l.rotation_euler.y = 0.42 * swing * math.sin(phase)
        thigh_r.rotation_euler.y = 0.42 * swing * math.sin(phase + math.pi)
        shin_l.rotation_euler.y = -0.30 * swing * max(
            0.0, math.sin(phase + 0.9))
        shin_r.rotation_euler.y = -0.30 * swing * max(
            0.0, math.sin(phase + math.pi + 0.9))
        for obj in (thigh_l, thigh_r, shin_l, shin_r):
            obj.keyframe_insert("rotation_euler", frame=f)
        # arms counter-swing
        upper_l.rotation_euler.y = -0.28 * swing * math.sin(phase) - 0.08
        upper_r.rotation_euler.y = -0.28 * swing \
            * math.sin(phase + math.pi) - 0.08
        upper_l.rotation_euler.x = 0.10
        upper_r.rotation_euler.x = 0.10
        fore_l.rotation_euler.x = -0.20 * swing * max(0.0, math.sin(phase))
        fore_r.rotation_euler.x = -0.20 * swing * max(
            0.0, math.sin(phase + math.pi))
        for obj in (upper_l, upper_r, fore_l, fore_r):
            obj.keyframe_insert("rotation_euler", frame=f)
        # body bob: twice per cycle; x walks 0 → ROBOT_STOP_X across the window
        walk_x = min(t - S2_START, S2_END - S2_START) / (S2_END - S2_START) \
            * ROBOT_STOP_X
        bob = 0.035 * abs(math.sin(phase))
        torso.location = (walk_x, 0, 1.02 + bob)
        torso.keyframe_insert("location", frame=f)

    # settle: feet together, arms back to rest at the end of the walk
    f = seconds(10.0)
    torso.location = (ROBOT_STOP_X, 0, 1.02)
    torso.keyframe_insert("location", frame=f)
    for obj, ry in ((thigh_l, 0.0), (thigh_r, 0.0), (shin_l, 0.0),
                    (shin_r, 0.0)):
        obj.rotation_euler.y = ry
        obj.keyframe_insert("rotation_euler", frame=f)
    for obj in (upper_l, upper_r):
        obj.rotation_euler.x = 0.10
        obj.rotation_euler.y = -0.08
        obj.keyframe_insert("rotation_euler", frame=f)
    for obj in (fore_l, fore_r):
        obj.rotation_euler.x = 0.0
        obj.keyframe_insert("rotation_euler", frame=f)


def animate_scene3(parts, holo):
    """10–15 s: reach out, touch → glow + particles, surprise, then smile."""
    torso = parts["torso"]
    upper, fore = parts["arms"]["R"]
    head = parts["head"]
    mouth = parts["mouth"]
    eyes = parts["eyes"]
    sphere, wire = holo["sphere"], holo["wire"]

    # reach forward (+x) with the near arm
    for t, ry in ((10.1, 0.0), (10.8, -1.25), (11.1, -1.45)):
        upper.rotation_euler.y = ry
        upper.rotation_euler.x = 0.12
        upper.keyframe_insert("rotation_euler", frame=seconds(t))
    fore.rotation_euler.x = 0.0
    fore.keyframe_insert("rotation_euler", frame=seconds(10.8))
    fore.rotation_euler.x = 0.15
    fore.keyframe_insert("rotation_euler", frame=seconds(11.1))

    # the sphere: constant slow spin, then a bright pulse + fast spin
    for t, rz in ((0.0, 0.0), (TOUCH_S, 0.35 * TOUCH_S), (15.0, 3.2 + 0.35 * TOUCH_S)):
        sphere.rotation_euler.z = rz
        wire.rotation_euler.z = -rz * 1.4
        sphere.keyframe_insert("rotation_euler", frame=seconds(t))
        wire.keyframe_insert("rotation_euler", frame=seconds(t))
    pulse = seconds(TOUCH_S)
    sphere.scale = (1, 1, 1)
    sphere.keyframe_insert("scale", frame=pulse)
    sphere.scale = (1.32, 1.32, 1.32)
    sphere.keyframe_insert("scale", frame=seconds(TOUCH_S + 0.35))
    sphere.scale = (1.12, 1.12, 1.12)
    sphere.keyframe_insert("scale", frame=seconds(TOUCH_S + 0.9))
    # emission strength via the material node (glow burst)
    for mat, base, peak in ((MAT["sphere"], 6.0, 26.0),
                            (MAT["sphere_wire"], 10.0, 34.0)):
        inp = mat.node_tree.nodes["Principled BSDF"].inputs["Emission Strength"]
        inp.default_value = base
        inp.keyframe_insert("default_value", frame=pulse)
        inp.default_value = peak
        inp.keyframe_insert("default_value", frame=seconds(TOUCH_S + 0.3))
        inp.default_value = base + 4.0
        inp.keyframe_insert("default_value", frame=seconds(15.0))

    # hologram light blooms with the pulse
    holo["light"].data.energy = 18
    holo["light"].data.keyframe_insert("energy", frame=pulse)
    holo["light"].data.energy = 110
    holo["light"].data.keyframe_insert("energy", frame=seconds(TOUCH_S + 0.3))
    holo["light"].data.energy = 55
    holo["light"].data.keyframe_insert("energy", frame=seconds(15.0))

    # surprise: hop back + wide eyes
    f1, f2, f3 = seconds(TOUCH_S + 0.15), seconds(TOUCH_S + 0.4), \
        seconds(TOUCH_S + 0.75)
    torso.location = (ROBOT_STOP_X, 0, 1.02)
    torso.keyframe_insert("location", frame=f1)
    torso.location = (ROBOT_STOP_X - 0.16, 0, 1.12)
    torso.keyframe_insert("location", frame=f2)
    torso.location = (ROBOT_STOP_X - 0.12, 0, 1.02)
    torso.keyframe_insert("location", frame=f3)
    for e in eyes:
        e.scale = (1.3, 1.3, 1.25)
        e.keyframe_insert("scale", frame=seconds(TOUCH_S + 0.3))
        e.scale = (1, 1, 1)
        e.keyframe_insert("scale", frame=seconds(TOUCH_S + 0.9))

    # smile: mouth appears, eyes squint, head tilts, happy bounce
    f = seconds(SMILE_S)
    mouth.scale = (0.0, 0.0, 0.0)
    mouth.keyframe_insert("scale", frame=seconds(SMILE_S - 0.15))
    mouth.scale = (1.0, 1.6, 0.28)
    mouth.keyframe_insert("scale", frame=f)
    for e in eyes:
        e.scale = (1, 1, 0.55)
        e.keyframe_insert("scale", frame=seconds(SMILE_S + 0.1))
    head.rotation_euler = (0.06, 0, 0.18)
    head.keyframe_insert("rotation_euler", frame=seconds(SMILE_S + 0.2))
    torso.location = (ROBOT_STOP_X - 0.12, 0, 1.07)
    torso.keyframe_insert("location", frame=seconds(13.3))
    torso.location = (ROBOT_STOP_X - 0.12, 0, 1.02)
    torso.keyframe_insert("location", frame=seconds(13.7))
    # arm returns to rest
    upper.rotation_euler.y = -0.08
    upper.rotation_euler.x = 0.10
    upper.keyframe_insert("rotation_euler", frame=seconds(12.2))


def build_particles():
    """36 emissive motes burst out of the sphere at the touch and drift up."""
    parts = []
    for i in range(36):
        ang = i * math.pi * 2 / 18
        rad = 0.28 + 0.1 * (i % 3)
        p = add_sphere(f"P{i:02d}", 0.045, SPHERE_POS, MAT["particle"],
                       seg=10, ring=6)
        p.scale = (0, 0, 0)
        t0 = TOUCH_S + 0.2 + (i % 6) * 0.12
        p.keyframe_insert("scale", frame=seconds(t0))
        out = 0.9 + (i % 5) * 0.22
        p.location = (SPHERE_POS[0] + out * math.cos(ang),
                      SPHERE_POS[1] + out * math.sin(ang) * 0.8,
                      SPHERE_POS[2] + 0.4 + (i % 4) * 0.25)
        p.scale = (1, 1, 1)
        p.keyframe_insert("scale", frame=seconds(t0 + 0.4))
        p.keyframe_insert("location", frame=seconds(t0 + 0.4))
        p.location = (SPHERE_POS[0] + out * 1.5 * math.cos(ang),
                      SPHERE_POS[1] + out * 1.5 * math.sin(ang) * 0.8,
                      SPHERE_POS[2] + 1.3 + (i % 5) * 0.3)
        p.keyframe_insert("location", frame=seconds(min(14.4, t0 + 2.6)))
        p.scale = (0.2, 0.2, 0.2)
        p.keyframe_insert("scale", frame=seconds(15.0))
        smooth_all(p)
        parts.append(p)
    return parts


def animate_camera(cam, target, torso):
    """Three cinematic shots: 3/4 close-up → side tracking → push-in."""
    # shot 1: slow dolly-in; the 3/4 angle shows the face and the wave
    for t, loc, tgt in ((0.0, (1.55, -2.85, 1.5), (0.2, 0, 1.5)),
                        (5.0, (1.3, -2.45, 1.45), (0.2, 0, 1.48))):
        cam.location = loc
        cam.keyframe_insert("location", frame=seconds(t))
        target.location = tgt
        target.keyframe_insert("location", frame=seconds(t))
    # shot 2: smooth side tracking while the robot walks
    for t, loc, tgt in ((5.0, (0.75, -3.2, 1.6), (0.1, 0, 1.35)),
                        (9.6, (2.55, -3.05, 1.5), (1.95, 0, 1.3))):
        cam.location = loc
        cam.keyframe_insert("location", frame=seconds(t))
        target.location = tgt
        target.keyframe_insert("location", frame=seconds(t))
    # shot 3: push in on the touch + reaction, end on the smile
    for t, loc, tgt in ((10.0, (2.75, -2.55, 1.5), (2.2, 0, 1.42)),
                        (11.4, (2.5, -1.95, 1.42), (2.3, 0, 1.4)),
                        (15.0, (2.35, -1.75, 1.38), (2.1, 0, 1.32))):
        cam.location = loc
        cam.keyframe_insert("location", frame=seconds(t))
        target.location = tgt
        target.keyframe_insert("location", frame=seconds(t))
    smooth_all(cam)
    smooth_all(target)


def set_render_settings(res_w, res_h, fps, ssr):
    sc = bpy.context.scene
    sc.render.engine = "BLENDER_EEVEE"
    sc.render.resolution_x = res_w
    sc.render.resolution_y = res_h
    sc.render.fps = fps
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    sc.eevee.taa_render_samples = 16
    sc.eevee.use_gtao = False
    sc.eevee.use_ssr = bool(ssr)
    sc.eevee.use_ssr_refraction = False
    sc.eevee.use_bloom = True
    sc.eevee.bloom_intensity = 0.07
    sc.eevee.bloom_threshold = 0.8
    sc.eevee.use_volumetric_lights = False
    sc.eevee.shadow_cube_size = "512"
    sc.view_settings.view_transform = "Filmic"
    sc.frame_start = 1
    sc.frame_end = END_FRAME
    sc.render.filepath = "//frames/frame_"
    sc.world = bpy.data.worlds.new("LabWorld")           # noqa: F821
    sc.world.use_nodes = True
    bg = sc.world.node_tree.nodes.get("Background")
    bg.inputs[0].default_value = (0.006, 0.007, 0.018, 1.0)
    bg.inputs[1].default_value = 1.0


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    opts = {"--out": "robot.blend", "--ssr": "1", "--res": "720x1280",
            "--fps": "24", "--duration": "15"}
    for i, a in enumerate(argv):
        if a in opts and i + 1 < len(argv):
            opts[a] = argv[i + 1]
    ssr = opts["--ssr"] not in ("0", "false", "False")
    res_w, res_h = map(int, opts["--res"].split("x"))
    fps = int(opts["--fps"])
    duration = int(opts["--duration"])
    end_frame = fps * duration

    bpy.ops.wm.read_factory_settings(use_empty=True)
    build_materials()
    parts = build_robot()
    holo = build_lab()
    lights = build_lights()
    cam, target = build_camera()
    bpy.context.scene.camera = cam

    animate_scene1(parts)
    animate_walk(parts)
    animate_scene3(parts, {"sphere": holo["sphere"], "wire": holo["wire"],
                           "light": lights["holo"]})
    build_particles()
    animate_camera(cam, target, parts["torso"])

    for obj in bpy.data.objects:
        smooth_all(obj)
    for mname in ("sphere", "sphere_wire"):
        smooth_mat_anim(MAT[mname])

    set_render_settings(res_w, res_h, fps, ssr)
    bpy.context.scene.frame_end = end_frame
    bpy.ops.wm.save_as_mainfile(filepath=opts["--out"], compress=True)
    print("SCENE_BUILT", opts["--out"], "frames", end_frame)


main()
