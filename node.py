"""Render node: a Blender that joined a master and renders its work.

While joined, the node loads the master's scene in place of its own file and
renders whatever the master sends: whole frames, sample slices of a frame,
or progressive viewport steps. In the GUI renders run as background jobs so
the window stays responsive; headless nodes (``run_node.py``) render
synchronously.
"""

import collections
import os
import platform
import queue
import socket
import tempfile
import threading
import time
import traceback
import zlib

import bpy
import numpy as np
from bpy.app.handlers import persistent
from mathutils import Matrix

from . import benchmark, exr, protocol
from .link import Link

VIEW_CAMERA_NAME = "__remote_cycles_view"
CACHE_DIR = os.path.join(tempfile.gettempdir(), "remote_cycles_cache")
MAX_VIEW_CHUNK = 16
_VIEW_OPS = {"scene", "xforms", "visible", "frame", "view", "view_stop"}


def log(*args):
    print("[remote-cycles node]", *args, flush=True)


# --------------------------------------------------------------------------
# Blob cache


def blob_path(key):
    return os.path.join(CACHE_DIR, key)


def cached_keys():
    os.makedirs(CACHE_DIR, exist_ok=True)
    return [k for k in os.listdir(CACHE_DIR) if not k.endswith(".part")]


def store_blob(key, payload, verify):
    if not key or "/" in key or "\\" in key or key.startswith("."):
        raise ValueError(f"bad blob key {key!r}")
    path = blob_path(key)
    if os.path.exists(path):
        return
    if verify and protocol.blob_hash(payload) != key:
        raise ValueError("blob hash mismatch")
    # Unique per writer: nodes on one machine share the cache.
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
    with open(tmp, "wb") as f:
        f.write(payload)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Render device


def cpu_name():
    return f"CPU ({platform.processor() or platform.machine()})"


def available_devices():
    """What Cycles can render with here: the best GPU backend's devices plus
    the CPU, as a list of {"id", "name", "type"}."""
    prefs = bpy.context.preferences.addons["cycles"].preferences
    current = prefs.compute_device_type
    order = [current] if current not in ("", "NONE") else []
    order += [t for t in ("METAL", "OPTIX", "CUDA", "HIP", "ONEAPI") if t not in order]
    backend = "NONE"
    for dtype in order:
        try:
            prefs.compute_device_type = dtype
        except TypeError:
            continue
        prefs.refresh_devices()
        if any(d.type == dtype for d in prefs.devices):
            backend = dtype
            break
    else:
        prefs.compute_device_type = "NONE"
        prefs.refresh_devices()
    devices = [{"id": d.id, "name": d.name, "type": d.type}
               for d in prefs.devices if d.type in (backend, "CPU")]
    if not any(d["type"] == "CPU" for d in devices):
        devices.append({"id": "CPU", "name": cpu_name(), "type": "CPU"})
    return devices


def default_selection(devices, cpu_only=False):
    """All GPUs, or the CPU if there are none."""
    gpus = [d["id"] for d in devices if d["type"] != "CPU"]
    if gpus and not cpu_only:
        return gpus
    return [d["id"] for d in devices if d["type"] == "CPU"][:1]


def apply_devices(devices, selected):
    """Make Cycles use exactly ``selected``; returns (scene device, description)."""
    chosen = [d for d in devices if d["id"] in selected]
    if not chosen:
        chosen = [d for d in devices if d["id"] in default_selection(devices, cpu_only=True)]
    description = " + ".join(d["name"] for d in chosen)
    if not any(d["type"] != "CPU" for d in chosen):
        return "CPU", description
    prefs = bpy.context.preferences.addons["cycles"].preferences
    ids = {d["id"] for d in chosen}
    for d in prefs.devices:
        d.use = d.id in ids
    return "GPU", description


# --------------------------------------------------------------------------
# Running renders

_render_events = {"complete": 0, "cancel": 0}


# These run on the render thread; they only bump counters.
@persistent
def _on_render_complete(*_args):
    _render_events["complete"] += 1


@persistent
def _on_render_cancel(*_args):
    _render_events["cancel"] += 1


class BlockingRunner:
    """Headless: render synchronously."""

    def start(self, scene, layer):
        bpy.ops.render.render(write_still=True, **_render_args(scene, layer))

    def poll(self):
        return "done"


class JobRunner:
    """GUI: render as a background job so the window stays responsive.

    The job may start a moment after the operator returns, so completion is
    taken from the render complete/cancel handlers, not from whether a job
    is currently running."""

    START_TIMEOUT = 30.0

    def __init__(self):
        self._complete = self._cancel = 0
        self._started = 0.0

    def start(self, scene, layer):
        self._complete = _render_events["complete"]
        self._cancel = _render_events["cancel"]
        self._started = time.monotonic()
        window = bpy.context.window_manager.windows[0]
        # No render window on the node. The preference is only changed while
        # the operator decides where to show the render, so it is never saved.
        view = bpy.context.preferences.view
        display = view.render_display_type
        view.render_display_type = "NONE"
        try:
            with bpy.context.temp_override(window=window):
                result = bpy.ops.render.render("INVOKE_DEFAULT", write_still=True,
                                               **_render_args(scene, layer))
        finally:
            view.render_display_type = display
        if "RUNNING_MODAL" not in result:
            raise RuntimeError("Blender did not start the render (is another render running?)")

    def poll(self):
        finished = _render_events["complete"] != self._complete
        cancelled = _render_events["cancel"] != self._cancel
        if not (finished or cancelled):
            if not bpy.app.is_job_running("RENDER") and \
                    time.monotonic() - self._started > self.START_TIMEOUT:
                return "cancelled"
            return "busy"
        if bpy.app.is_job_running("RENDER"):
            return "busy"  # let the job wind down before starting another
        return "cancelled" if cancelled else "done"


def _render_args(scene, layer):
    # An empty layer name counts as "not found" for render jobs.
    return {"scene": scene.name, "layer": layer} if layer else {"scene": scene.name}


class _Deferred(Exception):
    """A file was just loaded in the GUI; start the render on the next tick,
    once the window has caught up with the new file."""


def _set(obj, attr, value):
    if getattr(obj, attr) != value:
        setattr(obj, attr, value)


def to_matrix(flat):
    return Matrix([flat[i:i + 4] for i in range(0, 16, 4)])


# --------------------------------------------------------------------------
# Worker


class ViewState:
    """One viewport on the master that this node renders for."""

    def __init__(self, sid):
        self.sid = sid
        self.blend = None
        self.files = {}
        self.scene = None
        self.layer = ""
        self.frame = None
        self.applied_frame = None
        self.xforms = {}
        self.pending_xforms = set()
        self.visible = None
        self.visible_dirty = False
        self.view = None
        self.cam_name = None
        self.target = 0
        self.generation = 0
        self.reset()

    def reset(self):
        self.generation += 1
        self.step = 0
        self.accum = None
        self.done = 0
        self.finished = False

    @property
    def wants(self):
        return bool(self.blend and self.view and not self.finished)


class NodeWorker:
    def __init__(self, address, port, name, devices, selected, gui, score=None,
                 bench_samples=benchmark.SAMPLES, local=False):
        self.gui = gui
        self.name = name
        self.devices = devices
        self.selected = selected
        self.device, self.device_name = apply_devices(devices, selected)
        self.bench_samples = bench_samples
        if score is None and bench_samples == benchmark.SAMPLES:
            score = benchmark.cached_score(self.device_name)
        self.score = score
        self.work_dir = tempfile.mkdtemp(prefix="remote_cycles_node_")
        self.events = queue.Queue()

        sock = socket.create_connection((address, port), timeout=5)
        protocol.tune_socket(sock)
        protocol.send_msg(sock, {
            "op": "hello", "version": protocol.PROTOCOL_VERSION, "name": name,
            "device": self.device_name, "devices": devices, "selected": selected,
            "score": score, "blender": list(bpy.app.version), "local": local,
            "have": cached_keys(),
        })
        header, _ = protocol.recv_msg(sock)
        if header.get("op") != "welcome":
            sock.close()
            raise ConnectionError(header.get("text", "the master refused the connection"))
        sock.settimeout(None)
        self.master_name = header.get("name", address)
        self.link = Link(sock, self._on_message, name="master")

        self.runner = JobRunner() if gui else BlockingRunner()
        self.current = None
        self.work = collections.deque()
        self.views = {}
        self.loaded = None
        self.applied_frame = None
        self.accums = {}
        self.cancelled = set()
        self.status = "Idle"
        self.error = None
        self._rr = 0
        self.just_loaded = False
        self.bench_stage = 0 if score is None else None
        self.link.start()

    @property
    def alive(self):
        return self.link.alive

    def close(self):
        self.link.close()

    def rename(self, name):
        self.name = name
        self.link.send({"op": "rename", "name": name})

    def _set_devices(self, selected):
        """The master picked which devices to use; re-benchmark if needed."""
        known = {d["id"] for d in self.devices}
        self.selected = [i for i in selected if i in known] or default_selection(self.devices)
        self.device, self.device_name = apply_devices(self.devices, self.selected)
        self.loaded = None  # the scene's device is set when loading
        self.score = None
        if self.bench_samples == benchmark.SAMPLES:
            self.score = benchmark.cached_score(self.device_name)
        self.link.send({"op": "devices", "device": self.device_name, "selected": self.selected})
        if self.score is None:
            self.bench_stage = 0
        else:
            self.link.send({"op": "score", "score": self.score})
        log(f"devices: {self.device_name}")

    def report_status(self, text):
        if text != self.status:
            self.status = text
            self.link.send({"op": "status", "text": text})

    # Messages ---------------------------------------------------------------

    def _on_message(self, header, payload):
        """Receiver thread: uploads and pings are answered here directly."""
        op = header.get("op")
        if op == "put":
            store_blob(header["key"], payload, header.get("verify", True))
        elif op == "ping":
            self.link.send({"op": "pong", "seq": header.get("seq")},
                           bytes(header.get("reply_size", 0)))
        else:
            self.events.put((header, payload))

    def wait_event(self, timeout):
        try:
            header, payload = self.events.get(timeout=timeout)
        except queue.Empty:
            return
        self._handle(header, payload)

    def _handle(self, header, payload):
        op = header.get("op")
        if op == "_closed":
            self.error = "Master disconnected"
        elif op in _VIEW_OPS:
            self._handle_view(op, header)
        elif op == "work":
            if header["job"] in self.cancelled:
                self.link.send({"op": "done", "job": header["job"], "item": header["item"],
                                "kind": header["kind"], "discarded": True})
            else:
                self.work.append(header)
        elif op == "collect":
            self._send_passes(header["job"])
        elif op == "devices":
            self._set_devices(header.get("selected", []))
        elif op == "cancel":
            job = header["job"]
            self.cancelled.add(job)
            self.accums.pop(job, None)
            for w in [w for w in self.work if w["job"] == job]:
                self.work.remove(w)
                self.link.send({"op": "done", "job": job, "item": w["item"],
                                "kind": w["kind"], "discarded": True})

    def _handle_view(self, op, header):
        sid = header["sid"]
        if op == "view_stop":
            self.views.pop(sid, None)
            if self.loaded and self.loaded[1:] == ("view", sid):
                self.loaded = None
            return
        v = self.views.get(sid)
        if v is None:
            v = self.views[sid] = ViewState(sid)
        if op == "scene":
            v.blend = header["blend"]
            v.files = header.get("files", {})
            v.scene = header["scene"]
            v.layer = header.get("layer", "")
            v.frame = header.get("frame")
            v.xforms.clear()
            v.pending_xforms.clear()
            vis = header.get("visible")
            v.visible = set(vis) if vis is not None else None
            v.visible_dirty = True
            # The in-memory scene may carry transforms the new file lacks.
            if self.loaded and self.loaded[1:] == ("view", sid):
                self.loaded = None
        elif op == "xforms":
            v.xforms.update(header["objects"])
            v.pending_xforms.update(header["objects"])
        elif op == "visible":
            v.visible = set(header["objects"])
            v.visible_dirty = True
        elif op == "frame":
            v.frame = header["frame"]
        elif op == "view":
            v.view = header
        v.reset()

    # Main loop --------------------------------------------------------------

    def poll(self):
        """Handle messages, finish and start renders. Returns True while rendering."""
        while True:
            try:
                header, payload = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle(header, payload)

        if self.current is not None:
            state = self.runner.poll()
            if state == "busy":
                return True
            task, ctx = self.current
            self.current = None
            try:
                self._finish(task, ctx, state)
            except Exception as e:
                self._failed(task, e)

        if not self.alive:
            return False
        task = self._next_task()
        if task is None:
            self.report_status("Idle")
            return False
        try:
            self._start(task)
        except _Deferred:
            self.current = None
            if task[0] == "work":
                self.work.appendleft(task[1])
            return True
        except Exception as e:
            self.current = None
            self._failed(task, e)
        return self.current is not None

    def _next_task(self):
        if self.bench_stage is not None:
            return ("bench", None)
        if self.work:
            return ("work", self.work.popleft())
        wanting = [v for v in self.views.values() if v.wants]
        if wanting:
            self._rr += 1
            return ("view", wanting[self._rr % len(wanting)])
        return None

    def _start(self, task):
        kind, arg = task
        if kind == "bench":
            self._start_bench()
        elif kind == "work":
            self._start_work(arg)
        else:
            self._start_view(arg)

    def _finish(self, task, ctx, state):
        kind, arg = task
        if kind == "bench":
            self._finish_bench(ctx, state)
        elif kind == "work":
            self._finish_work(arg, ctx, state)
        else:
            self._finish_view(arg, ctx, state)

    def _failed(self, task, error):
        traceback.print_exc()
        kind, arg = task
        text = f"{type(error).__name__}: {error}"
        log(f"{kind} failed: {text}")
        if kind == "work":
            self.link.send({"op": "error", "job": arg["job"], "item": arg["item"], "text": text})
        elif kind == "view":
            arg.finished = True
            self.link.send({"op": "error", "sid": arg.sid, "text": text})
        else:
            self.bench_stage = None
            self.score = 0.001
            self.link.send({"op": "score", "score": self.score, "text": text})

    def _render(self, task, scene, layer, **ctx):
        if self.gui and self.just_loaded:
            self.just_loaded = False
            raise _Deferred()
        ctx["t0"] = time.monotonic()
        self.current = (task, ctx)
        self.runner.start(scene, layer)

    # Scene loading ----------------------------------------------------------

    def _open(self, key, files, scene_name):
        self.loaded = None
        bpy.ops.wm.open_mainfile(filepath=blob_path(key), load_ui=False, use_scripts=False)
        for img in bpy.data.images:
            if img.packed_file or img.library or img.source != "FILE":
                continue
            fkey = files.get(os.path.normpath(img.filepath_raw))
            if fkey and os.path.exists(blob_path(fkey)):
                img.filepath = blob_path(fkey)
        scene = bpy.data.scenes[scene_name]
        # The master's engine is REMOTE_CYCLES; Blender would fall back to EEVEE.
        scene.render.engine = "CYCLES"
        scene.cycles.device = self.device
        scene.render.use_persistent_data = True
        self.applied_frame = None
        self.just_loaded = True
        if self.gui:
            # Keep this node's own viewports from rendering with Cycles too.
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == "VIEW_3D" and area.spaces.active.shading.type == "RENDERED":
                        area.spaces.active.shading.type = "SOLID"
        return scene

    def _set_frame(self, scene, frame):
        if frame is not None and frame != self.applied_frame:
            f = float(frame)
            scene.frame_set(int(f), subframe=f - int(f))
            self.applied_frame = frame

    def _output(self, scene, name, multilayer, depth, codec):
        r = scene.render
        ims = r.image_settings
        ims.media_type = "MULTI_LAYER_IMAGE" if multilayer else "IMAGE"
        ims.file_format = "OPEN_EXR_MULTILAYER" if multilayer else "OPEN_EXR"
        ims.color_depth = depth
        ims.exr_codec = codec
        if not multilayer:
            ims.color_mode = "RGBA"
        r.filepath = os.path.join(self.work_dir, name)
        r.use_file_extension = False

    # Benchmark ----------------------------------------------------------------

    def _start_bench(self):
        if self.bench_stage == 0:
            scene = benchmark.build_scene(self.device, samples=1)
            self.report_status("Benchmarking (loading render kernels)")
        else:
            scene = bpy.data.scenes[benchmark.SCENE_NAME]
            scene.cycles.samples = self.bench_samples
            scene.update_tag()
            self.report_status("Benchmarking")
        self._output(scene, "bench.exr", False, "16", "NONE")
        self._render(("bench", None), scene, "")

    def _finish_bench(self, ctx, state):
        if state == "cancelled":
            raise RuntimeError("benchmark render was cancelled")
        if self.bench_stage == 0:
            self.bench_stage = 1
            return
        seconds = time.monotonic() - ctx["t0"]
        self.score = benchmark.score_from(seconds, self.bench_samples)
        self.bench_stage = None
        benchmark.remove_scene()
        if self.bench_samples == benchmark.SAMPLES:
            benchmark.store_score(self.device_name, self.score)
        log(f"benchmark: {self.score:.1f} Msamples/s ({seconds:.2f}s)")
        self.link.send({"op": "score", "score": self.score})

    # Final renders ------------------------------------------------------------

    def _start_work(self, w):
        if w["kind"] == "chunk":
            scene = self._prepare_chunk(w)
            self.report_status(f"Rendering samples {w['offset']}-{w['offset'] + w['count']} "
                               f"of frame {w['frame']:g}")
        else:
            scene = self._prepare_frame(w)
            self.report_status(f"Rendering frame {w['frame']:g}")
        self._render(("work", w), scene, w["layer"])

    def _prepare_frame(self, w):
        key = (w["blend"], "frame")
        if self.loaded != key:
            scene = self._open(w["blend"], w.get("files", {}), w["scene"])
            # The master composites, so send raw passes.
            scene.render.use_compositing = False
            scene.render.use_sequencer = False
            self._output(scene, "frame.exr", True, "32", "ZIP")
            self.loaded = key
        scene = bpy.data.scenes[w["scene"]]
        self._set_frame(scene, w["frame"])
        scene.update_tag()
        return scene

    def _prepare_chunk(self, w):
        key = (w["blend"], "chunk", w["denoise"])
        if self.loaded != key:
            scene = self._open(w["blend"], w.get("files", {}), w["scene"])
            r = scene.render
            r.use_compositing = False
            r.use_sequencer = False
            c = scene.cycles
            c.use_adaptive_sampling = False
            c.use_denoising = False
            c.use_sample_subset = True
            if w["denoise"]:
                # What the master needs to denoise the merged image.
                layer = scene.view_layers[w["layer"]]
                layer.cycles.denoising_store_passes = True
                layer.use_pass_vector = True
            self._output(scene, "chunk.exr", True, "32", "NONE")
            self.loaded = key
        scene = bpy.data.scenes[w["scene"]]
        self._set_frame(scene, w["frame"])
        c = scene.cycles
        c.samples = w["total"]
        c.sample_offset = w["offset"]
        c.sample_subset_length = w["count"]
        # Persistent data keeps old sampling settings unless tagged.
        scene.update_tag()
        return scene

    def _finish_work(self, w, ctx, state):
        reply = {"op": "done", "job": w["job"], "item": w["item"], "kind": w["kind"],
                 "seconds": time.monotonic() - ctx["t0"]}
        if w["job"] in self.cancelled:
            self.link.send({**reply, "discarded": True})
            return
        if state == "cancelled":
            raise RuntimeError("render was cancelled on the node")
        path = bpy.data.scenes[w["scene"]].render.filepath
        with open(path, "rb") as f:
            data = f.read()
        if w["kind"] == "frame":
            self.link.send(reply, data)
            return

        channels, width, height = exr.read(data)
        acc = self.accums.setdefault(w["job"], {"sum": {}, "first": None, "samples": 0,
                                                "width": width, "height": height})
        n = w["count"]
        for name, arr in channels.items():
            if protocol.is_averaged_channel(name):
                if name in acc["sum"]:
                    acc["sum"][name] += arr * n
                else:
                    acc["sum"][name] = arr.astype(np.float32) * n
        if w["offset"] == 0:
            acc["first"] = {name: np.array(arr, np.float32) for name, arr in channels.items()
                            if not protocol.is_averaged_channel(name)}
        acc["samples"] += n
        self.link.send({**reply, "samples": n})

    def _send_passes(self, job):
        acc = self.accums.pop(job, None)
        if acc is None or not acc["samples"]:
            self.link.send({"op": "passes", "job": job, "samples": 0})
            return
        channels = {name: s / acc["samples"] for name, s in acc["sum"].items()}
        if acc["first"]:
            channels.update(acc["first"])
        meta, data = protocol.pack_channels(channels)
        self.link.send({"op": "passes", "job": job, "samples": acc["samples"],
                        "has_first": acc["first"] is not None, "width": acc["width"],
                        "height": acc["height"], "meta": meta, "compressed": True}, data)

    # Viewport -----------------------------------------------------------------

    def _prepare_view(self, v):
        key = (v.blend, "view", v.sid)
        if self.loaded != key:
            scene = self._open(v.blend, v.files, v.scene)
            r = scene.render
            r.use_compositing = False
            r.use_sequencer = False
            r.use_border = False
            r.use_motion_blur = False
            r.use_multiview = False
            r.pixel_aspect_x = r.pixel_aspect_y = 1.0
            # Cycles uses the viewport simplify settings in the viewport.
            r.simplify_subdivision_render = r.simplify_subdivision
            r.simplify_child_particles_render = r.simplify_child_particles
            c = scene.cycles
            c.use_adaptive_sampling = False
            c.use_denoising = False
            c.use_animated_seed = False
            c.use_sample_subset = False
            c.time_limit = 0.0
            self._output(scene, "view.exr", False, "16", "NONE")

            cam = bpy.data.cameras.new(VIEW_CAMERA_NAME)
            cam.sensor_fit = "HORIZONTAL"
            cam.sensor_width = 36.0
            ob = bpy.data.objects.new(VIEW_CAMERA_NAME, cam)
            scene.collection.objects.link(ob)
            v.cam_name = ob.name
            v.applied_frame = None
            v.pending_xforms = set(v.xforms)
            v.visible_dirty = True
            self.loaded = key
        scene = bpy.data.scenes[v.scene]

        if v.frame is not None and v.frame != v.applied_frame:
            f = float(v.frame)
            scene.frame_set(int(f), subframe=f - int(f))
            v.applied_frame = v.frame
        for name in v.pending_xforms:
            ob = bpy.data.objects.get(name)
            if ob is not None:
                ob.matrix_basis = to_matrix(v.xforms[name])
        v.pending_xforms.clear()
        if v.visible_dirty and v.visible is not None:
            layer = scene.view_layers.get(v.layer) or scene.view_layers[0]
            for ob in layer.objects:
                if ob.name != v.cam_name:
                    _set(ob, "hide_render", ob.name not in v.visible)
        v.visible_dirty = False
        return scene

    def _apply_view_camera(self, scene, v):
        view = v.view
        ob = bpy.data.objects[v.cam_name]
        ob.matrix_world = to_matrix(view["matrix"])
        cam = ob.data
        if view["ortho"]:
            _set(cam, "type", "ORTHO")
            cam.ortho_scale = view["ortho_scale"]
        else:
            _set(cam, "type", "PERSP")
            cam.lens = view["lens"]
        cam.shift_x, cam.shift_y = view["shift"]
        cam.clip_start, cam.clip_end = view["clip"]
        dof = view.get("dof")
        cam.dof.use_dof = bool(dof)
        if dof:
            cam.dof.focus_object = bpy.data.objects.get(dof.get("focus_object") or "")
            cam.dof.focus_distance = dof["focus_distance"]
            cam.dof.aperture_fstop = dof["fstop"]
            cam.dof.aperture_blades = dof["blades"]
            cam.dof.aperture_rotation = dof["rotation"]
            cam.dof.aperture_ratio = dof["ratio"]
        if scene.camera != ob:
            scene.camera = ob

    def _start_view(self, v):
        scene = self._prepare_view(v)
        w, h = int(v.view["width"]), int(v.view["height"])
        c = scene.cycles
        if v.step == 0:
            v.target = c.preview_samples or 4096

        # First step after a change: 1 sample at half resolution, shown as-is.
        # Then growing chunks at full resolution with distinct seeds, averaged.
        big = w * h > 640 * 360
        preview = v.step == 0 and big
        if preview:
            samples, pct = 1, 50
        else:
            acc_steps = v.step - (1 if big else 0)
            samples = max(1, min(2 ** acc_steps, MAX_VIEW_CHUNK, v.target - v.done))
            pct = 100

        self._apply_view_camera(scene, v)
        r = scene.render
        _set(r, "resolution_x", w)
        _set(r, "resolution_y", h)
        _set(r, "resolution_percentage", pct)
        c.samples = samples
        c.seed = v.step
        scene.update_tag()
        self.report_status("Rendering viewport")
        self._render(("view", v), scene, v.layer, samples=samples, preview=preview,
                     generation=v.generation, path=r.filepath, transparent=r.film_transparent)

    def _finish_view(self, v, ctx, state):
        if state == "cancelled" or self.views.get(v.sid) is not v:
            return
        with open(ctx["path"], "rb") as f:
            img = exr.rgba(f.read())
        samples = ctx["samples"]
        stale = ctx["generation"] != v.generation
        if ctx["preview"] or stale:
            # Stale frames (the view changed mid-render) are still shown, so
            # navigating on a slow node shows something, but not accumulated.
            frame, shown = img, samples
        else:
            if v.accum is None or v.accum.shape != img.shape:
                v.accum = np.zeros(img.shape, np.float32)
                v.done = 0
            v.accum += img * samples
            v.done += samples
            frame, shown = v.accum / v.done, v.done
            if v.done >= v.target:
                v.finished = True
        if not stale:
            v.step += 1

        channels = 4 if ctx["transparent"] else 3
        data = np.ascontiguousarray(frame[::-1, :, :channels], dtype=np.float16).tobytes()
        compress = bool(v.view.get("compress"))
        if compress:
            data = zlib.compress(data, 1)
        self.link.send_latest(("frame", v.sid), {
            "op": "frame", "sid": v.sid, "width": frame.shape[1], "height": frame.shape[0],
            "channels": channels, "samples": shown, "target": v.target,
            "compressed": compress, "render_time": time.monotonic() - ctx["t0"],
        }, data)


# --------------------------------------------------------------------------
# Joining and leaving (one worker per Blender)

_worker = None
_state = {"status": ""}


def worker():
    return _worker


def status():
    if _worker is not None:
        return _worker.status
    return _state["status"]


def join(address, port, name, gui=True, cpu_only=False, score=None,
         bench_samples=benchmark.SAMPLES, local=False):
    """Connect to a master. Raises OSError/ConnectionError on failure."""
    global _worker
    if _worker is not None:
        leave(reload=False)
    devices = available_devices()
    selected = default_selection(devices, cpu_only)
    _worker = NodeWorker(address, port, name, devices, selected, gui, score=score,
                         bench_samples=bench_samples, local=local)
    if gui:
        if not bpy.app.timers.is_registered(_tick):
            bpy.app.timers.register(_tick, persistent=True)
    _state["status"] = ""
    log(f"joined master {_worker.master_name} at {address}:{port}, rendering on {_worker.device_name}")
    return _worker


def leave(reload=True, reason=""):
    global _worker
    w, _worker = _worker, None
    if w is None:
        return
    w.close()
    _state["status"] = reason
    if w.gui:
        if reload:
            # Don't leave the master's scene open where it could be edited.
            bpy.ops.wm.read_homefile()
    log(f"left master{': ' + reason if reason else ''}")


def _tick():
    w = _worker
    if w is None:
        return None
    try:
        busy = w.poll()
    except Exception:
        traceback.print_exc()
        busy = False
    if not w.alive and w.current is None:
        leave(reason=w.error or "Disconnected")
        return None
    return 0.02 if busy else 0.1


def run_headless(address, port, name, cpu_only=False, score=None,
                 bench_samples=benchmark.SAMPLES, local=False):
    """Blocking loop for a node without a window."""
    w = join(address, port, name, gui=False, cpu_only=cpu_only, score=score,
             bench_samples=bench_samples, local=local)
    while w.alive:
        if not w.poll():
            w.wait_event(0.2)
    leave(reload=False, reason=w.error or "Disconnected")


def register():
    if _on_render_complete not in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.append(_on_render_complete)
    if _on_render_cancel not in bpy.app.handlers.render_cancel:
        bpy.app.handlers.render_cancel.append(_on_render_cancel)


def unregister():
    if _worker is not None:
        leave(reload=False)
    if _on_render_complete in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.remove(_on_render_complete)
    if _on_render_cancel in bpy.app.handlers.render_cancel:
        bpy.app.handlers.render_cancel.remove(_on_render_cancel)
