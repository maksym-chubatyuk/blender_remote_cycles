"""Preferences, operators and panels."""

import json
import socket

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty

from . import master, node, protocol

# Shared with __init__: the discovery listener and session flags.
state = {"listener": None, "user_left": False}


def get_prefs():
    return bpy.context.preferences.addons[__package__].preferences


def machine_name():
    return get_prefs().machine_name.strip() or socket.gethostname()


def _device_choices(prefs):
    try:
        return json.loads(prefs.device_choices or "{}")
    except ValueError:
        return {}


def start_hosting():
    """Hosting is automatic whenever this Blender is not a node."""
    prefs = get_prefs()
    if node.worker() is None and master.get() is None:
        disabled = [n for n in prefs.disabled_nodes.split(",") if n]
        master.start(prefs.port, machine_name(), disabled, _device_choices(prefs))


def join(address, port, name=""):
    """Become a node of the master at address:port."""
    prefs = get_prefs()
    master.stop()
    try:
        w = node.join(address, port, machine_name(), gui=not bpy.app.background)
    except (OSError, ConnectionError):
        start_hosting()
        raise
    prefs.last_master = name or w.master_name
    state["user_left"] = False
    return w


def leave():
    node.leave()
    state["user_left"] = True
    start_hosting()


def discovered_masters():
    listener = state["listener"]
    if listener is None:
        return []
    cl = master.get()
    own = cl.id if cl else None
    return sorted((m for m in listener.masters() if m["id"] != own), key=lambda m: m["name"])


def _name_changed(prefs, context):
    name = machine_name()
    cl = master.get()
    if cl is not None:
        cl.name = name
        master.stop_local()  # restarted under the new name
    w = node.worker()
    if w is not None:
        w.rename(name)


def _port_changed(prefs, context):
    if master.get() is not None:
        master.stop()
        start_hosting()


def _use_local_changed(prefs, context):
    if not prefs.use_local:
        master.stop_local()


class RemoteCyclesPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    machine_name: StringProperty(
        name="Machine Name",
        description="Name other machines see for this one (defaults to the host name)",
        update=_name_changed,
    )
    port: IntProperty(
        name="Port",
        description="TCP port this Blender listens on for render nodes",
        default=protocol.CLUSTER_PORT, min=1, max=65535,
        update=_port_changed,
    )
    use_local: BoolProperty(
        name="Render on This Machine",
        description="Also render with this machine's own devices when it is the master. "
                    "Runs a background Blender, so the scene is held in memory twice",
        default=True,
        update=_use_local_changed,
    )
    auto_join: BoolProperty(
        name="Auto-Join",
        description="After you have joined a master once, join it again by itself "
                    "whenever it shows up on the network",
        default=True,
    )
    compress: BoolProperty(
        name="Compress Viewport Frames",
        description="Compress frames sent to the viewport. Helps on Wi-Fi, "
                    "costs CPU time on a wired LAN",
    )
    last_master: StringProperty(name="Last Master")
    join_address: StringProperty(
        name="Address",
        description="host or host:port of a master that discovery cannot see",
    )
    disabled_nodes: StringProperty(name="Disabled Nodes")
    device_choices: StringProperty(name="Device Choices")
    display_repaired: BoolProperty(name="Render Display Repaired")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        col = layout.column()
        col.prop(self, "machine_name", placeholder=socket.gethostname())
        col.prop(self, "port")
        col.prop(self, "use_local")
        col.prop(self, "auto_join")
        col.prop(self, "compress")
        layout.label(text="Pick which devices render in Render Properties > Render Cluster.",
                     icon="INFO")


class REMOTE_CYCLES_OT_join(bpy.types.Operator):
    """Join this master as a render node. This Blender then loads the
    master's scene and renders for it until you leave"""

    bl_idname = "remote_cycles.join"
    bl_label = "Join"

    master_id: StringProperty(options={"HIDDEN"})
    address: StringProperty(options={"HIDDEN"})

    def invoke(self, context, event):
        if bpy.data.is_dirty:
            return context.window_manager.invoke_confirm(
                self, event, title="Join render cluster?",
                message="This Blender will load the master's scene; unsaved changes here will be lost.",
                confirm_text="Join")
        return self.execute(context)

    def execute(self, context):
        name = ""
        if self.master_id:
            found = [m for m in discovered_masters() if m["id"] == self.master_id]
            if not found:
                self.report({"ERROR"}, "That master is no longer on the network")
                return {"CANCELLED"}
            host, port, name = found[0]["address"], found[0]["port"], found[0]["name"]
        else:
            text = (self.address or get_prefs().join_address).strip()
            if not text:
                self.report({"ERROR"}, "Enter the master's address")
                return {"CANCELLED"}
            host, _, port = text.partition(":")
            port = int(port) if port else protocol.CLUSTER_PORT
        try:
            w = join(host, port, name)
        except (OSError, ConnectionError, ValueError) as e:
            self.report({"ERROR"}, f"Cannot join {host}:{port}: {e}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Joined {w.master_name}, rendering on {w.device_name}")
        return {"FINISHED"}


class REMOTE_CYCLES_OT_leave(bpy.types.Operator):
    """Stop rendering for the master and reload your start-up file"""

    bl_idname = "remote_cycles.leave"
    bl_label = "Leave Cluster"

    def execute(self, context):
        leave()
        return {"FINISHED"}


class REMOTE_CYCLES_OT_toggle_node(bpy.types.Operator):
    """Use this machine for rendering, or not"""

    bl_idname = "remote_cycles.toggle_node"
    bl_label = "Toggle Machine"

    name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        cl = master.get()
        if cl is None:
            return {"CANCELLED"}
        cl.set_enabled(self.name, self.name in cl.disabled)
        get_prefs().disabled_nodes = ",".join(sorted(cl.disabled))
        return {"FINISHED"}


class REMOTE_CYCLES_OT_toggle_device(bpy.types.Operator):
    """Render with this device, or not"""

    bl_idname = "remote_cycles.toggle_device"
    bl_label = "Toggle Device"

    node_id: IntProperty(options={"HIDDEN"})
    device_id: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        cl = master.get()
        n = cl.nodes.get(self.node_id) if cl else None
        if n is None:
            return {"CANCELLED"}
        selected = [d for d in n.selected if d != self.device_id]
        if self.device_id not in n.selected:
            selected.append(self.device_id)
        if not selected:
            self.report({"WARNING"}, "Keep at least one device; untick the machine to leave it out")
            return {"CANCELLED"}
        cl.set_devices(n, selected)
        get_prefs().device_choices = json.dumps(cl.device_choices)
        return {"FINISHED"}


class REMOTE_CYCLES_OT_resync(bpy.types.Operator):
    """Send a fresh copy of the scene to the viewport's render node"""

    bl_idname = "remote_cycles.resync"
    bl_label = "Resync Scene"

    def execute(self, context):
        cl = master.get()
        if cl is not None:
            for s in cl.sessions.values():
                s.schedule_sync(0)
        return {"FINISHED"}


def draw_hosting(layout, context):
    cl = master.get()
    prefs = get_prefs()
    if cl is None:
        if node.worker() is None:
            layout.label(text=master.error() or "Starting...", icon="ERROR" if master.error() else "INFO")
        return
    layout.label(text=f"Master: {cl.name}", icon="NETWORK_DRIVE")
    if not cl.announce:
        col = layout.column(align=True)
        col.label(text="Not visible to other machines:", icon="HIDE_ON")
        col.label(text="set the render engine to Remote Cycles.")
    layout.prop(prefs, "use_local")

    with cl.lock:
        nodes = sorted(cl.nodes.values(), key=lambda n: (not n.local, -(n.score or 0)))
        sessions = list(cl.sessions.values())
    if not any(not n.local for n in nodes):
        col = layout.column(align=True)
        col.label(text="No other machines yet. On a GPU machine:")
        col.label(text="3D view sidebar (N) > Cluster > Join.")
    col = layout.column(align=True)
    for n in nodes:
        box = col.box()
        row = box.row()
        op = row.operator(REMOTE_CYCLES_OT_toggle_node.bl_idname, text="", emboss=False,
                          icon="CHECKBOX_HLT" if n.enabled else "CHECKBOX_DEHLT")
        op.name = n.name
        row.label(text=n.name, icon="HOME" if n.local else "DESKTOP")
        row.label(text=n.describe())
        if n.enabled:
            devices = box.column(align=True)
            for d in n.devices:
                drow = devices.row()
                drow.separator(factor=2.0)
                op = drow.operator(REMOTE_CYCLES_OT_toggle_device.bl_idname, text="", emboss=False,
                                   icon="CHECKBOX_HLT" if d["id"] in n.selected else "CHECKBOX_DEHLT")
                op.node_id = n.id
                op.device_id = d["id"]
                drow.label(text=f"{d['type']}: {d['name']}")
        box.label(text=n.status, icon="RENDER_STILL" if n.inflight else "BLANK1")
    for s in sessions:
        if s.node is not None:
            layout.label(text=f"Viewport renders on {s.node.name}", icon="VIEW3D")


def draw_joining(layout):
    w = node.worker()
    if w is not None:
        box = layout.box()
        box.label(text=f"Rendering for {w.master_name}", icon="LINKED")
        box.label(text=f"Devices: {w.device_name}")
        if w.score:
            box.label(text=f"Benchmark: {w.score:.0f} Msamples/s")
        box.label(text=w.status)
        box.label(text="Devices are chosen on the master.", icon="INFO")
        box.operator(REMOTE_CYCLES_OT_leave.bl_idname, icon="UNLINKED")
        return
    prefs = get_prefs()
    if node.status():
        layout.label(text=node.status(), icon="INFO")
    masters = discovered_masters()
    layout.label(text="Masters on the network:" if masters else "No masters found on the network")
    ours = list(bpy.app.version[:2])
    for m in masters:
        row = layout.row()
        compatible = list(m.get("blender", [])[:2]) == ours
        row.label(text=f"{m['name']}: {m.get('project') or 'untitled'}",
                  icon="BLENDER" if compatible else "ERROR")
        sub = row.row()
        sub.enabled = compatible
        op = sub.operator(REMOTE_CYCLES_OT_join.bl_idname, text="Join")
        op.master_id = m["id"]
        if not compatible:
            layout.label(text=f"  needs Blender {'.'.join(map(str, m.get('blender', [])[:2]))}")
    row = layout.row(align=True)
    row.prop(prefs, "join_address", text="")
    op = row.operator(REMOTE_CYCLES_OT_join.bl_idname, text="Join Address")
    op.master_id = ""
    layout.prop(prefs, "auto_join")


class RENDER_PT_remote_cycles(bpy.types.Panel):
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"
    bl_label = "Render Cluster"
    bl_order = -100
    COMPAT_ENGINES = {"REMOTE_CYCLES"}

    @classmethod
    def poll(cls, context):
        return context.engine in cls.COMPAT_ENGINES

    def draw(self, context):
        draw_hosting(self.layout, context)
        cl = master.get()
        if cl is not None and cl.sessions:
            self.layout.operator(REMOTE_CYCLES_OT_resync.bl_idname, icon="FILE_REFRESH")


class VIEW3D_PT_remote_cycles(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Cluster"
    bl_label = "Render Cluster"

    def draw(self, context):
        if node.worker() is None:
            draw_hosting(self.layout, context)
            self.layout.separator()
        draw_joining(self.layout)


classes = (
    RemoteCyclesPreferences,
    REMOTE_CYCLES_OT_join,
    REMOTE_CYCLES_OT_leave,
    REMOTE_CYCLES_OT_toggle_node,
    REMOTE_CYCLES_OT_toggle_device,
    REMOTE_CYCLES_OT_resync,
    RENDER_PT_remote_cycles,
    VIEW3D_PT_remote_cycles,
)
