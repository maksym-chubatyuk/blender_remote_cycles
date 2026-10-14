"""One window per machine while an animation renders on the cluster.

Each window shows the last frame that machine finished; the image name in
its header says which frame that was and what it is rendering now.
Blender's own render window is reused for the first machine, so there are
as many windows as machines.
"""

import re

import bpy
from bpy.app.handlers import persistent

_MACHINE = "remote_cycles_machine"
_RENDERING = re.compile(r"(: |, )rendering [-\d.]+$")
_state = {"job": None, "windows": {}, "opened": set(), "closed": set(), "spare": None,
          "versions": {}}


def _alive(wm, window):
    return any(w == window for w in wm.windows)


def _largest_area(window):
    return max(window.screen.areas, key=lambda a: a.width * a.height)


def _render_window(wm):
    """Blender's render window, if the render opened one."""
    for window in wm.windows[1:]:
        for area in window.screen.areas:
            if area.type == "IMAGE_EDITOR":
                image = area.spaces.active.image
                if image is not None and image.type == "RENDER_RESULT":
                    return window, area
    return None


def _open_window(wm):
    main = wm.windows[0]
    with bpy.context.temp_override(window=main, area=_largest_area(main)):
        bpy.ops.wm.window_new()
    window = wm.windows[-1]
    area = _largest_area(window)
    area.type = "IMAGE_EDITOR"
    return window, area


def _image(machine, size):
    for image in bpy.data.images:
        if image.get(_MACHINE) == machine:
            return image
    image = bpy.data.images.new(f"{machine}: waiting for a frame", size[0], size[1], float_buffer=True)
    image[_MACHINE] = machine
    image.use_view_as_render = True  # same look as the render window
    return image


def _fit(window, area):
    region = next((r for r in area.regions if r.type == "WINDOW"), None)
    if region is None:
        return
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.image.view_all(fit_view=True)


def _finish():
    """The animation is over: drop "rendering N" from the titles."""
    _state["job"] = None
    for image in bpy.data.images:
        if image.get(_MACHINE):
            image.name = _RENDERING.sub("", image.name)


def tick(cluster):
    job = cluster.anim_job
    if job is None or job.cancelled:
        if _state["job"] is not None:
            _finish()
        return
    wm = bpy.context.window_manager
    if _state["job"] != job.id:
        _state.update(job=job.id, opened=set(), closed=set(), spare=_render_window(wm))

    scene = bpy.data.scenes.get(job.scene)
    if scene is None:
        return
    r = scene.render
    size = (max(1, r.resolution_x * r.resolution_percentage // 100),
            max(1, r.resolution_y * r.resolution_percentage // 100))

    with cluster.lock:
        nodes = sorted(cluster.render_nodes(), key=lambda n: -n.score)
        rendering = {}
        for n in nodes:
            if n.inflight and n.inflight[0] == job.id:
                item = job.items.get(n.inflight[1])
                if item:
                    rendering[n.name] = item["frame"]
        previews = {k: dict(v) for k, v in cluster.previews.items()}

    for n in nodes:
        name = n.name
        if name in _state["closed"]:
            continue
        entry = _state["windows"].get(name)
        if entry is not None and not _alive(wm, entry[0]):
            entry = None
            if name in _state["opened"]:
                _state["closed"].add(name)  # the user closed it; leave it closed
                continue
        if entry is None:
            spare = _state["spare"]
            if spare is not None and _alive(wm, spare[0]):
                entry = spare
            else:
                entry = _open_window(wm)
            _state["spare"] = None
            _state["windows"][name] = entry
            _state["opened"].add(name)
        window, area = entry

        image = _image(name, size)
        if area.spaces.active.image != image:
            area.spaces.active.image = image
        preview = previews.get(name)
        if preview and _state["versions"].get(name) != preview["version"]:
            first = image.source != "FILE"
            image.source = "FILE"
            image.filepath = preview["path"]
            image.reload()
            _state["versions"][name] = preview["version"]
            if first:
                _fit(window, area)

        parts = [name]
        if preview:
            parts.append(f"frame {preview['frame']:g} done")
        if name in rendering:
            parts.append(f"rendering {rendering[name]:g}")
        label = ": ".join(parts[:1]) + (": " + ", ".join(parts[1:]) if len(parts) > 1 else "")
        if image.name != label[:63]:
            image.name = label[:63]


@persistent
def _drop_previews(*_args):
    """The preview images point at temporary files; keep them out of saved files."""
    for image in [i for i in bpy.data.images if i.get(_MACHINE)]:
        bpy.data.images.remove(image)
    _state["versions"].clear()


def register():
    if _drop_previews not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(_drop_previews)


def unregister():
    if _drop_previews in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_drop_previews)
