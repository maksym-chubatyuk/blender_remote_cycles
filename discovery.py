"""LAN discovery: masters broadcast beacons, everyone listens for them."""

import socket
import threading
import time

from . import protocol


class BeaconSender:
    """Broadcasts this master's beacon until stopped; ``info_fn`` returning
    None skips a beat."""

    def __init__(self, info_fn, port=protocol.DISCOVERY_PORT):
        self._info_fn = info_fn
        self._port = port
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="rc-beacon").start()

    def stop(self):
        self._stop.set()

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            while not self._stop.is_set():
                info = self._info_fn()
                if info is not None:
                    data = protocol.make_beacon(**info)
                    for addr in ("255.255.255.255", "<broadcast>"):
                        try:
                            sock.sendto(data, (addr, self._port))
                            break
                        except OSError:
                            continue
                self._stop.wait(protocol.BEACON_INTERVAL)
        finally:
            sock.close()


class Listener:
    """Collects beacons from masters on the LAN."""

    def __init__(self, port=protocol.DISCOVERY_PORT):
        self._port = port
        self._lock = threading.Lock()
        self._masters = {}
        self._stop = threading.Event()
        self.error = None
        threading.Thread(target=self._run, daemon=True, name="rc-discovery").start()

    def stop(self):
        self._stop.set()

    def masters(self):
        """Masters heard from recently, as a list of beacon dicts plus "address"."""
        now = time.monotonic()
        with self._lock:
            return [dict(info) for info, seen in self._masters.values()
                    if now - seen < protocol.BEACON_TIMEOUT]

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            sock.bind(("", self._port))
        except OSError as e:
            self.error = f"discovery unavailable: {e}"
            return
        sock.settimeout(0.5)
        try:
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                info = protocol.parse_beacon(data)
                if info is None or "id" not in info:
                    continue
                info["address"] = addr[0]
                with self._lock:
                    self._masters[info["id"]] = (info, time.monotonic())
        finally:
            sock.close()
