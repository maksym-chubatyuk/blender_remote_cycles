"""A framed TCP connection with a sender thread and a receiver thread.

Control messages are sent in order. Messages sent with ``send_latest`` (viewport
frames, viewport cameras) replace any unsent message with the same key, so a
slow link only ever carries the newest one.
"""

import collections
import socket
import threading
import traceback

from . import protocol


class Link:
    def __init__(self, sock, handler, name="link"):
        """``handler(header, payload)`` runs on the receiver thread; it gets
        ``{"op": "_closed"}`` once when the connection ends. Call ``start()``
        once the owner is ready to receive."""
        self.sock = sock
        self.name = name
        self.alive = True
        self.bytes_received = 0
        self._handler = handler
        self._cv = threading.Condition()
        self._control = collections.deque()
        self._latest = collections.OrderedDict()

    def start(self):
        threading.Thread(target=self._send_loop, daemon=True, name=f"{self.name}-send").start()
        threading.Thread(target=self._recv_loop, daemon=True, name=f"{self.name}-recv").start()
        return self

    def send(self, header, payload=b"", path=None):
        """Queue a message. With ``path``, the payload is read from that file
        on the sender thread."""
        with self._cv:
            self._control.append((header, payload, path))
            self._cv.notify()

    def send_latest(self, key, header, payload=b""):
        with self._cv:
            self._latest.pop(key, None)
            self._latest[key] = (header, payload, None)
            self._cv.notify()

    def close(self):
        with self._cv:
            if not self.alive:
                return
            self.alive = False
            self._cv.notify()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def _send_loop(self):
        while True:
            with self._cv:
                while self.alive and not self._control and not self._latest:
                    self._cv.wait()
                if not self.alive:
                    return
                if self._control:
                    header, payload, path = self._control.popleft()
                else:
                    _key, (header, payload, path) = self._latest.popitem(last=False)
            try:
                if path is not None:
                    try:
                        with open(path, "rb") as f:
                            payload = f.read()
                    except OSError:
                        continue
                protocol.send_msg(self.sock, header, payload)
            except OSError:
                self.close()
                return

    def _recv_loop(self):
        try:
            while True:
                header, payload = protocol.recv_msg(self.sock)
                self.bytes_received += len(payload)
                try:
                    self._handler(header, payload)
                except Exception:
                    traceback.print_exc()
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            self.close()
            self._handler({"op": "_closed"}, b"")
