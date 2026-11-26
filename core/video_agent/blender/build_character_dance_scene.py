# build_character_dance_scene.py — RIO Character Animation pipeline.
#
# Builds an ORIGINAL playground dance scene with TWO articulated
# low-poly characters (the permanent cast: Psychology Expert + Little
# Boy, stylized from the character design sheet) and a choreographed,
# beat-timed dance timeline with a directed multi-shot camera.
#
#   blender -b --factory-startup --python build_character_dance_scene.py -- \
#       --out dance.blend --duration 18 --fps 24 --beats 100
#
# HONEST CAPABILITY (also reported by the pipeline manifest):
#   * each character is an articulated JOINT-PIVOT rig: parented pivot
#     empties at shoulders/elbows/hips/knees/neck with per-limb keyframes
#     and per-limb phase offsets — NOT an armature/skinning system, and
#     NOT viseme lip-sync (the mouth is a simple talk scale)
#   * movements are real body-part rotations + root translation, never
#     whole-object scale/rotation fakes
from __future__ import annotations

import math
import sys

import bpy

FPS = 24
BEAT = 0.6                                   # 100 BPM — matches the music
RES_W, RES_H = 720, 1280                     # 9:16 portrait, 720p


def f(t: float) -> int:
    return max(1, int(round(t * FPS)) + 1)


def make_mat(name, base=(0.9, 0.9, 0.92), metallic=0.1, rough=0.5,
             emission=None, strength=0.0):
    mat = bpy.data.materials.new(name)                 # noqa: F821
    mat.use_nodes = True
    node = mat.node_tree.nodes.get("Principled BSDF")
    node.inputs["Base Color"].default_value = (*base, 1.0)
    node.inputs["Metallic"].default_value = metallic
    node.inputs["Roughness"].default_value = rough
    if emission is not None:
        node.inputs["Emission"].default_value = (*emission, 1.0)
        node.inputs["Emission Strength"].default_value = strength
    return mat


MAT = {}


def build_materials():
    MAT["expert_skin"] = make_mat("ex_skin", (0.90, 0.64, 0.42))
    MAT["expert_hair"] = make_mat("ex_hair", (0.09, 0.06, 0.04), rough=0.92,
                                  metallic=0.0)
    MAT["expert_blazer"] = make_mat("ex_blazer", (0.07, 0.13, 0.32), rough=0.55)
    MAT["expert_vest"] = make_mat("ex_vest", (0.45, 0.3, 0.16), rough=0.6)
    MAT["expert_trousers"] = make_mat("ex_trousers", (0.12, 0.12, 0.16))
    MAT["boy_skin"] = make_mat("boy_skin", (0.95, 0.70, 0.48))
    MAT["boy_hair"] = make_mat("boy_hair", (0.10, 0.065, 0.04), rough=0.92,
                               metallic=0.0)
    MAT["boy_hoodie"] = make_mat("boy_hoodie", (0.95, 0.72, 0.1), rough=0.6)
    MAT["boy_bottoms"] = make_mat("boy_bottoms", (0.2, 0.35, 0.75))
    MAT["shoe"] = make_mat("shoe", (0.9, 0.9, 0.92))
    MAT["eye"] = make_mat("eye", (0.02, 0.02, 0.03), rough=0.2)
    MAT["eye_white"] = make_mat("eye_white", (0.95, 0.95, 0.95))
    MAT["mouth"] = make_mat("mouth", (0.45, 0.1, 0.1))
    MAT["mouth_dark"] = make_mat("mouth_dark", (0.12, 0.05, 0.06),
                                rough=0.9)
    # a near-black frame: at rough 0.1 the area lights blew it out to light
    # grey and the "black round glasses" read as silver wire
    MAT["glass"] = make_mat("glass", (0.02, 0.02, 0.03), rough=0.38)
    MAT["grass"] = make_mat("grass", (0.24, 0.55, 0.25), rough=0.8)
    MAT["path"] = make_mat("path", (0.8, 0.72, 0.5), rough=0.8)
    MAT["sky"] = make_mat("sky", (0.34, 0.58, 0.92), rough=1.0,
                          emission=(0.34, 0.58, 0.92), strength=0.55)
    MAT["flower_r"] = make_mat("flower_r", (0.95, 0.3, 0.35), rough=0.6)
    MAT["flower_y"] = make_mat("flower_y", (0.98, 0.85, 0.2), rough=0.6)
    MAT["flower_p"] = make_mat("flower_p", (0.85, 0.45, 0.85), rough=0.6)
    MAT["flower_w"] = make_mat("flower_w", (0.97, 0.97, 0.99), rough=0.6)
    MAT["cloud"] = make_mat("cloud", (0.95, 0.97, 1.0), rough=1.0)
    MAT["wood"] = make_mat("wood", (0.75, 0.5, 0.25), rough=0.7)
    MAT["slide"] = make_mat("slide", (0.95, 0.3, 0.3), rough=0.5)
    MAT["swing"] = make_mat("swing", (0.95, 0.6, 0.15), rough=0.5)
    MAT["ball"] = make_mat("ball", (0.95, 0.25, 0.45), rough=0.4)
    MAT["leaf"] = make_mat("leaf", (0.2, 0.55, 0.2), rough=0.8)
    MAT["fence"] = make_mat("fence", (0.9, 0.85, 0.7), rough=0.7)


def add_sphere(name, radius, loc, mat, scale=(1, 1, 1), seg=16, ring=10):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=seg, ring_count=ring,
                                         radius=radius, location=loc)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = scale
    if mat:
        obj.data.materials.append(mat)
    return obj


def add_cube(name, size, loc, mat, rot=(0, 0, 0), scale=(1, 1, 1)):
    bpy.ops.mesh.primitive_cube_add(size=size, location=loc, rotation=rot)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = scale
    if mat:
        obj.data.materials.append(mat)
    return obj


def add_cyl(name, radius, depth, loc, mat, rot=(0, 0, 0), verts=12):
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=radius,
                                        depth=depth, location=loc,
                                        rotation=rot)
    obj = bpy.context.active_object
    obj.name = name
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


def parent_to(child, parent):
    # depsgraph MUST be current — a stale parent.matrix_world bakes the
    # build-time offset into matrix_parent_inverse and the child detaches
    # from the moving root later (the "invisible walking character" bug)
    bpy.context.view_layer.update()
    child.parent = parent
    child.matrix_parent_inverse = parent.matrix_world.inverted()


def pivot(name, loc, parent):
    empty = bpy.data.objects.new(name, None)       # noqa: F821
    empty.location = loc
    bpy.context.collection.objects.link(empty)
    if parent:
        parent_to(empty, parent)
    return empty


def smooth(obj):
    if obj.animation_data and obj.animation_data.action:
        for fc in obj.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "SINE"


# ── the articulated character rig ────────────────────────────────────────────

def build_character(name, cfg, x, y=0.0):
    """Joint-pivot rig. cfg: skin, hair, hair_style('wavy'|'short'), top,
    top2, legs, legs2, glasses(bool), beard(bool), shoe, scale.

    The character is BUILT AT THE ORIGIN (all part coordinates are
    root-relative by design) and the root is moved to (x, y) at the end —
    building directly at x would bake the offset into every
    matrix_parent_inverse and the body would detach from its moving root
    (the invisible-character bug)."""
    s = cfg.get("scale", 1.0)
    root = pivot(f"{name}_root", (0.0, 0.0, 0.0), None)
    hips = pivot(f"{name}_hips", (0, 0, 0.52 * s), root)

    pelvis = add_cube(f"{name}_pelvis", 0.3, (0, 0, 0.60 * s),
                      MAT[cfg["legs"]], scale=(0.62 * s, 0.42 * s, 0.42 * s))
    parent_to(pelvis, hips)
    spine = pivot(f"{name}_spine", (0, 0, 0.6 * s), hips)
    torso = add_cube(f"{name}_torso", 0.34, (0, 0, 0.80 * s),
                     MAT[cfg["top"]],
                     scale=(0.72 * s, 0.44 * s,
                            cfg.get("torso_h", 0.78) * s))
    parent_to(torso, spine)
    if cfg.get("top2"):
        # flush with the torso front (1.5 mm proud): a waistcoat is a colour
        # region on the chest, not a box glued to it — 10 mm of stand-off
        # cast a shadow that made it read as cardboard
        vest = add_cube(f"{name}_vest", 0.3, (0, -0.0495 * s, 0.775 * s),
                        MAT[cfg["top2"]],
                        scale=(0.70 * s, 0.18 * s, 0.72 * s))
        parent_to(vest, spine)
        if cfg.get("shirt"):
            for side, sx in (("L", -1), ("R", 1)):
                col = add_cube(f"{name}_collar_{side}", 0.05,
                               (sx * 0.030 * s, -0.072 * s, 0.902 * s),
                               MAT[cfg["shirt"]],
                               rot=(0, 0, math.radians(26 * sx)),
                               scale=(0.90, 0.5, 0.42))
                parent_to(col, spine)

    # a real neck mesh: without it the head floats above the collar
    neck_mesh = add_cyl(f"{name}_neckmesh", 0.058 * s, 0.16 * s,
                        (0, 0, 0.95 * s), MAT[cfg["skin"]], verts=10)
    parent_to(neck_mesh, spine)
    neck = pivot(f"{name}_neck", (0, 0, 1.02 * s), spine)
    head = add_sphere(f"{name}_head", 0.17 * s, (0, 0, 1.22 * s),
                      MAT[cfg["skin"]], scale=(0.92, 0.9, 1.0))
    parent_to(head, neck)
    hair = add_sphere(f"{name}_hair", 0.175 * s, (0, 0.02 * s, 1.26 * s),
                      MAT[cfg["hair"]],
                      scale=(0.95, 0.88, cfg.get("hair_h", 0.8)))
    parent_to(hair, head)
    if cfg.get("hair_style") == "wavy":
        # a wavy mop: a row of curls across the top plus a back row. Two
        # side curls alone read as "bun ears", not as the sheet's wavy hair.
        # the forehead surface at this height is y = -0.12, so curls parked
        # at y = -0.03 were INSIDE the skull and the front row was invisible
        # — it read as a bright bald notch between the hairline and the brow.
        for i, dx in enumerate((-0.10, -0.05, 0.0, 0.05, 0.10)):
            curl = add_sphere(f"{name}_haircurl{i}", 0.044 * s,
                              (dx * s, -0.100 * s, 1.358 * s),
                              MAT[cfg["hair"]], seg=10, ring=6)
            parent_to(curl, head)
        for i, dx in enumerate((-0.075, 0.0, 0.075)):
            curl = add_sphere(f"{name}_hairback{i}", 0.052 * s,
                              (dx * s, 0.085 * s, 1.31 * s),
                              MAT[cfg["hair"]], seg=10, ring=6)
            parent_to(curl, head)
    for side, sx in (("L", -1), ("R", 1)):
        ear = add_sphere(f"{name}_ear_{side}", 0.038 * s,
                         (sx * 0.152 * s, 0.005 * s, 1.215 * s),
                         MAT[cfg["skin"]], scale=(0.45, 0.85, 1.0),
                         seg=8, ring=6)
        parent_to(ear, head)
    if cfg.get("hair_style") == "short":
        for i, (dx, dz, w) in enumerate(((-0.075, 1.352, 1.0),
                                         (-0.025, 1.362, 0.9),
                                         (0.03, 1.352, 1.0))):
            lock = add_sphere(f"{name}_fringe{i}", 0.05 * s,
                              (dx * s, -0.115 * s, dz * s),
                              MAT[cfg["hair"]],
                              scale=(w, 0.5, 0.42), seg=10, ring=6)
            parent_to(lock, head)
    eyes = []
    brows: list = []
    for side, dx in (("L", -0.062 * s), ("R", 0.062 * s)):
        white = add_sphere(f"{name}_eyeW_{side}", 0.035 * s,
                           (dx, -0.14 * s, 1.24 * s), MAT["eye_white"],
                           seg=10, ring=8)
        pupil = add_sphere(f"{name}_eye_{side}", 0.018 * s,
                           (dx, -0.165 * s, 1.24 * s), MAT["eye"],
                           seg=8, ring=6)
        parent_to(white, head)
        parent_to(pupil, head)
        eyes.append((white, pupil))
        # the glasses lens ring tops out at z = 1.296; a brow lower than
        # ~1.33 merged with it into one thick dark band and the eyes read
        # as half-closed. Keep the brow clear of the lens and hug the skull
        # (face surface here is y = -0.127).
        brow = add_cube(f"{name}_brow_{side}", 0.05,
                        (dx, -0.128 * s, 1.308 * s), MAT[cfg["hair"]],
                        scale=(1.20, 0.40, 0.26))
        parent_to(brow, head)
        brows.append(brow)
        # NO eyelid mesh. A separate skin cap has to sit in FRONT of the
        # eyeball to be visible at all, which reads as a puffy, sleepy lid
        # and hides the brow. Blinks squash the eyeball itself instead
        # (see apply_visemes in build_lesson_scene), so no extra geometry.
    mouth = add_sphere(f"{name}_mouth", 0.032 * s,
                       (0, -0.130 * s, 1.113 * s), MAT["mouth"],
                       scale=(1.7, 0.4, 0.5), seg=8, ring=6)
    parent_to(mouth, head)
    # dark cavity behind the lips: without it an open mouth is a red blob
    cavity = add_sphere(f"{name}_mouthcavity", 0.031 * s,
                        (0, -0.141 * s, 1.113 * s), MAT["mouth_dark"],
                        scale=(1.5, 0.35, 0.3), seg=8, ring=6)
    parent_to(cavity, head)
    nose = add_sphere(f"{name}_nose", 0.022 * s, (0, -0.17 * s, 1.19 * s),
                      MAT[cfg["skin"]], seg=8, ring=6)
    parent_to(nose, head)
    if cfg.get("glasses"):
        for side, dx in (("L", -0.062 * s), ("R", 0.062 * s)):
            # the eyeball front is y = -0.175; a lens closer than that to
            # the face has the eye poke through its hole and the upper arc
            # cuts the white, which reads as a sleepy eye. Sit the ring
            # clear in front of the eye and wide enough to frame it.
            # primitive_torus_add lays the ring flat in the XY plane, so an
            # unrotated lens is seen EDGE-ON from the camera and reads as a
            # dark bar across the eyes. Stand it up to face the lens axis.
            lens = add_torus(f"{name}_glass_{side}", 0.050 * s, 0.007 * s,
                             (dx, -0.186 * s, 1.238 * s), MAT["glass"],
                             rot=(math.radians(90), 0, 0))
            parent_to(lens, head)
        bridge = add_cube(f"{name}_glassbridge", 0.05,
                          (0, -0.186 * s, 1.238 * s), MAT["glass"],
                          scale=(1.0, 0.4, 0.25))
        parent_to(bridge, head)
    if cfg.get("beard"):
        # A beard is a COLOUR PATCH on the face, not a ball in front of it.
        # The sphere is squashed until its surface sits ~5 mm proud of the
        # head, so the mouth stays visible (the old 0.15 m ball covered the
        # lips completely and no amount of lip-sync would have shown).
        beard = add_sphere(f"{name}_beard", 0.16 * s, (0, 0.0, 1.115 * s),
                           MAT[cfg["hair"]],
                           scale=(1.0, 0.78, cfg.get("beard_h", 0.72)))
        parent_to(beard, head)
        for side, sx in (("L", -1), ("R", 1)):
            mous = add_sphere(f"{name}_moustache_{side}", 0.05 * s,
                              (sx * 0.026 * s, -0.150 * s, 1.139 * s),
                              MAT[cfg["hair"]],
                              scale=(0.60, 0.34, 0.24), seg=10, ring=6)
            mous.rotation_euler = (0, 0, math.radians(-10 * sx))
            parent_to(mous, head)

    arms = {}
    for side, sx in (("L", -1), ("R", 1)):
        sh = pivot(f"{name}_shoulder_{side}", (sx * 0.22 * s, 0, 0.90 * s),
                   spine)
        upper = add_cyl(f"{name}_uarm_{side}", 0.05 * s, 0.24 * s,
                        (sx * 0.22 * s, 0, 0.78 * s), MAT[cfg["top"]])
        parent_to(upper, sh)
        # joint ball hides the hinge wedge so the arm reads as one limb
        sball = add_sphere(f"{name}_shoulderball_{side}", 0.068 * s,
                           (sx * 0.22 * s, 0, 0.90 * s), MAT[cfg["top"]],
                           seg=10, ring=8)
        parent_to(sball, sh)
        # the elbow MUST hang off the shoulder pivot, otherwise the upper
        # arm swings and the forearm stays behind (detached-arm bug)
        elbow = pivot(f"{name}_elbow_{side}", (sx * 0.22 * s, 0, 0.66 * s),
                      sh)
        sleeve = cfg.get("fore", cfg["skin"])
        fore = add_cyl(f"{name}_farm_{side}", 0.045 * s, 0.22 * s,
                       (sx * 0.22 * s, 0, 0.54 * s), MAT[sleeve])
        parent_to(fore, elbow)
        eball = add_sphere(f"{name}_elbowball_{side}", 0.05 * s,
                           (sx * 0.22 * s, 0, 0.66 * s), MAT[sleeve],
                           seg=10, ring=8)
        parent_to(eball, elbow)
        hand = add_sphere(f"{name}_hand_{side}", 0.055 * s,
                          (sx * 0.22 * s, 0, 0.41 * s), MAT[cfg["skin"]],
                          seg=10, ring=8)
        parent_to(hand, elbow)
        arms[side] = {"shoulder": sh, "elbow": elbow}

    legs = {}
    for side, sx in (("L", -1), ("R", 1)):
        hip = pivot(f"{name}_hip_{side}", (sx * 0.11 * s, 0, 0.46 * s), hips)
        thigh = add_cyl(f"{name}_thigh_{side}", 0.062 * s, 0.3 * s,
                        (sx * 0.11 * s, 0, 0.3 * s),
                        MAT[cfg.get("legs2", cfg["legs"])])
        parent_to(thigh, hip)
        hball = add_sphere(f"{name}_hipball_{side}", 0.066 * s,
                           (sx * 0.11 * s, 0, 0.46 * s),
                           MAT[cfg.get("legs2", cfg["legs"])], seg=10, ring=8)
        parent_to(hball, hip)
        # the knee MUST hang off the hip pivot (same detached-limb reason)
        knee = pivot(f"{name}_knee_{side}", (sx * 0.11 * s, 0, 0.16 * s),
                     hip)
        shin = add_cyl(f"{name}_shin_{side}", 0.05 * s, 0.26 * s,
                       (sx * 0.11 * s, 0, 0.02 * s),
                       MAT[cfg.get("legs2", cfg["legs"])])
        parent_to(shin, knee)
        kball = add_sphere(f"{name}_kneeball_{side}", 0.052 * s,
                           (sx * 0.11 * s, 0, 0.16 * s),
                           MAT[cfg.get("legs2", cfg["legs"])], seg=10, ring=8)
        parent_to(kball, knee)
        shoe = add_cube(f"{name}_shoe_{side}", 0.12,
                        (sx * 0.11 * s, -0.03 * s, -0.12 * s),
                        MAT[cfg.get("shoe", "shoe")],
                        scale=(1.1, 1.6, 0.5))
        parent_to(shoe, knee)
        legs[side] = {"hip": hip, "knee": knee}

    for obj in bpy.data.objects:
        if obj.name.startswith(name + "_") and obj.type == "MESH":
            for poly in obj.data.polygons:
                poly.use_smooth = True

    # rig complete → move the root to its scene start position (the whole
    # articulated body follows, because every parent inverse was computed
    # against the origin during construction).
    # base_z lifts the rig so the shoe soles rest ON the ground: the shoe
    # box bottoms out at -0.15*s in rig space, so without it every foot
    # sinks through the grass (the "stump legs" look).
    base_z = 0.15 * s
    bpy.context.view_layer.update()
    root.location = (x, y, base_z)
    bpy.context.view_layer.update()
    return {"name": name, "root": root, "hips": hips, "spine": spine,
            "neck": neck, "head": head, "eyes": eyes, "mouth": mouth,
            "brows": brows, "cavity": cavity,
            "arms": arms, "legs": legs, "scale": s, "base_z": base_z}


# ── the playground environment ───────────────────────────────────────────────

def build_playground():
    add_cyl("Ground", 7.0, 0.1, (0, 0, -0.05), MAT["grass"], verts=40)
    add_cyl("Path", 1.4, 0.02, (0, 1.0, 0.005), MAT["path"], verts=32)
    # soft sky backdrop (emissive so it reads as sky, not as a green wall)
    add_cyl("Backdrop", 9.0, 0.1, (0, 6.0, 4.0), MAT["sky"],
            rot=(math.radians(90), 0, 0), verts=40)
    for i, (cx, cy, cz, s) in enumerate(((-3, 2.5, 4.2, 1.2),
                                         (1.5, 3.0, 4.6, 1.5),
                                         (4, 2.0, 3.8, 1.0))):
        add_sphere(f"Cloud{i}", 0.5 * s, (cx, cy, cz), MAT["cloud"],
                   scale=(1.6, 1.0, 0.7), seg=10, ring=6)
    # flowers sit BEHIND the characters (y > 0) so they never block the
    # action — in the tight portrait shots anything at y < 0 is foreground
    flower_cols = ("flower_r", "flower_y", "flower_p", "flower_w")
    for i in range(10):
        fx = -3.0 + (i % 5) * 1.45
        fy = 2.05 + (i // 5) * 0.95
        add_cyl(f"Flower_stem{i}", 0.012, 0.16, (fx, fy, 0.08),
                MAT["leaf"], verts=6)
        add_sphere(f"Flower{i}", 0.045, (fx, fy, 0.18),
                   MAT[flower_cols[i % 4]], seg=8, ring=6)
    add_cube("Slide_bed", 0.5, (3.1, 0.6, 0.75), MAT["slide"],
             rot=(math.radians(-38), 0, 0), scale=(0.9, 2.2, 0.1))
    add_cyl("Slide_post", 0.06, 1.5, (3.1, 1.5, 0.7), MAT["wood"])
    for sx in (-1, 1):
        add_cyl(f"Swing_leg{sx}", 0.05, 2.0, (-2.8 + sx * 0.5, 2.2, 0.95),
                MAT["swing"], rot=(math.radians(14 * sx), 0, 0))
    add_cyl("Swing_bar", 0.04, 1.3, (-2.8, 2.2, 1.85), MAT["swing"],
            rot=(0, 0, math.radians(90)))
    add_sphere("Ball", 0.16, (1.2, -1.6, 0.16), MAT["ball"], seg=12, ring=8)
    for i, (tx, ty) in enumerate(((-4.6, 1.0), (4.8, 2.6))):
        add_cyl(f"Tree_trunk{i}", 0.14, 1.4, (tx, ty, 0.65), MAT["wood"])
        add_sphere(f"Tree_crown{i}", 0.75, (tx, ty, 1.7), MAT["leaf"],
                   scale=(1.1, 1.1, 1.25), seg=12, ring=8)
    for i in range(6):
        add_cyl(f"Fence{i}", 0.04, 0.7, (-4.5 + i * 1.8, 3.6, 0.35),
                MAT["fence"])
    add_cyl("Fence_rail", 0.03, 9.2, (0.0, 3.6, 0.55), MAT["fence"],
            rot=(0, math.radians(90), 0))
    # the arch used to sit at y=-3.4, i.e. right where the portrait camera
    # now stands — it filled a third of every two-shot. Decorative only, so
    # it lives behind the action instead.
    add_torus("Arch", 1.3, 0.09, (0, 2.2, 1.3), MAT["slide"],
              rot=(math.radians(90), 0, 0))


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

    sun = light("Sun", "SUN", (3, -2, 6), 1.6, (1.0, 0.97, 0.9))
    sun.rotation_euler = (math.radians(35), math.radians(12), 0)
    light("Fill", "AREA", (-2.5, -2.5, 2.5), 22, (0.75, 0.85, 1.0), 3.0)
    light("Rim", "AREA", (0, 2.8, 2.4), 18, (1.0, 0.85, 0.6), 3.0)


def build_camera():
    cam_data = bpy.data.cameras.new("Cam")          # noqa: F821
    cam_data.lens = 38
    # HORIZONTAL sensor fit: with the default AUTO fit a portrait render
    # maps the 36 mm sensor to the LONG side (height), so the horizontal
    # field of view collapses to 0.56x of it — the reason every shot was
    # framed as if landscape and cropped the characters.
    cam_data.sensor_fit = "HORIZONTAL"
    cam_data.sensor_width = 36.0
    cam = bpy.data.objects.new("Cam", cam_data)     # noqa: F821
    bpy.context.collection.objects.link(cam)
    target = bpy.data.objects.new("Cam_target", None)  # noqa: F821
    bpy.context.collection.objects.link(target)
    track = cam.constraints.new("TRACK_TO")
    track.target = target
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"
    return cam, target


# ── the reusable animation action library ────────────────────────────────────

class Anim:
    """Keyframe writer for one character rig — real per-joint keys with
    per-limb phase offsets, SINE-smoothed."""

    def __init__(self, ch, fps=FPS):
        self.ch, self.fps = ch, fps

    def _k(self, t: float) -> int:
        return f(t)

    def rot(self, obj, t, x=None, y=None, z=None):
        if x is not None:
            obj.rotation_euler.x = x
        if y is not None:
            obj.rotation_euler.y = y
        if z is not None:
            obj.rotation_euler.z = z
        obj.keyframe_insert("rotation_euler", frame=self._k(t))

    def loc(self, obj, t, x=None, y=None, z=None):
        if x is not None:
            obj.location.x = x
        if y is not None:
            obj.location.y = y
        if z is not None:
            # root height is always measured from the rig's ground contact
            # (base_z), so hop/bounce values stay relative to the floor
            if obj is self.ch["root"]:
                z += self.ch.get("base_z", 0.0)
            obj.location.z = z
        obj.keyframe_insert("location", frame=self._k(t))

    def scale(self, obj, t, x=None, y=None, z=None):
        if x is not None:
            obj.scale.x = x
        if y is not None:
            obj.scale.y = y
        if z is not None:
            obj.scale.z = z
        obj.keyframe_insert("scale", frame=self._k(t))

    # idle: breathing + natural sway
    def idle(self, t0, t1, base_x=0.0):
        ch = self.ch
        t = t0
        while t <= t1 + 0.05:
            ph = 2 * math.pi * (t - t0) / 2.8
            self.loc(ch["root"], t, x=base_x, z=0.012 * math.sin(ph))
            self.rot(ch["spine"], t, z=math.radians(1.8 * math.sin(ph + 0.6)))
            self.rot(ch["neck"], t, z=math.radians(3.0 * math.sin(ph + 1.4)))
            self.rot(ch["arms"]["L"]["shoulder"], t,
                     x=math.radians(4 * math.sin(ph + 0.9)))
            self.rot(ch["arms"]["R"]["shoulder"], t,
                     x=math.radians(4 * math.sin(ph + 1.9)))
            t += 1 / 12.0

    # walking: real leg swings + arm counter-swing + root travel
    def walk(self, t0, t1, x_from, x_to, facing=1.0):
        ch = self.ch
        steps = max(2, int((t1 - t0) / BEAT))
        t = t0
        while t <= t1 + 0.05:
            p = (t - t0) / (t1 - t0)
            cyc = 2 * math.pi * p * steps
            sx = x_from + (x_to - x_from) * p
            self.loc(ch["root"], t, x=sx,
                     z=0.035 * max(0.0, math.sin(cyc * 2)))
            for side, phase in (("L", 0.0), ("R", math.pi)):
                swing = math.sin(cyc + phase)
                self.rot(ch["legs"][side]["hip"], t,
                         x=math.radians(24 * swing * facing))
                self.rot(ch["legs"][side]["knee"], t,
                         x=math.radians(max(0.0, 34 * math.sin(
                             cyc + phase - 0.9)) * facing))
                self.rot(ch["arms"][side]["shoulder"], t,
                         x=math.radians(-18 * swing * facing))
            self.rot(ch["spine"], t,
                     z=math.radians(3.5 * math.sin(cyc) * facing))
            t += 1 / 12.0

    # dance: side steps + hip roll + bounce, arms riding the beat
    def dance_sidestep(self, t0, t1, base_x, phase=0.0, amp=1.0):
        ch = self.ch
        t = t0
        while t <= t1 + 0.05:
            b = (t - t0) / BEAT
            frac = (b + phase) - math.floor(b + phase)
            sway = math.sin(2 * math.pi * b / 2)
            hop = abs(math.sin(math.pi * frac))
            self.loc(ch["root"], t, x=base_x + amp * 0.16 * sway,
                     z=amp * 0.06 * hop)
            self.rot(ch["hips"], t, z=math.radians(7 * sway * amp))
            self.rot(ch["spine"], t, x=math.radians(5 * hop * amp),
                     z=math.radians(-5 * sway * amp))
            for side, sx in (("L", -1), ("R", 1)):
                out = sx * sway
                self.rot(ch["legs"][side]["hip"], t,
                         x=math.radians(10 * hop * amp),
                         z=math.radians(14 * out * amp))
                self.rot(ch["legs"][side]["knee"], t,
                         x=math.radians(18 * hop * amp))
                self.rot(ch["arms"][side]["shoulder"], t,
                         y=math.radians(-sx * (40 + 25 * hop) * amp),
                         x=math.radians(-12 * sway * amp))
                self.rot(ch["arms"][side]["elbow"], t,
                         x=math.radians(-35 - 30 * hop))
            self.rot(ch["neck"], t, z=math.radians(6 * sway * amp),
                     x=math.radians(3 * hop))
            t += 1 / 12.0

    # arm wave
    def wave(self, t0, t1, side="R", cycles=3.0):
        ch = self.ch
        sh = ch["arms"][side]["shoulder"]
        elbow = ch["arms"][side]["elbow"]
        sx = -1 if side == "L" else 1
        t = t0
        while t <= t1 + 0.05:
            p = (t - t0) / max(0.1, (t1 - t0))
            raise_env = min(1.0, p * 4, (1 - p) * 4)
            self.rot(sh, t,
                     y=math.radians(-sx * (152 + 12 * math.sin(
                         2 * math.pi * cycles * p)) * raise_env),
                     x=math.radians(-10 * raise_env))
            # a real wave bends the elbow: the folded forearm keeps the
            # hand inside the portrait frame and reads far better
            self.rot(elbow, t,
                     y=math.radians(-sx * (38 + 12 * math.sin(
                         2 * math.pi * cycles * p)) * raise_env))
            t += 1 / 12.0

    # clapping
    def clap(self, t0, t1, claps=4):
        ch = self.ch
        t = t0
        while t <= t1 + 0.05:
            clap_ph = 2 * math.pi * claps * (t - t0) / (t1 - t0)
            together = 0.5 + 0.5 * math.sin(clap_ph)
            for side, sx in (("L", -1), ("R", 1)):
                self.rot(ch["arms"][side]["shoulder"], t,
                         x=math.radians(-70 - 15 * together),
                         z=math.radians(-sx * 18 * together))
                self.rot(ch["arms"][side]["elbow"], t,
                         x=math.radians(-30 - 25 * together))
            self.rot(ch["neck"], t, x=math.radians(6 * together))
            t += 1 / 16.0

    # bounce
    def bounce(self, t0, t1, hops=4):
        ch = self.ch
        t = t0
        while t <= t1 + 0.05:
            p = (t - t0) / max(0.1, (t1 - t0))
            hop = abs(math.sin(math.pi * hops * p))
            self.loc(ch["root"], t, z=0.14 * hop)
            for side in ("L", "R"):
                self.rot(ch["legs"][side]["hip"], t,
                         x=math.radians(-18 * hop))
                self.rot(ch["legs"][side]["knee"], t,
                         x=math.radians(30 * hop))
                self.rot(ch["arms"][side]["shoulder"], t,
                         y=math.radians(-(-1 if side == "L" else 1)
                                        * 30 * hop))
            t += 1 / 12.0

    # point / look
    def point(self, t0, t1, side="R", raise_=True):
        ch = self.ch
        sh = ch["arms"][side]["shoulder"]
        sx = -1 if side == "L" else 1
        t = t0
        while t <= t1 + 0.05:
            p = min(1.0, (t - t0) / 0.4)
            self.rot(sh, t, x=math.radians(-75 * p if raise_ else 0),
                     z=math.radians(sx * 20 * p))
            self.rot(ch["arms"][side]["elbow"], t, x=math.radians(-10 * p))
            t += 1 / 12.0

    def look(self, t0, t1, yaw_deg=0.0, pitch_deg=0.0):
        ch = self.ch
        t = t0
        while t <= t1 + 0.05:
            p = min(1.0, (t - t0) / 0.35)
            self.rot(ch["neck"], t, z=math.radians(yaw_deg * p),
                     x=math.radians(pitch_deg * p))
            t += 1 / 12.0

    # celebrate / laugh / talk
    def celebrate(self, t0, t1, hops=2):
        ch = self.ch
        t = t0
        while t <= t1 + 0.05:
            p = (t - t0) / max(0.1, (t1 - t0))
            up = min(1.0, p * 5)
            hop = abs(math.sin(math.pi * hops * p))
            self.loc(ch["root"], t, z=0.1 * hop)
            for side, sx in (("L", -1), ("R", 1)):
                self.rot(ch["arms"][side]["shoulder"], t,
                         y=math.radians(-sx * 150 * up),
                         x=math.radians(-15 * up))
                self.rot(ch["arms"][side]["elbow"], t,
                         x=math.radians(-20 * up))
            self.rot(ch["head"], t, x=math.radians(-8 * up))
            t += 1 / 12.0

    def laugh(self, t0, t1):
        ch = self.ch
        t = t0
        while t <= t1 + 0.05:
            shake = math.sin(2 * math.pi * 6 * (t - t0))
            self.rot(ch["spine"], t, x=math.radians(8 + 2.5 * shake))
            self.rot(ch["head"], t, x=math.radians(-10 + 3 * shake))
            self.scale(ch["mouth"], t, x=1.7, y=0.5, z=1.6)
            for side, sx in (("L", -1), ("R", 1)):
                self.rot(ch["arms"][side]["shoulder"], t,
                         y=math.radians(-sx * 12 * shake))
            t += 1 / 16.0

    def talk(self, t0, t1, gestures=True):
        ch = self.ch
        t = t0
        while t <= t1 + 0.05:
            ph = 2 * math.pi * 4.0 * (t - t0)
            self.scale(ch["mouth"], t, x=1.7, y=0.4,
                       z=0.5 + 0.9 * abs(math.sin(ph / 2)))
            if gestures:
                self.rot(ch["arms"]["R"]["shoulder"], t,
                         z=math.radians(15 + 8 * math.sin(ph)),
                         x=math.radians(-20 + 10 * math.sin(ph / 2)))
                self.rot(ch["arms"]["R"]["elbow"], t,
                         x=math.radians(-40 - 15 * math.sin(ph / 2)))
                self.rot(ch["neck"], t, z=math.radians(4 * math.sin(ph / 3)))
            t += 1 / 16.0


def finish_smoothing(ch):
    for obj in bpy.data.objects:
        if obj.name.startswith(ch["name"] + "_"):
            smooth(obj)


# ── the choreographed timeline + multi-shot camera ──────────────────────────

def choreograph(boy, expert, cam, target, duration):
    """Dance story (beat-locked):
       shot 1 WIDE (0–4.2)  : boy walks in, notices the rhythm
       shot 2 FULL (4.2–8.4): boy dances; expert walks in (background)
       shot 3 TWO (8.4–12.6): expert invites (point+wave); boy claps
       shot 4 BOTH (12.6–16.2): mirrored side-step dance together
       shot 5 POSE (16.2–end): celebrate — arms up, freeze
    """
    a_boy, a_exp = Anim(boy), Anim(expert)

    a_boy.walk(0.4, 3.0, x_from=-2.6, x_to=-0.9)
    a_boy.idle(3.0, 4.2, base_x=-0.9)
    a_boy.look(3.2, 4.2, yaw_deg=18, pitch_deg=6)
    # the expert stops close to the boy: a vertical 9:16 two-shot cannot
    # hold two characters 2.6 m apart without shrinking them to specks
    a_exp.walk(4.4, 7.0, x_from=4.6, x_to=0.25)
    a_boy.dance_sidestep(4.2, 8.4, base_x=-0.9, phase=0.0, amp=1.0)
    a_boy.wave(6.6, 8.2, side="R", cycles=2.5)
    a_exp.idle(7.0, 8.6, base_x=0.25)
    # he points with the LEFT arm, i.e. toward the boy standing to his left
    # (pointing with the right arm threw the hand out of the portrait frame)
    a_exp.point(8.6, 10.0, side="L", raise_=True)
    a_exp.wave(10.0, 11.6, side="R", cycles=2.0)
    # gestures off: the point/wave already own the arm keys, and two
    # writers on one joint at different rates makes the arm jitter
    a_exp.talk(8.6, 11.6, gestures=False)
    a_boy.clap(8.6, 11.4, claps=5)
    a_boy.bounce(11.4, 12.6, hops=2)
    a_boy.dance_sidestep(12.6, 16.2, base_x=-0.9, phase=0.0, amp=1.15)
    a_exp.dance_sidestep(12.6, 16.2, base_x=0.25, phase=0.5, amp=0.9)
    a_exp.laugh(14.6, 16.2)
    a_boy.celebrate(16.2, duration, hops=2)
    a_exp.celebrate(16.2, duration, hops=2)

    finish_smoothing(boy)
    finish_smoothing(expert)

    # ── camera plan ──────────────────────────────────────────────────────
    # PORTRAIT framing math: with sensor_fit=HORIZONTAL the horizontal
    # coverage at distance d is W = d*36/lens and the vertical coverage is
    # H = W*RES_H/RES_W. So the distance that gives a wanted vertical
    # coverage is  d = H*lens*(RES_W/RES_H)/36. Every shot is placed with
    # that formula, which is why nothing is cropped and no zoom is needed
    # to "fix" a bad frame.
    def coverage_distance(cov_h: float, lens: float) -> float:
        return cov_h * lens * (RES_W / RES_H) / 36.0

    shots = [
        # (t0, t1, centre_x, centre_z, vertical_coverage_m, lens)
        (0.0, 4.2, -1.75, 0.55, 4.60, 40),       # 1 wide establishing
        (4.2, 8.4, -0.90, 0.60, 2.50, 55),       # 2 full-body dance
        (8.4, 12.6, -0.28, 0.80, 4.05, 55),      # 3 two-shot invite
        (12.6, 16.2, -0.32, 0.80, 5.00, 50),     # 4 both dancing
        (16.2, duration, -0.32, 0.88, 4.70, 55),  # 5 closing pose
    ]
    for t0, t1, cx, cz, cov_h, lens in shots:
        dist = coverage_distance(cov_h, lens)
        cam.data.lens = lens
        cam.location = (cx, -dist, cz + 0.12)
        target.location = (cx, 0.0, cz)
        cam.keyframe_insert("location", frame=f(t0))
        target.keyframe_insert("location", frame=f(t0))
        cam.data.keyframe_insert("lens", frame=f(t0))
        # gentle dolly inside each shot (a real move, never a zoom)
        mid_t = min(t0 + (t1 - t0) * 0.55, t1)
        cam.location = (cx + 0.05, -dist * 0.94, cz + 0.10)
        cam.keyframe_insert("location", frame=f(mid_t))
        cam.location = (cx, -dist, cz + 0.12)
        cam.keyframe_insert("location", frame=f(t1))
        cam.data.keyframe_insert("lens", frame=f(t1))
    if cam.animation_data and cam.animation_data.action:
        for fc in cam.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "SINE"
    # the aim point must SNAP at each cut (CONSTANT), never glide between
    # shots — the camera position already cuts on the same frame
    if target.animation_data and target.animation_data.action:
        for fc in target.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "CONSTANT"


def set_render_settings(res_w, res_h, fps, duration):
    sc = bpy.context.scene
    sc.render.engine = "BLENDER_EEVEE"
    sc.render.resolution_x = res_w
    sc.render.resolution_y = res_h
    sc.render.fps = fps
    sc.render.image_settings.file_format = "PNG"
    sc.eevee.taa_render_samples = 16
    sc.eevee.use_gtao = False
    sc.eevee.use_ssr = False
    sc.eevee.use_bloom = True
    sc.eevee.bloom_intensity = 0.05
    sc.eevee.shadow_cube_size = "512"
    sc.view_settings.view_transform = "Standard"
    sc.frame_start = 1
    sc.frame_end = int(fps * duration)
    sc.render.filepath = "//frames/frame_"
    sc.world = bpy.data.worlds.new("SkyWorld")       # noqa: F821
    sc.world.use_nodes = True
    bg = sc.world.node_tree.nodes.get("Background")
    bg.inputs[0].default_value = (0.35, 0.62, 0.95, 1.0)
    bg.inputs[1].default_value = 0.45        # ambient fill, not a floodlight


def main():
    global RES_W, RES_H
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    opts = {"--out": "dance.blend", "--res": "720x1280", "--fps": "24",
            "--duration": "18", "--beats": "100"}
    for i, a in enumerate(argv):
        if a in opts and i + 1 < len(argv):
            opts[a] = argv[i + 1]
    res_w, res_h = map(int, opts["--res"].split("x"))
    RES_W, RES_H = res_w, res_h          # shot framing math needs the aspect
    fps = int(opts["--fps"])
    duration = float(opts["--duration"])

    bpy.ops.wm.read_factory_settings(use_empty=True)
    build_materials()
    expert = build_character(
        "Expert",
        {"skin": "expert_skin", "hair": "expert_hair",
         "hair_style": "wavy", "top": "expert_blazer",
         "top2": "expert_vest", "legs": "expert_trousers",
         "legs2": "expert_trousers", "glasses": True, "beard": True,
         "scale": 1.12},
        x=4.6)
    boy = build_character(
        "Boy",
        {"skin": "boy_skin", "hair": "boy_hair", "hair_style": "short",
         "top": "boy_hoodie", "legs": "boy_bottoms",
         "legs2": "boy_bottoms", "shoe": "shoe", "scale": 0.72},
        x=-2.6)
    build_playground()
    build_lights()
    cam, target = build_camera()
    bpy.context.scene.camera = cam

    choreograph(boy, expert, cam, target, duration)
    set_render_settings(res_w, res_h, fps, duration)
    bpy.ops.wm.save_as_mainfile(filepath=opts["--out"], compress=True)
    print("SCENE_BUILT", opts["--out"], "frames", int(fps * duration))


if __name__ == "__main__":          # importable by other scene builders
    main()
