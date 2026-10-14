"""Scene snapshots and change detection on the master."""

import os
import tempfile

import bpy

from . import protocol

_SNAPSHOT_PATH = os.path.join(tempfile.gettempdir(), f"remote_cycles_{os.getpid()}.blend")

# Data-block types whose updates never change what gets rendered.
IGNORED_ID_TYPES = {
    "SCREEN", "WORKSPACE", "WINDOWMANAGER", "BRUSH", "PALETTE",
    "PAINTCURVE", "TEXT", "SOUND", "MASK", "MOVIECLIP",
}

# Object properties covered by transform sync, visibility sync, or that
# only affect the UI.
_OBJECT_FP_SKIP = (
    "rna_type", "location", "rotation", "scale", "delta_", "matrix_",
    "bound_box", "dimensions", "show_", "empty_display", "active_",
    "hide_viewport", "hide_select", "mode", "is_", "tag", "users",
    "session_uid", "original", "name_full", "preview", "select",
)


def flat_matrix(m):
    return [v for row in m for v in row]


def write_snapshot():
    """Write every scene and what it depends on to a .blend, return its bytes."""
    bpy.data.libraries.write(_SNAPSHOT_PATH, set(bpy.data.scenes), path_remap="ABSOLUTE")
    with open(_SNAPSHOT_PATH, "rb") as f:
        return f.read()


def collect_files():
    """External image files the snapshot refers to, as {abs path: cache key}.

    The key comes from path, size and mtime rather than content, so big
    textures are not re-read on every sync."""
    files = {}
    for img in bpy.data.images:
        if img.packed_file or img.library or img.source != "FILE" or not img.filepath:
            continue
        path = os.path.normpath(bpy.path.abspath(img.filepath))
        try:
            st = os.stat(path)
        except OSError:
            continue
        files[path] = protocol.blob_hash(f"{path}\0{st.st_size}\0{st.st_mtime_ns}".encode())
    return files


class Snapshot:
    __slots__ = ("key", "data", "files")

    def __init__(self):
        self.data = write_snapshot()
        self.key = protocol.blob_hash(self.data)
        self.files = collect_files()


def _props_fp(struct, skip=()):
    if struct is None:
        return None
    out = []
    for p in struct.bl_rna.properties:
        pid = p.identifier
        if p.type == "COLLECTION" or pid.startswith(skip):
            continue
        try:
            v = getattr(struct, pid)
        except AttributeError:
            continue
        if p.type == "POINTER":
            v = v.name if isinstance(v, bpy.types.ID) else None
        elif getattr(p, "is_array", False):
            v = tuple(tuple(x) if hasattr(x, "__len__") else x for x in v)
        out.append(v)
    return tuple(out)


def scene_fingerprint(scene):
    return (
        scene.world.name if scene.world else None,
        _props_fp(scene.cycles, ("rna_type",)),
        _props_fp(scene.render, ("rna_type",)),
        _props_fp(getattr(scene, "cycles_curves", None), ("rna_type",)),
    )


def object_fingerprint(ob):
    return (
        _props_fp(ob, _OBJECT_FP_SKIP),
        _props_fp(getattr(ob, "cycles", None), ("rna_type",)),
        tuple(s.material.name if s.material else None for s in ob.material_slots),
        tuple((m.name, m.show_viewport, m.show_render) for m in ob.modifiers),
    )
