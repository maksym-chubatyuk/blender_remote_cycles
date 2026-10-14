"""Scripted GUI test of the master's viewport path (opens a Blender window briefly).

    blender --factory-startup --python tests/test_gui.py -- OUTDIR

This windowed Blender hosts a cluster; a headless CPU node joins it.

Checks that the viewport draws frames, that moving an object sends only a
transform, that a material edit triggers a resync, and that navigating
sends a new view.
"""

import importlib.util
import os
import subprocess
import sys
import time

import bpy
import gpu
import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[sys.argv.index("--") + 1]
PORT = 19878
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
prefs = rc.ui.get_prefs()
# This machine's own render process must not touch the user's benchmark cache.
os.environ["BLENDER_USER_CONFIG"] = os.path.join(OUT, "local_config")
prefs.port, prefs.machine_name, prefs.auto_join, prefs.use_local = PORT, "gui-master", False, True
node_proc = subprocess.Popen(
    [bpy.app.binary_path, "-b", "-t", "4", "--factory-startup", "--python",
     os.path.join(REPO, "run_node.py"), "--", "--master", f"127.0.0.1:{PORT}",
     "--name", "cpu-node", "--cpu", "--once", "--score", "1"],
    stdout=open(os.path.join(OUT, "node.log"), "w"), stderr=subprocess.STDOUT, env=NODE_ENV)

sent = []
_orig_send = rc.master.ViewportSession._send
_orig_set_view = rc.master.ViewportSession.set_view


def _log_send(self, header, payload=b"", path=None):
    sent.append(header["op"])
    return _orig_send(self, header, payload, path)


def _log_set_view(self, view):
    if view != self._last_view and self.node is not None:
        sent.append("view")
    return _orig_set_view(self, view)


rc.master.ViewportSession._send = _log_send
rc.master.ViewportSession.set_view = _log_set_view

draw_times = []
_orig_view_draw = rc.engine.RemoteCyclesEngine.view_draw


def _timed_view_draw(self, context, depsgraph):
    t0 = time.perf_counter()
    _orig_view_draw(self, context, depsgraph)
    draw_times.append(time.perf_counter() - t0)


rc.engine.RemoteCyclesEngine.view_draw = _timed_view_draw

failures = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    if not ok:
        failures.append(name)


def view3d():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                return window, area
    raise RuntimeError("no 3D view")


def screenshot(name):
    window, area = view3d()
    with bpy.context.temp_override(window=window, area=area):
        bpy.ops.screen.screenshot_area(filepath=os.path.join(OUT, name))


def session():
    cl = rc.master.get()
    return next(iter(cl.sessions.values()), None) if cl else None


def frame_samples():
    s = session()
    return s.frame.samples if s and s.frame else 0


def steps():
    t0 = time.monotonic()
    while (rc.master.get() is None or not rc.master.get().render_nodes()) and time.monotonic() - t0 < 60:
        yield 0.2
    check("node joined the windowed master", rc.master.get() is not None and bool(rc.master.get().render_nodes()))
    scene = bpy.context.scene
    scene.render.engine = "REMOTE_CYCLES"
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120 and not any(
            n.local and n.score for n in rc.master.get().nodes.values()):
        yield 0.5
    cl = rc.master.get()
    check("master announces itself once Remote Cycles is the engine", cl.announce)
    check("this machine's render process started and joined",
          any(n.local and n.score for n in cl.nodes.values()),
          str([(n.name, n.score) for n in cl.nodes.values()]))
    prefs.machine_name = "gui-master-renamed"
    yield 0.5
    check("changing Machine Name renames the running master", cl.name == "gui-master-renamed", cl.name)
    prefs.use_local = False
    yield 3.0
    check("turning off Render on This Machine stops it",
          not rc.master.local_running() and not any(n.local for n in cl.nodes.values()))
    scene.render.engine = "REMOTE_CYCLES"
    scene.cycles.preview_samples = 32
    bpy.ops.mesh.primitive_plane_add(size=20, location=(0, 0, -1))
    mat = bpy.data.materials["Material"]
    bpy.data.objects["Light"].data.energy = 2000

    # Buffer from numpy is on the hot path of every frame upload.
    arr = np.ones(1920 * 1080 * 4, np.float32)
    t0 = time.perf_counter()
    gpu.types.Buffer("FLOAT", arr.size, arr)
    check("numpy -> gpu.Buffer is fast", time.perf_counter() - t0 < 0.1,
          f"({(time.perf_counter() - t0) * 1000:.1f} ms for 1080p)")

    window, area = view3d()
    space = area.spaces.active
    space.shading.type = "RENDERED"
    space.region_3d.view_perspective = "CAMERA"
    yield 1.0

    t0 = time.monotonic()
    while frame_samples() < 32 and time.monotonic() - t0 < 60:
        yield 0.1
    s = session()
    check("viewport session connected", s is not None, f"({s.node.name if s and s.node else 'none'})")
    check("viewport converges", frame_samples() >= 32, f"({frame_samples()} spp)")
    check("initial sync is a full scene", "scene" in sent and "view" in sent, str(sent))
    yield 0.5
    screenshot("gui_initial.png")

    sent.clear()
    bpy.data.objects["Cube"].location.x += 1.5
    yield 1.0
    check("moving an object sends only a transform",
          "xforms" in sent and "scene" not in sent, str(sent))

    sent.clear()
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.05, 0.6, 0.05, 1)
    yield 1.5
    check("material edit triggers a resync", "scene" in sent, str(sent))
    t0 = time.monotonic()
    while frame_samples() < 32 and time.monotonic() - t0 < 30:
        yield 0.1
    screenshot("gui_green.png")

    sent.clear()
    bpy.context.view_layer.objects.active = bpy.data.objects["Light"]
    bpy.data.objects["Light"].select_set(True)
    yield 1.0
    check("selecting does not resync", "scene" not in sent, str(sent))

    sent.clear()
    bpy.data.objects["Plane"].hide_set(True)
    yield 1.0
    check("hiding sends visibility only", "visible" in sent and "scene" not in sent, str(sent))
    bpy.data.objects["Plane"].hide_set(False)

    sent.clear()
    space.region_3d.view_perspective = "PERSP"
    space.region_3d.view_distance *= 1.3
    area.tag_redraw()
    yield 1.0
    check("navigating sends a new view", "view" in sent, str(sent))

    t0 = time.monotonic()
    while frame_samples() < 32 and time.monotonic() - t0 < 60:
        yield 0.1
    yield 0.5
    screenshot("gui_final.png")

    times = sorted(draw_times)
    if times:
        check("view_draw is cheap", times[len(times) // 2] < 0.02,
              f"(median {times[len(times) // 2] * 1000:.1f} ms, max {times[-1] * 1000:.1f} ms, "
              f"{len(times)} draws)")

    # --- Ctrl+F12: one window per machine -----------------------------------
    space.shading.type = "SOLID"
    prefs.use_local = True
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120 and len(cl.render_nodes()) < 2:
        yield 0.5
    machines = sorted(n.name for n in cl.render_nodes())
    check("two machines ready for the animation", len(machines) == 2, str(machines))
    r = scene.render
    r.resolution_x, r.resolution_y, r.resolution_percentage = 320, 240, 100
    scene.cycles.samples = 16
    scene.frame_start, scene.frame_end = 1, 6
    r.image_settings.file_format = "PNG"
    r.filepath = os.path.join(OUT, "anim_####.png")
    wm = bpy.context.window_manager
    with bpy.context.temp_override(window=wm.windows[0]):
        bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
    yield 1.0
    t0 = time.monotonic()
    while bpy.app.is_job_running("RENDER") and time.monotonic() - t0 < 120:
        yield 0.2
    yield 1.0
    check("all animation frames written",
          all(os.path.exists(os.path.join(OUT, f"anim_{f:04d}.png")) for f in range(1, 7)))
    shown = {}
    for w in wm.windows[1:]:
        for a in w.screen.areas:
            img = a.spaces.active.image if a.type == "IMAGE_EDITOR" else None
            if img is not None and img.get("remote_cycles_machine"):
                shown[img["remote_cycles_machine"]] = (w, img)
    check("one window per machine during the animation", sorted(shown) == machines,
          f"{sorted(shown)} in {len(wm.windows) - 1} extra windows")
    check("no window beyond one per machine", len(wm.windows) - 1 == len(machines),
          f"{len(wm.windows) - 1} extra windows")
    check("each machine window shows its frames", all(img.source == "FILE" for _w, img in shown.values()),
          str([img.name for _w, img in shown.values()]))
    # Frames are multilayer EXRs; Python reports their size as 0x0, so check the type.
    check("machine windows hold loaded frames",
          all(img.has_data and img.type == "MULTILAYER" for _w, img in shown.values()),
          str([(img.type, img.has_data) for _w, img in shown.values()]))
    for i, (w, img) in enumerate(shown.values()):
        area = max(w.screen.areas, key=lambda a: a.width * a.height)
        # New windows may still be resized by the window manager; let them settle.
        with bpy.context.temp_override(window=w):
            bpy.ops.wm.redraw_timer(type="DRAW_WIN", iterations=3)
        yield 3.0
        with bpy.context.temp_override(window=w, area=area):
            bpy.ops.screen.screenshot_area(filepath=os.path.join(OUT, f"machine_window_{i}.png"))
        print("window title:", img.name, flush=True)

    # --- F12: Blender's normal render window --------------------------------
    for w in list(wm.windows[1:]):
        with bpy.context.temp_override(window=w):
            bpy.ops.wm.window_close()
    yield 0.5
    r.filepath = os.path.join(OUT, "still.png")
    with bpy.context.temp_override(window=wm.windows[0]):
        bpy.ops.render.render("INVOKE_DEFAULT", write_still=True)
    yield 1.0
    t0 = time.monotonic()
    while bpy.app.is_job_running("RENDER") and time.monotonic() - t0 < 60:
        yield 0.2
    yield 0.5
    render_windows = [w for w in wm.windows[1:] for a in w.screen.areas
                      if a.type == "IMAGE_EDITOR" and a.spaces.active.image
                      and a.spaces.active.image.type == "RENDER_RESULT"]
    check("F12 shows Blender's render window", len(render_windows) == 1)
    check("F12 wrote the image", os.path.exists(os.path.join(OUT, "still.png")))
    if render_windows:
        area = max(render_windows[0].screen.areas, key=lambda a: a.width * a.height)
        with bpy.context.temp_override(window=render_windows[0]):
            bpy.ops.wm.redraw_timer(type="DRAW_WIN", iterations=3)
        yield 3.0
        with bpy.context.temp_override(window=render_windows[0], area=area):
            bpy.ops.screen.screenshot_area(filepath=os.path.join(OUT, "render_window.png"))

    print("FAILURES:", failures if failures else "none", flush=True)
    node_proc.kill()


_it = steps()


def _tick():
    try:
        return next(_it)
    except StopIteration:
        pass
    except Exception:
        import traceback
        traceback.print_exc()
        failures.append("exception")
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(_tick, first_interval=2.0)
