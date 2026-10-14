"""The "Remote Cycles" render engine: Cycles settings and shaders locally,
rendering on the cluster's nodes."""

import os

import blf
import bpy
import gpu
from gpu_extras.presets import draw_texture_2d
from mathutils import Matrix

from . import exr, master, sync
from .master import FramesJob, SplitJob, ViewportSession
from .sync import flat_matrix


def get_prefs():
    return bpy.context.preferences.addons[__package__].preferences


def view_params(context, scene):
    """Describe the viewport as a camera: the node renders through it."""
    region, rv3d = context.region, context.region_data
    view = camera_from_matrices(rv3d.window_matrix, rv3d.view_matrix, rv3d.is_perspective,
                                region.width, region.height)
    camera = scene.camera
    if rv3d.view_perspective == "CAMERA" and camera and camera.type == "CAMERA":
        dof = camera.data.dof
        if dof.use_dof:
            view["dof"] = {
                "focus_object": dof.focus_object.name if dof.focus_object else None,
                "focus_distance": dof.focus_distance, "fstop": dof.aperture_fstop,
                "blades": dof.aperture_blades, "rotation": dof.aperture_rotation,
                "ratio": dof.aperture_ratio,
            }
    return view


def camera_from_matrices(P, view_matrix, is_perspective, width, height):
    """Camera settings reproducing projection ``P`` (a 36 mm sensor, fit horizontally)."""
    cam = view_matrix.inverted()
    # Shifts are relative to the frustum width.
    aspect = P[0][0] / P[1][1]
    view = {"op": "view", "width": width, "height": height,
            "ortho": not is_perspective, "lens": 50.0, "ortho_scale": 1.0, "dof": None}
    if is_perspective:
        view["lens"] = 18.0 * P[0][0]  # 36 mm sensor
        view["shift"] = (P[0][2] / 2, P[1][2] / 2 * aspect)
        near, far = P[2][3] / (P[2][2] - 1), P[2][3] / (P[2][2] + 1)
    else:
        view["ortho_scale"] = 2.0 / P[0][0]
        view["shift"] = (-P[0][3] / 2, -P[1][3] / 2 * aspect)
        near, far = (P[2][3] + 1) / P[2][2], (P[2][3] - 1) / P[2][2]
        # Ortho viewports clip behind the eye; cameras cannot, so back off.
        if near < 1e-3:
            back = 1e-3 - near
            cam = cam @ Matrix.Translation((0, 0, back))
            near, far = near + back, far + back
    view["clip"] = (near, far)
    view["matrix"] = flat_matrix(cam)
    return view


def animation_items(scene, layer):
    """(frame, view layer) pairs Blender will ask for, from the current frame on."""
    r = scene.render
    layers = [layer] if r.use_single_layer else [vl.name for vl in scene.view_layers if vl.use]
    if layer not in layers:
        layers = [layer]
    items = []
    for f in range(scene.frame_current, scene.frame_end + 1, max(1, scene.frame_step)):
        if f != scene.frame_current and not r.use_overwrite and os.path.exists(r.frame_path(frame=f)):
            continue  # Blender skips frames that already exist
        items.extend((float(f), name) for name in layers)
    return items


class RemoteCyclesEngine(bpy.types.RenderEngine):
    bl_idname = "REMOTE_CYCLES"
    bl_label = "Remote Cycles"
    bl_use_preview = False
    bl_use_postprocess = True
    bl_use_eevee_viewport = True
    bl_use_shading_nodes_custom = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = None
        self.texture = None
        self.texture_version = 0

    def __del__(self):
        session = getattr(self, "session", None)
        if session is not None:
            session.close()

    def update_render_passes(self, scene=None, renderlayer=None):
        try:
            from cycles import engine as cycles_engine
        except ImportError:
            return
        cycles_engine.register_passes(self, scene, renderlayer)

    # Final render ---------------------------------------------------------

    def render(self, depsgraph):
        if self.is_preview:
            return
        cl = master.get()
        if cl is None:
            self.report({"ERROR"}, "Remote Cycles: not hosting a cluster "
                        f"({master.error() or 'this Blender joined another master'})")
            return
        scene = depsgraph.scene.original
        layer = depsgraph.view_layer.name
        r = scene.render
        scale = r.resolution_percentage / 100
        size = (int(r.resolution_x * scale), int(r.resolution_y * scale))
        with cl.lock:
            nodes = cl.render_nodes()
        if not nodes:
            self.report({"ERROR"}, "Remote Cycles: no render nodes have joined this master")
            return
        if self.is_animation:
            self._render_animation(cl, scene, layer, size)
        else:
            self._render_still(cl, scene, layer, size, len(nodes))

    def _wait(self, cl, job, done, progress):
        """Wait until ``done()``; False if cancelled or failed."""
        with cl.cond:
            while True:
                if self.test_break():
                    cl.cancel(job)
                    return False
                if job.error:
                    self.report({"ERROR"}, f"Remote Cycles: {job.error}")
                    cl.cancel(job)
                    return False
                if done():
                    return True
                if not cl.render_nodes():
                    self.update_stats("", "Remote Cycles: waiting for a render node to join")
                else:
                    text, fraction = progress()
                    self.update_stats("", text)
                    if fraction is not None:
                        self.update_progress(fraction)
                cl.cond.wait(0.25)

    def _render_still(self, cl, scene, layer, size, node_count):
        c = scene.cycles
        denoise = c.use_denoising
        frame = scene.frame_current_final
        self.update_stats("", "Remote Cycles: preparing scene")
        snap = sync.Snapshot()
        # Denoising a merged image needs the vector pass, which motion blur rules out.
        if node_count >= 2 and not (denoise and scene.render.use_motion_blur):
            job = SplitJob(cl, snap, scene.name, frame, layer, c.samples, denoise)
            cl.submit(job)

            def progress():
                busy = sorted({n.name for n in cl.nodes.values() if n.inflight and n.inflight[0] == job.id})
                return (f"Remote Cycles: {job.samples_done}/{job.total} samples on {', '.join(busy) or '...'}",
                        job.samples_done / max(1, job.total))
            if not self._wait(cl, job, lambda: job.phase == "merge", progress):
                return
            try:
                self.update_stats("", "Remote Cycles: merging")
                channels, total = job.merge()
                path = os.path.join(cl.work_dir, f"job{job.id}_merged.exr")
                exr.write(path, channels, *size, strings={f"cycles.{layer}.samples": str(total)})
                if denoise:
                    self.update_stats("", "Remote Cycles: denoising")
                    path = self._denoise(scene, layer, path)
                self._load(path, size, layer)
            finally:
                cl.finish(job)
            return

        job = FramesJob(cl, snap, scene.name, [(frame, layer)])
        cl.submit(job)
        item = job.item(frame, layer)

        def progress():
            names = [cl.nodes[n].name for n in item["nodes"] if n in cl.nodes]
            return f"Remote Cycles: rendering on {', '.join(names) or '...'}", None
        try:
            if self._wait(cl, job, lambda: item["state"] == "done", progress):
                self._load(item["result"], size, layer)
        finally:
            cl.finish(job)

    def _render_animation(self, cl, scene, layer, size):
        frame = scene.frame_current_final
        with cl.lock:
            job = cl.anim_job
            item = job.item(frame, layer) if job and job.scene == scene.name else None
            reuse = item is not None and item["state"] != "consumed"
        if not reuse:
            if job is not None:
                cl.cancel(job)
            self.update_stats("", "Remote Cycles: preparing animation")
            job = FramesJob(cl, sync.Snapshot(), scene.name, animation_items(scene, layer))
            with cl.lock:
                cl.anim_job = job
                cl.submit(job)
            item = job.item(frame, layer)

        def progress():
            done = sum(1 for i in job.items.values() if i["state"] in ("done", "consumed"))
            names = [cl.nodes[n].name for n in item["nodes"] if n in cl.nodes]
            where = f"on {', '.join(names)}" if names else "queued"
            return (f"Remote Cycles: frame {frame:g} {where} | {done}/{len(job.items)} frames done",
                    None)
        if not self._wait(cl, job, lambda: item["state"] == "done", progress):
            return
        self._load(item["result"], size, layer)
        with cl.lock:
            job.consume(frame, layer)
            if job.finished:
                cl.finish(job)

    @staticmethod
    def _denoise(scene, layer, path):
        import _cycles
        out = path[:-4] + "_denoised.exr"
        view_layer = scene.view_layers[layer]
        _cycles.denoise(bpy.context.preferences.as_pointer(), scene.as_pointer(),
                        view_layer.as_pointer(), input=[path], output=[out])
        return out

    def _load(self, path, size, layer):
        result = self.begin_result(0, 0, size[0], size[1], layer=layer)
        try:
            result.load_from_file(path)
        finally:
            self.end_result(result)

    # Viewport -------------------------------------------------------------

    def _session(self):
        if self.session is None:
            cl = master.get()
            if cl is None:
                return None
            self.session = ViewportSession(cl, compress=get_prefs().compress)
        return self.session

    def view_update(self, context, depsgraph):
        s = self._session()
        if s is None:
            return
        scene = depsgraph.scene
        s.scene_name = scene.name
        s.layer_name = depsgraph.view_layer.name
        visible = sorted(ob.name for ob in context.view_layer.objects
                         if ob.visible_get(viewport=context.space_data))
        if s.node is None:
            s.visible = visible
            return
        if not s.synced:
            s.visible = visible
            s.full_sync()
            return
        if visible != s.visible:
            s.visible = visible
            s.send_visible()
        if scene.frame_current_final != s.frame_number:
            s.frame_number = scene.frame_current_final
            s.send_frame_number()

        full = False
        xforms = {}
        for u in depsgraph.updates:
            idb = u.id
            if isinstance(idb, bpy.types.Object):
                name = idb.name
                if u.is_updated_geometry:
                    if name in s.ignore_geometry:
                        s.ignore_geometry.discard(name)
                    else:
                        full = True
                if u.is_updated_transform:
                    xforms[name] = flat_matrix(idb.original.matrix_basis)
                if not (u.is_updated_geometry or u.is_updated_transform):
                    # Selection, visibility or a render property; only the
                    # last needs a resync.
                    if sync.object_fingerprint(idb.original) != s.object_fp.get(name):
                        full = True
            elif isinstance(idb, bpy.types.Scene):
                if sync.scene_fingerprint(idb.original) != s.scene_fp:
                    full = True
            elif idb.id_type not in sync.IGNORED_ID_TYPES:
                full = True

        if full:
            s.schedule_sync()
        elif xforms:
            s.send_xforms(xforms)

    def view_draw(self, context, depsgraph):
        region = context.region
        s = self._session()
        if s is None:
            self._draw_message(region, "Remote Cycles: not hosting a cluster "
                               f"({master.error() or 'this Blender joined another master'})")
            return

        s.set_view(view_params(context, depsgraph.scene))

        frame = s.frame
        if frame is not None and frame.version != self.texture_version:
            h, w = frame.pixels.shape[:2]
            buf = gpu.types.Buffer("FLOAT", w * h * 4, frame.pixels.ravel())
            self.texture = gpu.types.GPUTexture((w, h), format="RGBA32F", data=buf)
            self.texture_version = frame.version
        s.drawn_version = s.frame_version if frame is None else frame.version

        if self.texture is not None:
            gpu.state.blend_set("ALPHA_PREMULT")
            self.bind_display_space_shader(depsgraph.scene)
            draw_texture_2d(self.texture, (0, 0), region.width, region.height)
            self.unbind_display_space_shader()
            gpu.state.blend_set("NONE")

        self.update_stats("", s.status_line())
        if s.error or self.texture is None:
            self._draw_message(region, s.status_line())

    @staticmethod
    def _draw_message(region, text):
        font = 0
        size = 12 * bpy.context.preferences.system.ui_scale
        blf.size(font, size)
        blf.color(font, 1.0, 1.0, 1.0, 1.0)
        blf.enable(font, blf.SHADOW)
        blf.shadow(font, 3, 0.0, 0.0, 0.0, 1.0)
        tw, _th = blf.dimensions(font, text)
        blf.position(font, max(10, (region.width - tw) / 2), region.height / 2, 0)
        blf.draw(font, text)
        blf.disable(font, blf.SHADOW)


def _cycles_panels():
    stack = list(bpy.types.Panel.__subclasses__())
    while stack:
        panel = stack.pop()
        stack.extend(panel.__subclasses__())
        compat = getattr(panel, "COMPAT_ENGINES", None)
        if isinstance(compat, set) and "CYCLES" in compat:
            yield panel


def patch_cycles_panels():
    """Show Cycles' settings panels (samples, light paths, materials...) for our engine."""
    for panel in _cycles_panels():
        panel.COMPAT_ENGINES.add(RemoteCyclesEngine.bl_idname)


def unpatch_cycles_panels():
    for panel in _cycles_panels():
        panel.COMPAT_ENGINES.discard(RemoteCyclesEngine.bl_idname)


def register():
    bpy.utils.register_class(RemoteCyclesEngine)
    patch_cycles_panels()


def unregister():
    unpatch_cycles_panels()
    bpy.utils.unregister_class(RemoteCyclesEngine)
