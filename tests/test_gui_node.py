"""A GUI Blender joins as a node (opens a window briefly).

    blender -b --factory-startup --python tests/test_gui_node.py -- OUTDIR

This background Blender is the master. It starts a windowed Blender that
finds it through discovery and clicks Join like a user would, then renders
a still, an animation and a viewport on it. That exercises the node's
non-blocking render jobs and scene loading inside a real window.
"""

import importlib.util
import os
import subprocess
import sys
import time

import bpy
import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[sys.argv.index("--") + 1]
PORT = 19877
os.makedirs(OUT, exist_ok=True)
# Nodes get their own config so test benchmarks never land in the user's cache.
NODE_ENV = {**os.environ, "BLENDER_USER_CONFIG": os.path.join(OUT, "node_config")}

spec = importlib.util.spec_from_file_location(
    "remote_cycles", os.path.join(REPO, "__init__.py"), submodule_search_locations=[REPO])
rc = importlib.util.module_from_spec(spec)
sys.modules["remote_cycles"] = rc
spec.loader.exec_module(rc)
bpy.context.preferences.addons.new().module = "remote_cycles"
rc.register()
cl = rc.master.start(PORT, "gui-test-master")
cl.announce = True  # normally set while the render engine is Remote Cycles

W, H = 320, 240
failures = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    if not ok:
        failures.append(name)


def wait_for(cond, timeout):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.1)
    return False


def load_rgba(path):
    img = bpy.data.images.load(path)
    w, h = img.size
    px = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(px)
    bpy.data.images.remove(img)
    return px.reshape(h, w, 4)


def similar(name, a, b, tol=0.003):
    def blocks(px, n=8):
        h, w = px.shape[:2]
        return px[:h - h % n, :w - w % n, :3].reshape(h // n, n, w // n, n, 3).mean(axis=(1, 3))
    diff = float(np.abs(blocks(a) - blocks(b)).mean())
    check(name, diff < tol * max(float(blocks(b).mean()), 1e-3), f"(block diff {diff:.5f})")


# The windowed node: load the add-on, find this master, press Join.
node_script = os.path.join(OUT, "gui_node.py")
with open(node_script, "w") as f:
    f.write(f'''
import bpy, importlib.util, os, sys, time
REPO = {REPO!r}
spec = importlib.util.spec_from_file_location("remote_cycles", os.path.join(REPO, "__init__.py"),
                                              submodule_search_locations=[REPO])
rc = importlib.util.module_from_spec(spec); sys.modules["remote_cycles"] = rc; spec.loader.exec_module(rc)
bpy.context.preferences.addons.new().module = "remote_cycles"
rc.register()
prefs = rc.ui.get_prefs()
prefs.machine_name = "gui-node"; prefs.auto_join = False; prefs.port = {PORT + 100}
def try_join():
    for m in rc.ui.discovered_masters():
        if m["name"] == "gui-test-master":
            bpy.ops.remote_cycles.join(master_id=m["id"])
            return None
    return 0.2
def quit_when_left():
    if rc.node.worker() is None and getattr(quit_when_left, "joined", False):
        bpy.ops.wm.quit_blender()
        return None
    if rc.node.worker() is not None:
        quit_when_left.joined = True
    return 0.5
bpy.app.timers.register(try_join, first_interval=1.0)
bpy.app.timers.register(quit_when_left, first_interval=2.0, persistent=True)
''')
log = open(os.path.join(OUT, "gui_node.log"), "w")
proc = subprocess.Popen([bpy.app.binary_path, "--factory-startup", "--python", node_script],
                        stdout=log, stderr=subprocess.STDOUT, env=NODE_ENV)


def node():
    with cl.lock:
        return next(iter(cl.nodes.values()), None)


check("windowed node joined through discovery and benchmarked",
      wait_for(lambda: node() is not None and node().score, 120),
      f"{node().name} {node().score}" if node() else "no node")

scene = bpy.context.scene
bpy.ops.mesh.primitive_plane_add(size=20, location=(0, 0, -1))
cube = bpy.data.objects["Cube"]
cube.keyframe_insert("rotation_euler", frame=1)
cube.rotation_euler = (0, 0, 1.5)
cube.keyframe_insert("rotation_euler", frame=4)
bpy.data.objects["Light"].data.energy = 2000
r = scene.render
r.resolution_x, r.resolution_y, r.resolution_percentage = W, H, 100
r.image_settings.file_format = "OPEN_EXR"
scene.frame_start, scene.frame_end = 1, 4
scene.cycles.samples = 32
scene.cycles.use_denoising = False
scene.cycles.device = "CPU"
scene.frame_set(2)


def render(engine, path, animation=False):
    r.engine = engine
    r.filepath = path
    bpy.ops.render.render(animation=animation, write_still=not animation)


render("CYCLES", os.path.join(OUT, "ref.exr"))
t0 = time.monotonic()
render("REMOTE_CYCLES", os.path.join(OUT, "still.exr"))
check("still rendered on the windowed node", os.path.exists(os.path.join(OUT, "still.exr")),
      f"({time.monotonic() - t0:.2f}s)")
similar("still equals a local render", load_rgba(os.path.join(OUT, "still.exr")),
        load_rgba(os.path.join(OUT, "ref.exr")))

render("CYCLES", os.path.join(OUT, "ref_####.exr"), animation=True)
t0 = time.monotonic()
render("REMOTE_CYCLES", os.path.join(OUT, "anim_####.exr"), animation=True)
check("animation rendered on the windowed node",
      all(os.path.exists(os.path.join(OUT, f"anim_{i:04d}.exr")) for i in range(1, 5)),
      f"({time.monotonic() - t0:.2f}s)")
similar("animation frame 4 equals a local render", load_rgba(os.path.join(OUT, "anim_0004.exr")),
        load_rgba(os.path.join(OUT, "ref_0004.exr")))

r.engine = "REMOTE_CYCLES"
scene.cycles.preview_samples = 32
session = rc.master.ViewportSession(cl)
session.scene_name, session.layer_name = scene.name, bpy.context.view_layer.name
session.visible = sorted(o.name for o in scene.objects)
session.size = (W, H)
session.attach()
session.full_sync()
dg = bpy.context.evaluated_depsgraph_get()
cam = scene.camera
session.set_view(rc.engine.camera_from_matrices(cam.calc_matrix_camera(dg, x=W, y=H),
                                                cam.matrix_world.inverted(), True, W, H))
t0 = time.monotonic()
ok = wait_for(lambda: session.frame is not None and session.frame.samples >= 32, 60)
check("viewport converges on the windowed node", ok,
      f"({time.monotonic() - t0:.2f}s, {session.frame.samples if session.frame else 0} spp)")
if ok:
    render("CYCLES", os.path.join(OUT, "ref_view.exr"))
    similar("viewport frame matches a local render", session.frame.pixels,
            load_rgba(os.path.join(OUT, "ref_view.exr")), tol=0.03)
session.close()

# Closing the master makes the node leave and restore its own file.
rc.master.stop()
check("windowed node leaves and exits when the master goes away", wait_for(lambda: proc.poll() is not None, 30))
proc.kill()
print("FAILURES:", failures if failures else "none", flush=True)
sys.exit(1 if failures else 0)
