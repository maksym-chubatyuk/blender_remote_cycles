"""Master side: hosts the cluster, schedules work on nodes, merges results.

Nodes connect to the master (found through UDP beacons). Final renders are
jobs:

* ``SplitJob``: one image, its samples split across all nodes. Nodes take
  chunks sized by their speed, halving as the work runs out, so a wrong
  speed estimate cannot leave everyone waiting on one node.
* ``FramesJob``: whole frames (animations, or a still on a single node).
  Free nodes take the next frame, fastest first. When no frames are left,
  an idle node re-renders a running frame if it would finish it sooner.

Viewports stream from the node that delivers frames fastest, counting both
render speed and link speed.
"""

import atexit
import collections
import itertools
import math
import os
import socket
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
import zlib

import bpy
import numpy as np

from . import discovery, protocol, sync
from .link import Link

LINK_TEST_BYTES = 4 << 20

_ids = itertools.count(1)


def log(*args):
    print("[remote-cycles master]", *args, flush=True)


class NodeLink:
    """A joined node, as seen by the master."""

    def __init__(self, cluster, sock, addr, hello):
        self.cluster = cluster
        self.id = next(_ids)
        self.address = addr[0]
        self.name = hello.get("name") or addr[0]
        self.device = hello.get("device", "?")
        self.devices = hello.get("devices", [])
        self.selected = hello.get("selected", [])
        self.local = bool(hello.get("local"))
        self.score = hello.get("score")
        self.have = set(hello.get("have", ()))
        self.rtt = None
        self.bandwidth = None
        self.status = "Benchmarking" if self.score is None else "Idle"
        self.inflight = None
        self._ping_sent = {}
        self.link = Link(sock, self._on_message, name=f"node-{self.name}")

    @property
    def alive(self):
        return self.link.alive

    @property
    def enabled(self):
        return self.name not in self.cluster.disabled

    def ensure_blob(self, key, data):
        if key not in self.have:
            self.link.send({"op": "put", "key": key, "verify": True}, data)
            self.have.add(key)

    def ensure_file(self, key, path):
        if key not in self.have:
            self.link.send({"op": "put", "key": key, "verify": False}, path=path)
            self.have.add(key)

    def upload(self, snapshot):
        self.ensure_blob(snapshot.key, snapshot.data)
        for path, key in snapshot.files.items():
            self.ensure_file(key, path)

    def start_link_test(self):
        self._ping(1, 0)

    def _ping(self, seq, reply_size):
        self._ping_sent[seq] = time.monotonic()
        self.link.send({"op": "ping", "seq": seq, "reply_size": reply_size})

    def _on_message(self, header, payload):
        if header.get("op") == "pong":
            elapsed = time.monotonic() - self._ping_sent.pop(header.get("seq"), time.monotonic())
            if header.get("seq") == 1:
                self.rtt = elapsed
                self._ping(2, LINK_TEST_BYTES)
            else:
                self.bandwidth = len(payload) / max(elapsed, 1e-4)
                self.cluster.changed()
            return
        self.cluster.on_message(self, header, payload)

    def describe(self):
        bits = []
        if self.score:
            bits.append(f"{self.score:.0f} Ms/s")
        if self.bandwidth:
            bits.append(f"{self.bandwidth / 1e6:.0f} MB/s")
        return " | ".join(bits)


# --------------------------------------------------------------------------
# Jobs


class Job:
    def __init__(self, cluster, snapshot, scene_name):
        self.cluster = cluster
        self.id = next(_ids)
        self.snapshot = snapshot
        self.scene = scene_name
        self.cancelled = False
        self.error = None

    def work(self, **fields):
        return {"op": "work", "job": self.id, "blend": self.snapshot.key,
                "files": self.snapshot.files, "scene": self.scene, **fields}

    def next_for(self, node):
        return None

    def on_done(self, node, header, payload):
        pass

    def on_error(self, node, header):
        pass

    def on_passes(self, node, header, payload):
        pass

    def on_node_lost(self, node):
        pass

    def cleanup(self):
        pass


class FramesJob(Job):
    """Whole frames rendered on single nodes, results kept until consumed."""

    def __init__(self, cluster, snapshot, scene_name, items):
        super().__init__(cluster, snapshot, scene_name)
        self.items = {}
        self.lookup = {}
        self.pending = collections.deque()
        for frame, layer in items:
            iid = next(_ids)
            self.items[iid] = {"frame": frame, "layer": layer, "state": "pending",
                               "nodes": {}, "result": None, "tries": 0}
            self.lookup[(frame, layer)] = iid
            self.pending.append(iid)
        self.times = {}

    def next_for(self, node):
        while self.pending:
            iid = self.pending.popleft()
            if self.items[iid]["state"] == "pending":
                return self._assign(node, iid)
        return self._help_tail(node)

    def _assign(self, node, iid):
        item = self.items[iid]
        item["state"] = "running"
        item["nodes"][node.id] = time.monotonic()
        return self.work(kind="frame", item=iid, frame=item["frame"], layer=item["layer"])

    def estimate(self, node):
        """Expected seconds per frame on ``node``, from measured times."""
        if node.id in self.times:
            return self.times[node.id]
        for other_id, seconds in self.times.items():
            other = self.cluster.nodes.get(other_id)
            if other and other.score and node.score:
                return seconds * other.score / node.score
        return None

    def _help_tail(self, node):
        """No frames left: duplicate the running frame this node would beat by most."""
        mine = self.estimate(node)
        if mine is None:
            return None
        now = time.monotonic()
        best = None
        for iid, item in self.items.items():
            if item["state"] != "running" or node.id in item["nodes"] or len(item["nodes"]) > 1:
                continue
            remaining = []
            for nid, started in item["nodes"].items():
                other = self.cluster.nodes.get(nid)
                est = self.estimate(other) if other else None
                if est is not None:
                    remaining.append(est - (now - started))
            if not remaining:
                continue
            left = min(remaining)
            if mine < 0.8 * left and (best is None or left > best[0]):
                best = (left, iid)
        return self._assign(node, best[1]) if best else None

    def on_done(self, node, header, payload):
        item = self.items.get(header["item"])
        if item is None or header.get("discarded"):
            return
        item["nodes"].pop(node.id, None)
        seconds = header.get("seconds", 0.0)
        prev = self.times.get(node.id)
        self.times[node.id] = seconds if prev is None else 0.5 * prev + 0.5 * seconds
        if item["state"] != "running":
            return  # a duplicate finished second
        path = os.path.join(self.cluster.work_dir, f"job{self.id}_item{header['item']}.exr")
        with open(path, "wb") as f:
            f.write(payload)
        item["result"] = path
        item["state"] = "done"
        self.cluster.history.append(("frame", self.id, item["frame"], item["layer"], node.name))
        if self.cluster.anim_job is self:
            self.cluster.set_preview(node, item["frame"], payload)

    def on_error(self, node, header):
        item = self.items.get(header.get("item"))
        if item is None:
            return
        item["nodes"].pop(node.id, None)
        item["tries"] += 1
        if item["state"] == "running" and not item["nodes"]:
            if item["tries"] < 3:
                item["state"] = "pending"
                self.pending.appendleft(header["item"])
            else:
                item["state"] = "failed"
                self.error = header.get("text", "render failed")

    def on_node_lost(self, node):
        for iid, item in self.items.items():
            if node.id in item["nodes"]:
                item["nodes"].pop(node.id)
                if item["state"] == "running" and not item["nodes"]:
                    item["state"] = "pending"
                    self.pending.appendleft(iid)

    def item(self, frame, layer):
        return self.items.get(self.lookup.get((frame, layer)))

    def consume(self, frame, layer):
        item = self.item(frame, layer)
        if item and item["result"]:
            try:
                os.remove(item["result"])
            except OSError:
                pass
            item["result"] = None
        if item:
            item["state"] = "consumed"

    @property
    def finished(self):
        return all(i["state"] in ("consumed", "failed") for i in self.items.values())

    def cleanup(self):
        for item in self.items.values():
            if item["result"]:
                try:
                    os.remove(item["result"])
                except OSError:
                    pass


class SplitJob(Job):
    """One frame whose samples are split across nodes and merged here."""

    def __init__(self, cluster, snapshot, scene_name, frame, layer, total, denoise):
        super().__init__(cluster, snapshot, scene_name)
        self.frame = frame
        self.layer = layer
        self.total = total
        self.denoise = denoise
        self.min_chunk = max(1, total // 32)
        self.pending = [(0, total)]
        self.running = {}
        self.uncollected = collections.defaultdict(list)
        self.collecting = set()
        self.sets = []
        self.samples_done = 0
        self.errors = 0
        self.phase = "render"

    def next_for(self, node):
        if self.phase != "render" or not self.pending:
            return None
        remaining = sum(count for _, count in self.pending)
        speeds = [n.score for n in self.cluster.render_nodes()] or [node.score]
        share = node.score / sum(speeds)
        chunk = max(self.min_chunk, math.ceil(remaining * share / 2))
        offset, count = self.pending[0]
        take = min(chunk, count)
        if take == count:
            self.pending.pop(0)
        else:
            self.pending[0] = (offset + take, count - take)
        iid = next(_ids)
        self.running[iid] = (node.id, offset, take)
        return self.work(kind="chunk", item=iid, frame=self.frame, layer=self.layer,
                         offset=offset, count=take, total=self.total, denoise=self.denoise)

    def on_done(self, node, header, payload):
        entry = self.running.pop(header["item"], None)
        if entry is None or header.get("discarded"):
            return
        _nid, offset, count = entry
        self.uncollected[node.id].append((offset, count))
        self.samples_done += count
        self.cluster.history.append(("chunk", self.id, offset, count, node.name))
        self._maybe_collect()

    def _maybe_collect(self):
        if self.pending or self.running or self.collecting:
            return
        for nid in list(self.uncollected):
            node = self.cluster.nodes.get(nid)
            if node is None or not node.alive:
                continue
            self.collecting.add(nid)
            node.link.send({"op": "collect", "job": self.id})
        if not self.collecting:
            self.phase = "merge"
        else:
            self.phase = "collect"

    def on_passes(self, node, header, payload):
        if node.id not in self.collecting:
            return
        self.collecting.discard(node.id)
        self.uncollected.pop(node.id, None)
        if header.get("samples"):
            channels = protocol.unpack_channels(header["meta"], header["height"], header["width"],
                                                payload, header.get("compressed", True))
            self.sets.append((header["samples"], header.get("has_first", False), channels))
        if not self.collecting:
            self.phase = "merge" if not self.pending else "render"

    def _return_ranges(self, ranges):
        for offset, count in ranges:
            self.pending.append((offset, count))
            self.samples_done -= count
        self.pending.sort()

    def on_error(self, node, header):
        entry = self.running.pop(header.get("item"), None)
        if entry:
            self.pending.append(entry[1:])
            self.pending.sort()
        self.errors += 1
        if self.errors > 5:
            self.error = header.get("text", "render failed")

    def on_node_lost(self, node):
        lost = [(o, c) for nid, o, c in self.running.values() if nid == node.id]
        self.running = {k: v for k, v in self.running.items() if v[0] != node.id}
        for offset, count in lost:
            self.pending.append((offset, count))
        self.pending.sort()
        # Samples it accumulated but never sent back are gone too.
        self._return_ranges(self.uncollected.pop(node.id, []))
        self.collecting.discard(node.id)
        if self.pending:
            self.phase = "render"
        else:
            self._maybe_collect()

    def merge(self):
        total = sum(s for s, _, _ in self.sets)
        out = {}
        for samples, has_first, channels in self.sets:
            for name, arr in channels.items():
                if protocol.is_averaged_channel(name):
                    if name in out:
                        out[name] += arr * samples
                    else:
                        out[name] = arr * samples
                elif has_first:
                    out[name] = arr
        for name in out:
            if protocol.is_averaged_channel(name):
                out[name] /= total
        return out, total


# --------------------------------------------------------------------------
# Cluster


class Cluster:
    def __init__(self, port, name, disabled=(), device_choices=None):
        self.id = uuid.uuid4().hex
        self.name = name
        self.port = port
        self.project = ""
        # Only announced while this Blender actually uses Remote Cycles.
        self.announce = False
        self.device_choices = dict(device_choices or {})
        self.blender = list(bpy.app.version)
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.nodes = {}
        self.disabled = set(disabled)
        self.jobs = []
        self.sessions = {}
        self.anim_job = None
        self.history = collections.deque(maxlen=1000)
        # Latest finished animation frame per machine, for its preview window.
        self.previews = {}
        self.version = 0
        self.work_dir = tempfile.mkdtemp(prefix="remote_cycles_master_")
        self.server = socket.create_server(("", port))
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True, name="rc-accept").start()
        self.beacon = discovery.BeaconSender(self._beacon_info)
        log(f"hosting as {name!r} on port {port}")

    def stop(self):
        self.running = False
        self.beacon.stop()
        try:
            self.server.close()
        except OSError:
            pass
        with self.lock:
            for job in list(self.jobs):
                self.cancel(job)
            for node in list(self.nodes.values()):
                node.link.close()

    def changed(self):
        self.version += 1

    def _beacon_info(self):
        if not self.announce:
            return None
        return {"id": self.id, "name": self.name, "port": self.port,
                "project": self.project, "blender": self.blender}

    # Nodes ------------------------------------------------------------------

    def _accept_loop(self):
        while self.running:
            try:
                sock, addr = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handshake, args=(sock, addr), daemon=True).start()

    def _handshake(self, sock, addr):
        try:
            sock.settimeout(5)
            protocol.tune_socket(sock)
            hello, _ = protocol.recv_msg(sock)
            if hello.get("op") != "hello" or hello.get("version") != protocol.PROTOCOL_VERSION:
                protocol.send_msg(sock, {"op": "reject", "text":
                                         "add-on version mismatch, update Remote Cycles"})
                sock.close()
                return
            if list(hello.get("blender", []))[:2] != self.blender[:2]:
                protocol.send_msg(sock, {"op": "reject", "text":
                                         f"Blender version mismatch: master runs "
                                         f"{'.'.join(map(str, self.blender))}"})
                sock.close()
                return
            protocol.send_msg(sock, {"op": "welcome", "name": self.name})
            sock.settimeout(None)
        except (OSError, ValueError, ConnectionError):
            sock.close()
            return
        node = NodeLink(self, sock, addr, hello)
        with self.lock:
            self.nodes[node.id] = node
            self.changed()
            log(f"node {node.name} joined ({node.device})")
            node.link.start()
            choice = self.device_choices.get(node.name)
            if choice and sorted(choice) != sorted(node.selected):
                self.set_devices(node, choice)
            elif node.score:
                node.start_link_test()
            self.dispatch()
            self.cond.notify_all()

    def render_nodes(self):
        return [n for n in self.nodes.values() if n.alive and n.enabled and n.score]

    def set_enabled(self, name, enabled):
        with self.lock:
            if enabled:
                self.disabled.discard(name)
            else:
                self.disabled.add(name)
            self.changed()
            self.dispatch()

    def set_preview(self, node, frame, data):
        path = os.path.join(self.work_dir, f"preview_{node.id}.exr")
        with open(path + ".part", "wb") as f:
            f.write(data)
        os.replace(path + ".part", path)
        version = self.previews.get(node.name, {}).get("version", 0) + 1
        self.previews[node.name] = {"path": path, "frame": frame, "version": version}

    def set_devices(self, node, selected):
        """Choose which of a node's devices render; it re-benchmarks if needed."""
        with self.lock:
            node.selected = list(selected)
            node.score = None
            node.status = "Benchmarking new devices"
            self.device_choices[node.name] = list(selected)
            node.link.send({"op": "devices", "selected": list(selected)})
            self.changed()

    def pick_viewport_node(self, width, height):
        """The node expected to deliver a 1-sample viewport frame first."""
        nodes = self.render_nodes()
        if not nodes:
            return None
        pixels = max(1, width * height)

        def cost(n):
            render = pixels / (n.score * 1e6)
            transfer = pixels * 6 / (n.bandwidth or 50e6)
            return render + transfer
        return min(nodes, key=cost)

    def on_message(self, node, header, payload):
        op = header.get("op")
        if op == "frame":
            session = self.sessions.get(header.get("sid"))
            if session is not None:
                session.on_frame(header, payload)
            return
        with self.lock:
            if op == "_closed":
                self._node_lost(node)
            elif op == "score":
                node.score = header["score"]
                node.status = "Idle" if not header.get("text") else f"Benchmark failed: {header['text']}"
                # Measured now rather than at join, while the node is idle.
                if node.bandwidth is None:
                    node.start_link_test()
                log(f"node {node.name} scored {node.score:.1f} Msamples/s")
                self.dispatch()
            elif op == "status":
                node.status = header.get("text", "")
            elif op == "devices":
                node.device = header.get("device", node.device)
                node.selected = header.get("selected", node.selected)
            elif op == "rename":
                node.name = header.get("name", node.name)
            elif op in ("done", "error", "passes"):
                job = self._job(header.get("job"))
                if op == "error" and header.get("sid") is not None:
                    session = self.sessions.get(header["sid"])
                    if session is not None:
                        session.error = header.get("text")
                if op in ("done", "error") and header.get("job") is not None:
                    node.inflight = None
                if job is not None:
                    if op == "done":
                        job.on_done(node, header, payload)
                    elif op == "error":
                        log(f"node {node.name}: {header.get('text')}")
                        job.on_error(node, header)
                    else:
                        job.on_passes(node, header, payload)
                self.dispatch()
            self.changed()
            self.cond.notify_all()

    def _node_lost(self, node):
        if self.nodes.pop(node.id, None) is None:
            return
        log(f"node {node.name} left")
        for job in self.jobs:
            job.on_node_lost(node)
        for session in list(self.sessions.values()):
            if session.node is node:
                session.on_node_lost()
        self.dispatch()

    # Jobs -------------------------------------------------------------------

    def _job(self, job_id):
        for job in self.jobs:
            if job.id == job_id:
                return job
        return None

    def submit(self, job):
        with self.lock:
            self.jobs.append(job)
            self.dispatch()
            self.changed()

    def cancel(self, job):
        with self.lock:
            job.cancelled = True
            if job in self.jobs:
                self.jobs.remove(job)
            if self.anim_job is job:
                self.anim_job = None
            for node in self.nodes.values():
                node.link.send({"op": "cancel", "job": job.id})
            job.cleanup()
            self.dispatch()
            self.changed()

    def finish(self, job):
        with self.lock:
            if job in self.jobs:
                self.jobs.remove(job)
            if self.anim_job is job:
                self.anim_job = None
            job.cleanup()
            self.dispatch()
            self.changed()

    def dispatch(self):
        """Give free nodes work, fastest first. Call with the lock held."""
        nodes = sorted(self.render_nodes(), key=lambda n: -n.score)
        for job in list(self.jobs):
            if job.cancelled or job.error:
                continue
            for node in nodes:
                if node.inflight is not None:
                    continue
                header = job.next_for(node)
                if header is None:
                    continue
                node.upload(job.snapshot)
                node.inflight = (job.id, header["item"])
                node.link.send(header)

    def wait(self, timeout):
        with self.cond:
            self.cond.wait(timeout)


# --------------------------------------------------------------------------
# Viewport sessions


class Frame:
    __slots__ = ("version", "pixels", "samples", "target", "render_time")

    def __init__(self, version, pixels, samples, target, render_time):
        self.version = version
        self.pixels = pixels
        self.samples = samples
        self.target = target
        self.render_time = render_time


class ViewportSession:
    """One rendered viewport on the master, served by one node."""

    def __init__(self, cluster, compress=False):
        self.cluster = cluster
        self.sid = next(_ids)
        self.compress = compress
        self.node = None
        self.status = "Waiting for a render node to join"
        self.error = None
        self.ui_dirty = False

        self.frame = None
        self.frame_version = 0
        self.drawn_version = 0

        self.scene_name = None
        self.layer_name = None
        self.synced = False
        self.sync_due = None
        self.visible = None
        self.frame_number = None
        self.scene_fp = None
        self.object_fp = {}
        self.ignore_geometry = set()
        self.size = (1920, 1080)
        self._last_view = None
        cluster.sessions[self.sid] = self

    @property
    def alive(self):
        return self.node is not None and self.node.alive

    def close(self):
        self.cluster.sessions.pop(self.sid, None)
        if self.node is not None:
            self.node.link.send({"op": "view_stop", "sid": self.sid})
            self.node = None

    def attach(self):
        """Pick a node if we have none; returns True when newly attached."""
        if self.node is not None:
            return False
        with self.cluster.lock:
            node = self.cluster.pick_viewport_node(*self.size)
        if node is None:
            return False
        self.node = node
        self.synced = False
        self._last_view = None
        self.error = None
        self.status = "Waiting for first frame"
        return True

    def on_node_lost(self):
        self.node = None
        self.synced = False
        self._last_view = None
        self.status = "Render node left, switching"
        self.ui_dirty = True

    def _send(self, header, payload=b"", path=None):
        if self.node is not None:
            self.node.link.send({**header, "sid": self.sid}, payload, path)

    def set_view(self, view):
        self.size = (view["width"], view["height"])
        if view == self._last_view or self.node is None:
            return
        self._last_view = view
        self.node.link.send_latest(("view", self.sid), {**view, "sid": self.sid,
                                                        "compress": self.compress})

    def send_xforms(self, xforms):
        self._send({"op": "xforms", "objects": xforms})

    def send_visible(self):
        self._send({"op": "visible", "objects": self.visible})

    def send_frame_number(self):
        self._send({"op": "frame", "frame": self.frame_number})

    def schedule_sync(self, delay=0.15):
        self.sync_due = time.monotonic() + delay

    def full_sync(self):
        """Snapshot the scene and send it. Main thread only."""
        if self.node is None:
            return
        scene = bpy.data.scenes.get(self.scene_name)
        if scene is None:
            return
        layer = scene.view_layers.get(self.layer_name) or scene.view_layers[0]
        edited = [ob for ob in layer.objects if ob.mode == "EDIT"]
        for ob in edited:
            ob.update_from_editmode()
        # That write-back triggers one more geometry update; skip it.
        self.ignore_geometry = {ob.name for ob in edited}

        snap = sync.Snapshot()
        self.node.upload(snap)
        self.frame_number = scene.frame_current_final
        self._send({"op": "scene", "blend": snap.key, "files": snap.files, "scene": scene.name,
                    "layer": layer.name, "frame": self.frame_number, "visible": self.visible})
        self.scene_fp = sync.scene_fingerprint(scene)
        self.object_fp = {ob.name: sync.object_fingerprint(ob) for ob in layer.objects}
        self.synced = True

    def on_frame(self, header, payload):
        """Receiver thread."""
        w, h, c = header["width"], header["height"], header["channels"]
        if header.get("compressed"):
            payload = zlib.decompress(payload)
        half = np.frombuffer(payload, np.float16).reshape(h, w, c)
        pixels = np.ones((h, w, 4), np.float32)
        pixels[..., :c] = half
        self.frame_version += 1
        self.frame = Frame(self.frame_version, pixels, header["samples"], header["target"],
                           header.get("render_time", 0.0))
        self.error = None

    def status_line(self):
        if self.error:
            return f"Remote Cycles: {self.error}"
        if self.node is None:
            return f"Remote Cycles: {self.status}"
        f = self.frame
        where = f"{self.node.name} ({self.node.device})"
        if f is None:
            return f"Remote Cycles @ {where}: {self.status}"
        return (f"Remote Cycles @ {where} | Sample {f.samples}/{f.target}"
                f" | {f.render_time * 1000:.0f} ms/step")


# --------------------------------------------------------------------------
# Module state

_cluster = None
_error = None


def get():
    return _cluster


def error():
    return _error


def start(port, name, disabled=(), device_choices=None):
    global _cluster, _error
    if _cluster is not None:
        return _cluster
    try:
        _cluster = Cluster(port, name, disabled, device_choices)
        _error = None
    except OSError as e:
        _error = f"Cannot host on port {port}: {e.strerror or e}"
        log(_error)
    return _cluster


def stop():
    global _cluster
    stop_local()
    if _cluster is not None:
        _cluster.stop()
        _cluster = None


# The master's own devices render through a background Blender on the same
# machine: rendering inside this Blender would freeze its window, and a node
# must load the scene in place of the open file.
_local = {"proc": None}


def local_running():
    proc = _local["proc"]
    return proc is not None and proc.poll() is None


def start_local(port, name):
    if local_running():
        return
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_node.py")
    log_path = os.path.join(tempfile.gettempdir(), "remote_cycles_local_node.log")
    with open(log_path, "w") as log_file:
        _local["proc"] = subprocess.Popen(
            [bpy.app.binary_path, "-b", "--factory-startup", "--python", script, "--",
             "--master", f"127.0.0.1:{port}", "--name", f"{name} (this machine)",
             "--local", "--once"],
            stdout=log_file, stderr=subprocess.STDOUT)
    log(f"started this machine's render process (log: {log_path})")


def stop_local():
    proc = _local["proc"]
    _local["proc"] = None
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(3)
    except subprocess.TimeoutExpired:
        proc.kill()


atexit.register(stop_local)


def tick():
    """Main-thread upkeep: attach viewports to nodes, run due syncs, redraw."""
    cl = _cluster
    if cl is None:
        return False
    cl.project = bpy.path.basename(bpy.data.filepath) or "untitled"
    now = time.monotonic()
    redraw = False
    for s in list(cl.sessions.values()):
        if s.attach():
            s.sync_due = now
            redraw = True
        if s.sync_due is not None and now >= s.sync_due and s.scene_name:
            s.sync_due = None
            try:
                s.full_sync()
            except Exception as e:
                traceback.print_exc()
                s.error = f"Sync failed: {e}"
            redraw = True
        if s.frame_version != s.drawn_version or s.ui_dirty:
            s.ui_dirty = False
            redraw = True
    return redraw
