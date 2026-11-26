# build_avatar_scene.py — "RIO", an ORIGINAL friendly futuristic AI
# assistant avatar (Blender 3.6, bpy).
#
# Builds a single hero shot for a vertical video: a hovering assistant
# robot that idles, bobs, turns its head, blinks, and TALKS (mouth
# animation during the narration window). Everything is original
# procedural geometry — no imported models, no copyrighted character.
#
#   blender -b --factory-startup --python build_avatar_scene.py -- \
#       --out /path/avatar.blend [--duration 18] [--fps 24] \
#       [--talk-start 1.5] [--talk-end 16.0] [--res 720x1280]
#
# Light geometry + Eevee 16 samples — proven to render headless on this
# 2C/4T laptop with Intel HD 4400 (the robot pipeline completed in ~7 min).
from __future__ import annotations

import math
import sys

import bpy                                          # noqa: F401

FPS = 24
DURATION_S = 18.0

# ── helpers (same proven patterns as build_robot_scene.py) ──────────────────


def seconds(t: float) -> int:
    return max(1, int(round(t * FPS)) + 1)


def make_mat(name, base=(0.9, 0.9, 0.92), metallic=0.85, rough=0.3,
             emission=None, strength=0.0, alpha=1.0):
    mat = bpy.data.materials.new(name)              # noqa: F821
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
    MAT["shell"] = make_mat("rio_shell", (0.88, 0.91, 0.95), 0.7, 0.25)
    MAT["accent"] = make_mat("rio_accent", (0.05, 0.45, 0.95), 0.8, 0.2)
    MAT["dark"] = make_mat("rio_dark", (0.04, 0.05, 0.08), 0.85, 0.35)
    MAT["visor"] = make_mat("rio_visor", (0.01, 0.02, 0.04), 0.4, 0.08)
    MAT["glow"] = make_mat("rio_glow", (0, 0, 0), 0.0, 0.1,
                           (0.15, 0.95, 1.0), 12.0)
    MAT["glow_soft"] = make_mat("rio_glow_soft", (0, 0, 0), 0.0, 0.3,
                                (0.2, 0.85, 1.0), 5.0)
    MAT["floor"] = make_mat("stage_floor", (0.05, 0.055, 0.1), 0.85, 0.2)
    MAT["ring"] = make_mat("stage_ring", (0, 0, 0), 0.0, 0.3,
                           (0.3, 0.9, 1.0), 7.0)


def add_sphere(name, radius, loc, mat, scale=(1, 1, 1), seg=20, ring=12):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=seg, ring_count=ring,
                                         radius=radius, location=loc)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = scale
    if mat:
        obj.data.materials.append(mat)
    return obj


def add_cyl(name, radius, depth, loc, mat, rot=(0, 0, 0), verts=16,
            scale=(1, 1, 1)):
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=radius,
                                        depth=depth, location=loc,
                                        rotation=rot)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = scale
    if mat:
        obj.data.materials.append(mat)
    return obj


def add_torus(name, radius, minor, loc, mat, rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_torus_add(major_radius=radius,
                                     minor_radius=minor, location=loc,
                                     rotation=rot)
    obj = bpy.context.active_object
    obj.name = name
    if mat:
        obj.data.materials.append(mat)
    return obj


def parent_to(child, parent, keep_world=True):
    child.parent = parent
    if keep_world:
        child.matrix_parent_inverse = parent.matrix_world.inverted()


def smooth_all(obj):
    for poly in obj.data.polygons:
        poly.use_smooth = True


def limb(name, radius, length, loc, mat, parent=None):
    parts = []
    upper = add_cyl(f"{name}_upper", radius, length, loc, mat)
    parts.append(upper)
    if parent:
        parent_to(upper, parent)
    return parts


# ── the avatar ───────────────────────────────────────────────────────────────

def build_avatar():
    """Original hovering assistant robot: head/visor/eyes/mouth, torso with
    a glowing core, two swaying arms, hover ring. Returns named parts."""
    root = bpy.data.objects.new("RIO_root", None)   # noqa: F821
    bpy.context.collection.objects.link(root)

    # torso: rounded capsule body
    torso = add_sphere("Torso", 0.42, (0, 0, 1.05), MAT["shell"],
                       scale=(1.0, 0.78, 1.15))
    chest = add_sphere("Chest_plate", 0.3, (0, -0.3, 1.08), MAT["accent"],
                       scale=(1.0, 0.42, 0.9))
    parent_to(chest, torso)
    core = add_cyl("Core", 0.09, 0.05, (0, -0.46, 1.1), MAT["glow"],
                   rot=(math.radians(90), 0, 0))
    parent_to(core, torso)
    belt = add_torus("Belt", 0.34, 0.045, (0, 0, 0.72), MAT["dark"])
    parent_to(belt, torso)
    parent_to(torso, root)

    # neck + head
    neck = add_cyl("Neck", 0.1, 0.16, (0, 0, 1.62), MAT["dark"])
    parent_to(neck, torso)
    head = add_sphere("Head", 0.34, (0, 0, 1.95), MAT["shell"],
                      scale=(1.0, 0.92, 0.94))
    parent_to(head, neck)
    visor = add_sphere("Visor", 0.27, (0, -0.12, 1.97), MAT["visor"],
                       scale=(1.0, 0.5, 0.72))
    parent_to(visor, head)

    eyes = []
    for side, x in (("L", -0.1), ("R", 0.1)):
        eye = add_sphere(f"Eye_{side}", 0.055, (x, -0.24, 2.02), MAT["glow"],
                         seg=12, ring=8)
        parent_to(eye, head)
        eyes.append(eye)

    # mouth: a glowing bar that TALKS (scale pulses during narration)
    mouth = add_sphere("Mouth", 0.075, (0, -0.23, 1.85), MAT["glow_soft"],
                       scale=(1.6, 0.35, 0.35), seg=12, ring=8)
    parent_to(mouth, head)
    ear_l = add_cyl("Ear_L", 0.055, 0.05, (-0.33, 0, 1.97), MAT["accent"],
                    rot=(0, math.radians(90), 0))
    ear_r = add_cyl("Ear_R", 0.055, 0.05, (0.33, 0, 1.97), MAT["accent"],
                    rot=(0, math.radians(90), 0))
    parent_to(ear_l, head)
    parent_to(ear_r, head)
    antenna = add_cyl("Antenna", 0.015, 0.22, (0, 0, 2.3), MAT["dark"])
    tip = add_sphere("Antenna_tip", 0.045, (0, 0, 2.44), MAT["glow"], seg=10,
                     ring=6)
    parent_to(antenna, head)
    parent_to(tip, antenna)

    # arms: shoulder pivot empties + simple limbs, gentle sway
    arms = {}
    for side, x in (("L", -0.5), ("R", 0.5)):
        pivot = bpy.data.objects.new(f"Arm_pivot_{side}", None)  # noqa: F821
        bpy.context.collection.objects.link(pivot)
        pivot.location = (x, 0, 1.28)
        parent_to(pivot, root)
        shoulder = add_sphere(f"Shoulder_{side}", 0.1, (x, 0, 1.28),
                              MAT["accent"])
        parent_to(shoulder, pivot)
        upper = add_cyl(f"Upperarm_{side}", 0.07, 0.34, (x, 0, 1.05),
                        MAT["shell"])
        parent_to(upper, pivot)
        elbow = add_sphere(f"Elbow_{side}", 0.08, (x, 0, 0.9), MAT["dark"])
        parent_to(elbow, pivot)
        fore = add_cyl(f"Forearm_{side}", 0.06, 0.3, (x, 0, 0.72),
                       MAT["shell"])
        parent_to(fore, pivot)
        hand = add_sphere(f"Hand_{side}", 0.09, (x, 0, 0.52), MAT["glow_soft"],
                          seg=12, ring=8)
        parent_to(hand, pivot)
        arms[side] = pivot

    # hover ring under the avatar
    ring = add_torus("Hover_ring", 0.42, 0.035, (0, 0, 0.18), MAT["ring"])
    parent_to(ring, root)
    glow_disc = add_cyl("Hover_glow", 0.3, 0.01, (0, 0, 0.06),
                        MAT["glow_soft"])
    parent_to(glow_disc, root)

    return {"root": root, "torso": torso, "head": head, "eyes": eyes,
            "mouth": mouth, "arms": arms, "ring": ring, "antenna_tip": tip}


def build_stage():
    """Minimal futuristic stage: dark backdrop wall + reflective-ish floor."""
    floor = add_cyl("Floor", 3.2, 0.08, (0, 0, -0.04), MAT["floor"], verts=48)
    wall = add_cyl("Backdrop", 3.4, 0.06, (0, 2.6, 2.2), MAT["floor"],
                   rot=(math.radians(90), 0, 0), verts=48)
    ring = add_torus("Stage_ring", 1.1, 0.02, (0, 0, 0.02), MAT["glow_soft"])
    return {"floor": floor, "wall": wall, "ring": ring}


def build_lights():
    def light(name, kind, loc, energy, color, size=2.0):
        data = bpy.data.lights.new(name, kind)   # noqa: F821
        data.energy = energy
        data.color = color
        if kind == "AREA":
            data.size = size
        obj = bpy.data.objects.new(name, data)   # noqa: F821
        obj.location = loc
        bpy.context.collection.objects.link(obj)
        return obj

    key = light("Key", "AREA", (-1.8, -2.2, 3.0), 220, (1.0, 0.98, 0.95),
                2.4)
    fill = light("Fill", "AREA", (2.2, -1.8, 1.8), 90, (0.6, 0.8, 1.0), 2.6)
    rim = light("Rim", "AREA", (0.6, 2.2, 2.6), 160, (0.2, 0.7, 1.0), 2.0)
    glow = light("Core_glow", "POINT", (0, -0.7, 1.1), 18, (0.2, 0.9, 1.0))
    return {"key": key, "fill": fill, "rim": rim, "glow": glow}


def build_camera():
    cam_data = bpy.data.cameras.new("Cam")          # noqa: F821
    cam_data.lens = 42
    cam = bpy.data.objects.new("Cam", cam_data)     # noqa: F821
    cam.location = (0, -2.9, 1.5)
    cam.rotation_euler = (math.radians(84), 0, 0)
    bpy.context.collection.objects.link(cam)
    target = bpy.data.objects.new("Cam_target", None)  # noqa: F821
    target.location = (0, 0, 1.5)
    bpy.context.collection.objects.link(target)
    track = cam.constraints.new("TRACK_TO")
    track.target = target
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"
    return cam, target


# ── animation ────────────────────────────────────────────────────────────────

def animate_avatar(parts, duration, talk_start, talk_end):
    """Idle bob + head turn + blinks + arm sway + TALK mouth animation —
    all real keyframes, sampled every few frames for smooth curves."""
    fps = FPS
    end_f = seconds(duration)
    root, head = parts["root"], parts["head"]
    mouth = parts["mouth"]
    eyes = parts["eyes"]
    arms = parts["arms"]
    ring = parts["ring"]

    # idle bob on the root (z) + gentle sway (rotation z)
    step = 4
    t = 0.0
    while t <= duration + 0.2:
        f = seconds(t)
        root.location.z = 0.05 * math.sin(2 * math.pi * t / 2.6)
        root.rotation_euler.z = math.radians(1.6 * math.sin(
            2 * math.pi * t / 4.4))
        root.keyframe_insert("location", frame=f)
        root.keyframe_insert("rotation_euler", frame=f)
        ring.scale = (1.0 + 0.04 * math.sin(2 * math.pi * t / 2.6 + 1.0),) * 3
        ring.keyframe_insert("scale", frame=f)
        t += step / fps
    for fc in root.animation_data.action.fcurves:
        for kp in fc.keyframe_points:
            kp.interpolation = "SINE"
            kp.easing = "AUTO"

    # head: slow curious turns + tiny nods
    t = 0.0
    while t <= duration + 0.2:
        f = seconds(t)
        head.rotation_euler.z = math.radians(8.5 * math.sin(
            2 * math.pi * t / 7.5))
        head.rotation_euler.x = math.radians(2.2 * math.sin(
            2 * math.pi * t / 5.1 + 0.8))
        head.keyframe_insert("rotation_euler", frame=f)
        t += 0.5

    # arm sway (opposite phases — natural idle)
    for side, pivot in arms.items():
        phase = 0.0 if side == "L" else math.pi
        t = 0.0
        while t <= duration + 0.2:
            f = seconds(t)
            pivot.rotation_euler.x = math.radians(4.5 * math.sin(
                2 * math.pi * t / 3.4 + phase))
            pivot.rotation_euler.z = math.radians(2.5 * math.sin(
                2 * math.pi * t / 5.2 + phase))
            pivot.keyframe_insert("rotation_euler", frame=f)
            t += step / fps

    # blinks: ~every 3.2 s, 0.22 s long
    blink_times = []
    bt = 2.1
    while bt < duration:
        blink_times.append(bt)
        bt += 3.2 + 0.4 * ((bt * 7) % 3) / 3
    for eye in eyes:
        for i, bt in enumerate(blink_times):
            for tt, sc in ((bt, 1.0), (bt + 0.09, 0.08),
                           (bt + 0.2, 1.0)):
                eye.scale.y = sc
                eye.keyframe_insert("scale", frame=seconds(tt))
        smooth_all(eye)

    # TALK: mouth opens/closes ~4.5 Hz during the narration window; closed
    # before/after. Amplitude tapers at the edges of each open phase.
    mt = 0.0
    mstep = 1 / 12.0                      # 12 keys per second = smooth talk
    while mt <= duration + 0.1:
        f = seconds(mt)
        if talk_start <= mt <= talk_end:
            phase = 2 * math.pi * 4.5 * (mt - talk_start)
            open_amt = 0.5 + 0.5 * math.sin(phase)
            edge = min(1.0, (mt - talk_start) / 0.25,
                       (talk_end - mt) / 0.25)
            mz = 0.15 + 0.75 * open_amt * max(0.0, edge)
            mx = 1.6 + 0.25 * open_amt
        else:
            mz, mx = 0.12, 1.6
        mouth.scale = (mx, 0.35, mz)
        mouth.keyframe_insert("scale", frame=f)
        mt += mstep
    smooth_all(mouth)

    # antenna tip pulses with the "voice"
    tip = parts["antenna_tip"]
    mt = 0.0
    while mt <= duration + 0.1:
        f = seconds(mt)
        talking = talk_start <= mt <= talk_end
        s = 1.0 + (0.5 * (0.5 + 0.5 * math.sin(
            2 * math.pi * 4.5 * (mt - talk_start))) if talking else 0.0)
        tip.scale = (s, s, s)
        tip.keyframe_insert("scale", frame=f)
        mt += 0.2


def animate_camera(cam, target, duration):
    """Slow push-in for a living shot."""
    for t, loc in ((0.0, (0, -3.05, 1.52)), (duration * 0.5, (0, -2.95, 1.5)),
                   (duration, (0, -2.8, 1.47))):
        cam.location = loc
        cam.keyframe_insert("location", frame=seconds(t))
    target.location = (0, 0, 1.5)
    target.keyframe_insert("location", frame=seconds(duration))
    for fc in cam.animation_data.action.fcurves:
        for kp in fc.keyframe_points:
            kp.interpolation = "SINE"


def set_render_settings(res_w, res_h, fps, duration):
    sc = bpy.context.scene
    sc.render.engine = "BLENDER_EEVEE"
    sc.render.resolution_x = res_w
    sc.render.resolution_y = res_h
    sc.render.fps = fps
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    sc.eevee.taa_render_samples = 16
    sc.eevee.use_gtao = False
    sc.eevee.use_ssr = False
    sc.eevee.use_ssr_refraction = False
    sc.eevee.use_bloom = True
    sc.eevee.bloom_intensity = 0.06
    sc.eevee.bloom_threshold = 0.8
    sc.eevee.use_volumetric_lights = False
    sc.eevee.shadow_cube_size = "512"
    sc.view_settings.view_transform = "Filmic"
    sc.frame_start = 1
    sc.frame_end = int(fps * duration)
    sc.render.filepath = "//frames/frame_"
    sc.world = bpy.data.worlds.new("StageWorld")     # noqa: F821
    sc.world.use_nodes = True
    bg = sc.world.node_tree.nodes.get("Background")
    bg.inputs[0].default_value = (0.005, 0.006, 0.016, 1.0)
    bg.inputs[1].default_value = 1.0


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    opts = {"--out": "avatar.blend", "--res": "720x1280", "--fps": "24",
            "--duration": "18", "--talk-start": "1.2", "--talk-end": "16.0"}
    for i, a in enumerate(argv):
        if a in opts and i + 1 < len(argv):
            opts[a] = argv[i + 1]
    res_w, res_h = map(int, opts["--res"].split("x"))
    fps = int(opts["--fps"])
    duration = float(opts["--duration"])
    talk_start = float(opts["--talk-start"])
    talk_end = float(opts["--talk-end"])

    bpy.ops.wm.read_factory_settings(use_empty=True)
    build_materials()
    parts = build_avatar()
    build_stage()
    build_lights()
    cam, target = build_camera()
    bpy.context.scene.camera = cam

    animate_avatar(parts, duration, talk_start, talk_end)
    animate_camera(cam, target, duration)
    for obj in bpy.data.objects:
        if obj.type == "MESH":
            smooth_all(obj)

    set_render_settings(res_w, res_h, fps, duration)
    bpy.ops.wm.save_as_mainfile(filepath=opts["--out"], compress=True)
    print("SCENE_BUILT", opts["--out"], "frames", int(fps * duration))


main()
