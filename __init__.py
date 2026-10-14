"""Remote Cycles: a render cluster for Blender over the LAN.

Every machine runs this add-on. The Blender with your project hosts a
cluster (the master); Blenders on other machines join it as render nodes and
render in its place. The "Remote Cycles" render engine then renders the
viewport on the best node, splits still images across all nodes by samples,
and hands animation frames to whichever node is free, fastest first.
"""

import time
import traceback

import bpy

from . import discovery, engine, master, node, ui, windows

_state = {"last_redraw": 0.0, "ui_version": None, "last_host_try": 0.0, "last_join_try": 0.0,
          "last_local_try": 0.0, "display_checked": False}


def _repair_render_display(prefs):
    """Version 0.3 and older switched "Render In" to "Keep User Interface"
    while joined as a node, and it could get saved that way. Put it back once."""
    if prefs.display_repaired:
        return
    prefs.display_repaired = True
    view = bpy.context.preferences.view
    if view.render_display_type == "NONE":
        view.render_display_type = "WINDOW"
        print("[remote-cycles] restored Preferences > Interface > Render In to New Window")


def _tag_redraw(rendered_views, panels):
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                if rendered_views and area.spaces.active.shading.type == "RENDERED":
                    area.tag_redraw()
                elif panels:
                    for region in area.regions:
                        if region.type == "UI":
                            region.tag_redraw()
            elif panels and area.type == "PROPERTIES":
                area.tag_redraw()


def _ui_version():
    cl = master.get()
    w = node.worker()
    return (
        cl.version if cl else None,
        cl.announce if cl else None,
        tuple((n.name, n.status, n.score, n.bandwidth, tuple(n.selected))
              for n in cl.nodes.values()) if cl else None,
        (w.status, w.score) if w else node.status(),
        tuple(m["id"] for m in ui.discovered_masters()),
    )


def _tick():
    now = time.monotonic()
    try:
        prefs = ui.get_prefs()
    except KeyError:
        return None

    # Host whenever we are not a node; retry if the port was busy.
    if node.worker() is None and master.get() is None and now - _state["last_host_try"] > 5:
        _state["last_host_try"] = now
        ui.start_hosting()

    cl = master.get()
    if cl is not None:
        # Announce, and render on this machine too, only while this Blender
        # actually uses Remote Cycles.
        scene = bpy.context.scene
        cl.announce = scene is not None and scene.render.engine == "REMOTE_CYCLES"
        if cl.announce and prefs.use_local and not master.local_running() \
                and now - _state["last_local_try"] > 10:
            _state["last_local_try"] = now
            master.start_local(cl.port, cl.name)

    # Rejoin the last master when it shows up, unless this Blender is a
    # master itself (using Remote Cycles, or with nodes attached).
    acting_master = cl is not None and (cl.announce or cl.nodes)
    if node.worker() is None and prefs.auto_join and prefs.last_master and not acting_master \
            and not ui.state["user_left"] and now - _state["last_join_try"] > 5:
        for m in ui.discovered_masters():
            if m["name"] == prefs.last_master and list(m.get("blender", [])[:2]) == list(bpy.app.version[:2]):
                _state["last_join_try"] = now
                try:
                    ui.join(m["address"], m["port"], m["name"])
                except (OSError, ConnectionError) as e:
                    print(f"[remote-cycles] auto-join failed: {e}")
                break

    if not _state["display_checked"]:
        _state["display_checked"] = True
        _repair_render_display(prefs)

    if cl is not None:
        try:
            windows.tick(cl)
        except Exception:
            traceback.print_exc()

    rendered = master.tick()
    panels = False
    if now - _state["last_redraw"] > 0.5:
        version = _ui_version()
        panels = version != _state["ui_version"]
        _state["ui_version"] = version
        _state["last_redraw"] = now
    if rendered or panels:
        _tag_redraw(rendered, panels)
    return 1 / 30


def _late_patch():
    # Cycles may register after us at startup.
    engine.patch_cycles_panels()


def register():
    for cls in ui.classes:
        bpy.utils.register_class(cls)
    engine.register()
    node.register()
    windows.register()
    ui.state["listener"] = discovery.Listener()
    bpy.app.timers.register(_tick, first_interval=0.5, persistent=True)
    bpy.app.timers.register(_late_patch, first_interval=1.0, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if ui.state["listener"] is not None:
        ui.state["listener"].stop()
        ui.state["listener"] = None
    windows.unregister()
    node.unregister()
    master.stop()
    engine.unregister()
    for cls in reversed(ui.classes):
        bpy.utils.unregister_class(cls)
