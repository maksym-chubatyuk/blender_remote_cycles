"""End-to-end cluster test without a GUI: this Blender is the master, two
headless CPU nodes of different speed join it.

    blender -b --factory-startup --python tests/test_cluster.py -- OUTDIR

Checks discovery, benchmark ranking, split stills (with and without
denoising), single-node stills, animations spread across nodes, the
viewport, and a node dropping out mid-animation.
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
PORT = 19876
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
cl = rc.master.start(PORT, "test-master")
assert cl is not None, rc.master.error()
cl.announce = True  # normally set while the render engine is Remote Cycles

W, H = 320, 240
failures = []
procs = {}


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    if not ok:
        failures.append(name)


def spawn_node(name, threads, discover=False):
    args = [bpy.app.binary_path, "-b", "-t", str(threads), "--factory-startup",
            "--python", os.path.join(REPO, "run_node.py"), "--",
            "--name", name, "--cpu", "--once", "--bench-samples", "8"]
    args += ["--join", "test-master"] if discover else ["--master", f"127.0.0.1:{PORT}"]
    log = open(os.path.join(OUT, f"node_{name}.log"), "w")
    procs[name] = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, env=NODE_ENV)


def wait_for(cond, timeout, what=""):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.1)
    print(f"timed out waiting for {what}", flush=True)
    return False


def nodes_by_name():
    with cl.lock:
        return {n.name: n for n in cl.nodes.values()}


def load_rgba(path):
    img = bpy.data.images.load(path)
    w, h = img.size
    px = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(px)
    bpy.data.images.remove(img)
    return px.reshape(h, w, 4)


def blocks(px, n=8):
    h, w = px.shape[:2]
    return px[:h - h % n, :w - w % n, :3].reshape(h // n, n, w // n, n, 3).mean(axis=(1, 3))


def similar(name, a, b, tol=0.03):
    diff = float(np.abs(blocks(a) - blocks(b)).mean())
    scale = float(blocks(b).mean())
    check(name, diff < tol * max(scale, 1e-3), f"(block diff {diff:.5f}, level {scale:.3f})")


def setup_scene():
    scene = bpy.context.scene
    bpy.ops.mesh.primitive_plane_add(size=20, location=(0, 0, -1))
    cube = bpy.data.objects["Cube"]
    cube.rotation_euler = (0, 0, 0)
    cube.keyframe_insert("rotation_euler", frame=1)
    cube.rotation_euler = (0, 0, 1.5)
    cube.keyframe_insert("rotation_euler", frame=8)
    bpy.data.objects["Light"].data.energy = 2000
    r = scene.render
    r.resolution_x, r.resolution_y, r.resolution_percentage = W, H, 100
    r.image_settings.file_format = "OPEN_EXR"
    scene.frame_start, scene.frame_end = 1, 8
    c = scene.cycles
    c.samples = 32
    c.use_denoising = False
    c.device = "CPU"
    return scene


def render(scene, engine, path, animation=False):
    scene.render.engine = engine
    scene.render.filepath = path
    if animation:
        bpy.ops.render.render(animation=True)
    else:
        bpy.ops.render.render(write_still=True)


scene = setup_scene()
scene.frame_set(3)

# --- joining -------------------------------------------------------------
spawn_node("fast", 6, discover=True)
spawn_node("slow", 1)
ok = wait_for(lambda: len(nodes_by_name()) == 2 and all(n.score for n in nodes_by_name().values()),
              180, "two benchmarked nodes")
check("both nodes joined and benchmarked", ok, str({n: v.score for n, v in nodes_by_name().items()}))
nodes = nodes_by_name()
check("discovery: node found the master by its beacon", "fast" in nodes)
check("benchmark ranks the 6-thread node above the 1-thread node",
      nodes.get("fast") and nodes.get("slow") and nodes["fast"].score > nodes["slow"].score)
wait_for(lambda: all(n.bandwidth for n in nodes_by_name().values()), 10, "link test")
check("link speed measured", all(n.bandwidth for n in nodes_by_name().values()),
      str({n: f"{v.bandwidth / 1e6:.0f} MB/s" for n, v in nodes_by_name().items() if v.bandwidth}))

# --- devices, announcing, renaming --------------------------------------------
slow = nodes_by_name()["slow"]
check("nodes report their devices", any(d["type"] == "CPU" for d in slow.devices) and slow.selected,
      str(slow.devices))
cl.set_devices(slow, list(slow.selected))
check("changing devices re-benchmarks the node",
      slow.score is None and wait_for(lambda: slow.score is not None, 120), str(slow.score))

listener = rc.ui.state["listener"]
cl.announce = False
hidden = wait_for(lambda: all(m["id"] != cl.id for m in listener.masters()), 10)
check("a master that is not using Remote Cycles is not announced", hidden)
cl.announce = True
cl.name = "renamed-master"
check("renaming the master shows up on the network",
      wait_for(lambda: any(m["name"] == "renamed-master" for m in listener.masters()), 10))
cl.name = "test-master"

# --- split still -----------------------------------------------------------
render(scene, "CYCLES", os.path.join(OUT, "ref_still.exr"))
ref = load_rgba(os.path.join(OUT, "ref_still.exr"))
cl.history.clear()
t0 = time.monotonic()
render(scene, "REMOTE_CYCLES", os.path.join(OUT, "split_still.exr"))
print(f"split still took {time.monotonic() - t0:.2f}s")
chunks = [h for h in cl.history if h[0] == "chunk"]
check("still was split across both nodes", {h[4] for h in chunks} == {"fast", "slow"},
      str([(h[2], h[3], h[4]) for h in chunks]))
check("all samples rendered exactly once", sorted((h[2], h[3]) for h in chunks)
      and sum(h[3] for h in chunks) == 32)
similar("split still equals a local render", load_rgba(os.path.join(OUT, "split_still.exr")), ref, tol=0.002)

# --- split still with denoising -------------------------------------------
scene.cycles.use_denoising = True
render(scene, "CYCLES", os.path.join(OUT, "ref_denoised.exr"))
render(scene, "REMOTE_CYCLES", os.path.join(OUT, "split_denoised.exr"))
den = load_rgba(os.path.join(OUT, "split_denoised.exr"))
ref_den = load_rgba(os.path.join(OUT, "ref_denoised.exr"))
noise = lambda px: float(np.abs(np.diff(px[..., 0], axis=1)).mean())
similar("denoised split still matches local denoised render", den, ref_den)
check("denoised split still is actually denoised", noise(den) < noise(ref) * 0.6,
      f"(noise {noise(den):.4f} vs noisy {noise(ref):.4f})")
scene.cycles.use_denoising = False

# --- single node still -----------------------------------------------------
cl.set_enabled("slow", False)
cl.history.clear()
render(scene, "REMOTE_CYCLES", os.path.join(OUT, "single_still.exr"))
frames = [h for h in cl.history if h[0] == "frame"]
check("with one node enabled the still renders whole on it", [h[4] for h in frames] == ["fast"],
      str(frames))
similar("single node still equals a local render", load_rgba(os.path.join(OUT, "single_still.exr")), ref, 0.002)
cl.set_enabled("slow", True)

# --- animation ---------------------------------------------------------------
render(scene, "CYCLES", os.path.join(OUT, "ref_anim_####.exr"), animation=True)
cl.history.clear()
t0 = time.monotonic()
render(scene, "REMOTE_CYCLES", os.path.join(OUT, "anim_####.exr"), animation=True)
print(f"animation took {time.monotonic() - t0:.2f}s")
frames = [h for h in cl.history if h[0] == "frame"]
by_node = {}
for h in frames:
    by_node.setdefault(h[4], []).append(int(h[2]))
check("all 8 frames written", all(os.path.exists(os.path.join(OUT, f"anim_{f:04d}.exr")) for f in range(1, 9)))
check("frames spread across both nodes", set(by_node) == {"fast", "slow"}, str(by_node))
check("fast node rendered more frames", len(by_node.get("fast", [])) > len(by_node.get("slow", [])), str(by_node))
first = min(frames, key=lambda h: h[2]) if frames else None
check("first frame went to the fastest node", first is not None and first[4] == "fast", str(first))
for f in (1, 5, 8):
    similar(f"animation frame {f} equals local render",
            load_rgba(os.path.join(OUT, f"anim_{f:04d}.exr")),
            load_rgba(os.path.join(OUT, f"ref_anim_{f:04d}.exr")), 0.002)

# --- the end of an animation does not wait on a slow node -------------------
# 3 frames: the fast node takes 1 and 3, the slow one 2. The fast node then
# finishes long before the slow one would, so it should take frame 2 over.
scene.frame_end = 3
scene.cycles.samples = 256
cl.history.clear()
t0 = time.monotonic()
render(scene, "REMOTE_CYCLES", os.path.join(OUT, "tail_####.exr"), animation=True)
winners = {int(h[2]): h[4] for h in cl.history if h[0] == "frame"}
check("idle fast node took over the slow node's last frame", winners == {1: "fast", 2: "fast", 3: "fast"},
      f"{winners} in {time.monotonic() - t0:.2f}s")
scene.frame_end = 8
scene.cycles.samples = 32

# --- viewport ---------------------------------------------------------------
scene.render.engine = "REMOTE_CYCLES"
scene.cycles.preview_samples = 32
session = rc.master.ViewportSession(cl)
session.scene_name, session.layer_name = scene.name, bpy.context.view_layer.name
session.visible = sorted(o.name for o in scene.objects)
session.size = (W, H)
check("viewport attaches to the fastest delivering node", session.attach() and session.node.name == "fast",
      session.node.name if session.node else "none")
session.full_sync()
dg = bpy.context.evaluated_depsgraph_get()
cam = scene.camera
session.set_view(rc.engine.camera_from_matrices(cam.calc_matrix_camera(dg, x=W, y=H),
                                                cam.matrix_world.inverted(), True, W, H))
wait_for(lambda: session.frame and session.frame.samples >= 32, 60, "viewport frame")
f = session.frame
check("viewport frame arrived", f is not None and f.samples >= 32)
if f is not None:
    render(scene, "CYCLES", os.path.join(OUT, "ref_view.exr"))
    similar("viewport frame matches local render", f.pixels, load_rgba(os.path.join(OUT, "ref_view.exr")))
session.close()

# --- node lost mid-animation --------------------------------------------------
scene.render.engine = "REMOTE_CYCLES"
scene.render.filepath = os.path.join(OUT, "lost_####.exr")


def kill_slow_soon():
    # Kill as soon as the slow node is busy rendering a frame.
    slow = nodes_by_name().get("slow")
    if slow is not None and slow.inflight is not None:
        procs["slow"].kill()
        return None
    return 0.05


import threading


def killer():
    t0 = time.monotonic()
    while time.monotonic() - t0 < 60:
        if kill_slow_soon() is None:
            return
        time.sleep(0.05)


threading.Thread(target=killer, daemon=True).start()
bpy.ops.render.render(animation=True)
check("animation finishes after a node drops out",
      all(os.path.exists(os.path.join(OUT, f"lost_{f:04d}.exr")) for f in range(1, 9)))
check("dropped node left the cluster", "slow" not in nodes_by_name())

# --- this machine renders too --------------------------------------------------
os.environ["BLENDER_USER_CONFIG"] = NODE_ENV["BLENDER_USER_CONFIG"]
rc.master.start_local(PORT, "test-master")
local_ok = wait_for(lambda: any(n.local and n.score for n in nodes_by_name().values()), 180)
local = next((n for n in nodes_by_name().values() if n.local), None)
check("this machine's own render process joins as a node", local_ok and local.name == "test-master (this machine)",
      local.name if local else "none")
cl.history.clear()
render(scene, "REMOTE_CYCLES", os.path.join(OUT, "with_local.exr"))
check("this machine takes part in rendering",
      any(h[4] == "test-master (this machine)" for h in cl.history), str(list(cl.history)))
similar("still with this machine included equals a local render",
        load_rgba(os.path.join(OUT, "with_local.exr")), ref, 0.002)
rc.master.stop_local()
check("stopping it removes it from the cluster",
      wait_for(lambda: not any(n.local for n in nodes_by_name().values()), 15))

for p in procs.values():
    p.kill()
rc.master.stop()
print("FAILURES:", failures if failures else "none", flush=True)
sys.exit(1 if failures else 0)
