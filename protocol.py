"""Wire protocol shared by masters and nodes.

Every TCP message is a 4-byte big-endian header length, a UTF-8 JSON header,
then ``header["size"]`` bytes of binary payload (possibly zero).

Masters announce themselves with UDP broadcast beacons; nodes that join
connect to the master over TCP.

This module must stay free of ``bpy`` so it can be tested on its own.
"""

import hashlib
import json
import socket
import struct
import zlib

import numpy as np

PROTOCOL_VERSION = 3
CLUSTER_PORT = 9876
DISCOVERY_PORT = 9875
BEACON_INTERVAL = 1.0
BEACON_TIMEOUT = 4.0
APP = "remote_cycles"

_LEN = struct.Struct(">I")


def send_msg(sock, header, payload=b""):
    header = dict(header)
    header["size"] = len(payload)
    data = json.dumps(header).encode()
    sock.sendall(_LEN.pack(len(data)) + data)
    if len(payload):
        sock.sendall(payload)


def recv_exact(sock, n):
    buf = bytearray(n)
    view = memoryview(buf)
    pos = 0
    while pos < n:
        k = sock.recv_into(view[pos:], n - pos)
        if not k:
            raise ConnectionError("connection closed")
        pos += k
    return buf


def recv_msg(sock):
    (n,) = _LEN.unpack(recv_exact(sock, 4))
    header = json.loads(recv_exact(sock, n))
    size = header.get("size", 0)
    payload = recv_exact(sock, size) if size else b""
    return header, payload


def blob_hash(data):
    return hashlib.sha256(data).hexdigest()


def tune_socket(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, 8 << 20)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Discovery


def make_beacon(**fields):
    return json.dumps({"app": APP, "version": PROTOCOL_VERSION, **fields}).encode()


def parse_beacon(data):
    try:
        msg = json.loads(data)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("app") != APP:
        return None
    return msg


# --------------------------------------------------------------------------
# Pass buffers

# Passes that hold one sample's value rather than an average, so they cannot
# be averaged across sample chunks; the chunk starting at sample 0 wins.
_UNFILTERED = ("Depth", "Position", "Vector", "IndexOB", "IndexMA", "Crypto", "Debug")


def is_averaged_channel(name):
    """Channel names look like "ViewLayer.Pass Name.R"."""
    parts = name.split(".")
    pass_name = parts[-2] if len(parts) >= 2 else name
    return not any(key in pass_name for key in _UNFILTERED)


def pack_channels(channels, compress=True):
    """Serialize ``{name: (h, w) array}``; colors as half, the rest float32."""
    meta = []
    chunks = []
    for name in sorted(channels):
        dtype = "<f2" if is_averaged_channel(name) else "<f4"
        arr = np.ascontiguousarray(channels[name], dtype=dtype)
        meta.append([name, dtype])
        chunks.append(arr.tobytes())
    data = b"".join(chunks)
    if compress:
        data = zlib.compress(data, 1)
    return meta, data


def unpack_channels(meta, height, width, payload, compressed=True):
    data = zlib.decompress(payload) if compressed else bytes(payload)
    out = {}
    pos = 0
    for name, dtype in meta:
        n = height * width * np.dtype(dtype).itemsize
        out[name] = np.frombuffer(data, dtype, height * width, pos).reshape(height, width).astype(np.float32)
        pos += n
    return out
