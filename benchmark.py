"""A small built-in Cycles scene for ranking render nodes.

The score is millions of camera samples per second (pixels x samples /
seconds), measured after a warm-up render so kernel compilation is excluded.
Results are cached per Blender version and render device.
"""

import json
import os

import bmesh
import bpy

SCENE_NAME = "__remote_cycles_benchmark"
WIDTH, HEIGHT, SAMPLES = 640, 360, 64


def _cache_path():
    base = bpy.utils.user_resource("CONFIG", path="remote_cycles", create=True)
    return os.path.join(base, "benchmark.json")


def _cache_key(device_name):
    return f"{bpy.app.version_string}|{device_name}"


def cached_score(device_name):
    try:
        with open(_cache_path()) as f:
            return json.load(f).get(_cache_key(device_name))
    except (OSError, ValueError):
        return None


def store_score(device_name, score):
    path = _cache_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data[_cache_key(device_name)] = score
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


def score_from(seconds, samples):
    return WIDTH * HEIGHT * samples / max(seconds, 1e-6) / 1e6


def _material(name, **inputs):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    for key, value in inputs.items():
        bsdf.inputs[key].default_value = value
    return mat


def _mesh_object(name, build, material, location):
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    build(bm)
    bm.to_mesh(mesh)
    bm.free()
    for poly in mesh.polygons:
        poly.use_smooth = True
    mesh.materials.append(material)
    ob = bpy.data.objects.new(name, mesh)
    ob.location = location
    return ob


def build_scene(device, samples=SAMPLES):
    """Create the benchmark scene; returns it. Remove it with ``remove_scene``."""
    remove_scene()
    scene = bpy.data.scenes.new(SCENE_NAME)
    r = scene.render
    r.engine = "CYCLES"
    r.resolution_x, r.resolution_y, r.resolution_percentage = WIDTH, HEIGHT, 100
    r.use_compositing = False
    r.use_sequencer = False
    c = scene.cycles
    c.device = device
    c.samples = samples
    c.use_adaptive_sampling = False
    c.use_denoising = False
    c.seed = 0

    world = bpy.data.worlds.new(SCENE_NAME)
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.3
    scene.world = world

    glass = _material(SCENE_NAME + "_glass", **{"Transmission Weight": 1.0, "Roughness": 0.05})
    metal = _material(SCENE_NAME + "_metal", **{"Metallic": 1.0, "Roughness": 0.25})
    diffuse = _material(SCENE_NAME + "_diffuse", **{"Base Color": (0.6, 0.5, 0.4, 1.0)})

    objects = [
        _mesh_object(SCENE_NAME + "_glass", lambda bm: bmesh.ops.create_icosphere(
            bm, subdivisions=5, radius=1.0), glass, (-1.2, 0, 1)),
        _mesh_object(SCENE_NAME + "_metal", lambda bm: bmesh.ops.create_uvsphere(
            bm, u_segments=64, v_segments=32, radius=0.8), metal, (1.2, 0.5, 0.8)),
        _mesh_object(SCENE_NAME + "_cube", lambda bm: bmesh.ops.create_cube(
            bm, size=1.0), diffuse, (0.2, 1.8, 0.5)),
        _mesh_object(SCENE_NAME + "_floor", lambda bm: bmesh.ops.create_grid(
            bm, x_segments=1, y_segments=1, size=10), diffuse, (0, 0, 0)),
    ]
    light_data = bpy.data.lights.new(SCENE_NAME, "AREA")
    light_data.energy = 800
    light_data.size = 2
    light = bpy.data.objects.new(SCENE_NAME + "_light", light_data)
    light.location = (2, -2, 5)
    cam_data = bpy.data.cameras.new(SCENE_NAME)
    cam = bpy.data.objects.new(SCENE_NAME + "_camera", cam_data)
    cam.location = (0, -7, 3)
    cam.rotation_euler = (1.2, 0, 0)
    for ob in objects + [light, cam]:
        scene.collection.objects.link(ob)
    scene.camera = cam
    return scene


def remove_scene():
    scene = bpy.data.scenes.get(SCENE_NAME)
    if scene is None:
        return
    objects = list(scene.collection.objects)
    data = [ob.data for ob in objects if ob.data is not None]
    for ob in objects:
        bpy.data.objects.remove(ob)
    for block in data:
        if isinstance(block, bpy.types.Mesh):
            bpy.data.meshes.remove(block)
        elif isinstance(block, bpy.types.Light):
            bpy.data.lights.remove(block)
        elif isinstance(block, bpy.types.Camera):
            bpy.data.cameras.remove(block)
    for mat in [m for m in bpy.data.materials if m.name.startswith(SCENE_NAME)]:
        bpy.data.materials.remove(mat)
    if scene.world and scene.world.name.startswith(SCENE_NAME):
        bpy.data.worlds.remove(scene.world)
    bpy.data.scenes.remove(scene)
