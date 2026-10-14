"""Minimal OpenEXR reader/writer for uncompressed scanline images.

Nodes ask Blender to write renders with ``exr_codec='NONE'`` so they can be
decoded with numpy alone (Blender's Python has no OpenImageIO). Blender 5
writes multilayer files as multi-part EXRs, one part per pass; both layouts
are supported for reading. Writing produces a single-part file whose channel
names follow the ``Layer.Pass.Channel`` convention, which Blender and the
Cycles denoiser read as a multilayer image.
"""

import struct

import numpy as np

_MAGIC = b"\x76\x2f\x31\x01"
_PIXEL_DTYPES = {0: "<u4", 1: "<f2", 2: "<f4"}
_DTYPE_CODES = {np.dtype("<f2"): 1, np.dtype("<f4"): 2}
_FLAG_TILED = 0x200
_FLAG_LONG_NAMES = 0x400
_FLAG_MULTIPART = 0x1000


def _cstr(buf, pos):
    end = buf.index(b"\0", pos)
    return buf[pos:end].decode(), end + 1


def _read_header(data, pos):
    attrs = {}
    while True:
        name, pos = _cstr(data, pos)
        if not name:
            return attrs, pos
        _type, pos = _cstr(data, pos)
        (size,) = struct.unpack_from("<i", data, pos)
        pos += 4
        attrs[name] = data[pos:pos + size]
        pos += size


def _channels(attrs):
    chl = attrs["channels"]
    out = []
    pos = 0
    while chl[pos] != 0:
        name, pos = _cstr(chl, pos)
        ptype, _plinear, xs, ys = struct.unpack_from("<iB3xii", chl, pos)
        pos += 16
        if xs != 1 or ys != 1:
            raise ValueError("subsampled channels not supported")
        out.append((name, _PIXEL_DTYPES[ptype]))
    return out


def read(data):
    """Return ``(channels, width, height)``.

    ``channels`` maps channel name to a (height, width) array, top row first.
    """
    data = memoryview(data).cast("B")
    if bytes(data[:4]) != _MAGIC:
        raise ValueError("not an OpenEXR file")
    (version,) = struct.unpack_from("<i", data, 4)
    if version & _FLAG_TILED:
        raise ValueError("tiled EXR not supported")
    multipart = bool(version & _FLAG_MULTIPART)
    raw = bytes(data)

    pos = 8
    headers = []
    while True:
        attrs, pos = _read_header(raw, pos)
        if not attrs:
            break
        headers.append(attrs)
        if not multipart:
            break

    out = {}
    width = height = 0
    tables = []
    for attrs in headers:
        if attrs["compression"][0] != 0:
            raise ValueError("compressed EXR not supported, write with codec NONE")
        xmin, ymin, xmax, ymax = struct.unpack("<4i", attrs["dataWindow"])
        width, height = xmax - xmin + 1, ymax - ymin + 1
        tables.append(np.frombuffer(raw, "<u8", height, pos))
        pos += 8 * height

    for part, (attrs, offsets) in enumerate(zip(headers, tables)):
        channels = _channels(attrs)
        prefix = [("part", "<i4")] if multipart else []
        line = np.dtype(prefix + [("y", "<i4"), ("size", "<i4")] +
                        [(name, dt, (width,)) for name, dt in channels])
        steps = np.diff(offsets.astype(np.int64))
        if np.all(steps == line.itemsize):
            lines = np.frombuffer(raw, line, height, int(offsets[0]))
        else:
            lines = np.array([np.frombuffer(raw, line, 1, int(o))[0] for o in offsets], dtype=line)
        lines = lines[np.argsort(lines["y"], kind="stable")]
        for name, _dt in channels:
            out[name] = lines[name]
    return out, width, height


def write(path, channels, width, height, strings=None):
    """Write ``{name: (height, width) array}`` as an uncompressed single-part EXR.

    float16 arrays are stored as half, everything else as float32.
    ``strings`` adds text header attributes, e.g. the ``cycles.<layer>.samples``
    metadata the Cycles denoiser needs.
    """
    names = sorted(channels)
    arrays = []
    chlist = b""
    for name in names:
        arr = np.asarray(channels[name])
        if arr.dtype != np.float16:
            arr = arr.astype("<f4", copy=False)
        arr = np.ascontiguousarray(arr, dtype=arr.dtype.newbyteorder("<"))
        arrays.append(arr)
        chlist += name.encode() + b"\0" + struct.pack("<iB3xii", _DTYPE_CODES[arr.dtype], 0, 1, 1)
    chlist += b"\0"

    def attr(name, type_, value):
        return name.encode() + b"\0" + type_.encode() + b"\0" + struct.pack("<i", len(value)) + value

    window = struct.pack("<4i", 0, 0, width - 1, height - 1)
    header = b"".join([
        attr("channels", "chlist", chlist),
        attr("compression", "compression", b"\0"),
        attr("dataWindow", "box2i", window),
        attr("displayWindow", "box2i", window),
        attr("lineOrder", "lineOrder", b"\0"),
        attr("pixelAspectRatio", "float", struct.pack("<f", 1.0)),
        attr("screenWindowCenter", "v2f", struct.pack("<2f", 0.0, 0.0)),
        attr("screenWindowWidth", "float", struct.pack("<f", 1.0)),
    ] + [attr(k, "string", str(v).encode()) for k, v in (strings or {}).items()]) + b"\0"

    long_names = any(len(n) > 31 for n in names)
    version = 2 | (_FLAG_LONG_NAMES if long_names else 0)
    line_bytes = sum(a.dtype.itemsize * width for a in arrays)
    start = 8 + len(header) + 8 * height
    offsets = start + np.arange(height, dtype="<u8") * (8 + line_bytes)

    lines = np.empty(height, np.dtype([("y", "<i4"), ("size", "<i4")] +
                                      [(f"c{i}", a.dtype, (width,)) for i, a in enumerate(arrays)]))
    lines["y"] = np.arange(height)
    lines["size"] = line_bytes
    for i, a in enumerate(arrays):
        lines[f"c{i}"] = a
    with open(path, "wb") as f:
        f.write(_MAGIC + struct.pack("<i", version) + header + offsets.tobytes() + lines.tobytes())


def rgba(data):
    """Decode to a (height, width, 4) float32 array, top row first.

    Channels are matched by suffix so "R" and "ViewLayer.Combined.R" both work.
    """
    channels, width, height = read(data)

    def find(suffix):
        for name, arr in channels.items():
            if name == suffix or name.endswith("." + suffix):
                return arr
        return None

    out = np.empty((height, width, 4), np.float32)
    for i, c in enumerate("RGBA"):
        arr = find(c)
        out[..., i] = 1.0 if arr is None else arr
    return out
