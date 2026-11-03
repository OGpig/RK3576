"""Mirror the board's real DRM scanout and forward input to the input daemon."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time

from PIL import Image

from .drm import DrmCapture


class InputClient:
    """Line-delimited JSON client for rkdesk_input.py."""

    def __init__(self, path: str = "/var/run/rkdesk-input.sock") -> None:
        self.path = path
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()

    def _connect(self) -> bool:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(self.path)
            self.sock = sock
            return True
        except OSError:
            self.sock = None
            return False

    def send(self, command: dict) -> bool:
        payload = (json.dumps(command, separators=(",", ":")) + "\n").encode()
        with self.lock:
            for _ in range(2):
                if self.sock is None and not self._connect():
                    return False
                try:
                    self.sock.sendall(payload)  # type: ignore[union-attr]
                    return True
                except OSError:
                    try:
                        self.sock.close()  # type: ignore[union-attr]
                    except OSError:
                        pass
                    self.sock = None
        return False


class MirrorSource:
    def __init__(self, input_socket: str = "/var/run/rkdesk-input.sock",
                 device: str = "/dev/dri/card0", start_weston: bool = True) -> None:
        self.input = InputClient(input_socket)
        self.device = device
        if start_weston and not os.path.exists("/var/run/wayland-0"):
            print("[rkdesk] starting weston")
            subprocess.run(["/etc/init.d/S49weston", "start"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        self.capture = self._open_capture(device)
        self.width, self.height = self.capture.width, self.capture.height
        self.last_signature: bytes | None = None
        self.last_refresh = 0.0
        self.input.send({"t": "resize", "w": self.width, "h": self.height})
        print(f"[rkdesk] mirror {self.width}x{self.height}")

    @staticmethod
    def _open_capture(device: str) -> DrmCapture:
        last_error = None
        for _ in range(20):
            try:
                return DrmCapture(device)
            except Exception as error:  # noqa: BLE001 - display may not be ready
                last_error = error
                time.sleep(0.5)
        raise RuntimeError(f"cannot open DRM capture: {last_error}")

    def grab(self) -> Image.Image | None:
        now = time.monotonic()
        if now - self.last_refresh > 2.0:
            self.last_refresh = now
            try:
                if self.capture.refresh():
                    self.last_signature = None
                    print("[rkdesk] switched to a new active CRTC")
            except Exception:
                pass
        try:
            image, signature = self.capture.grab()
        except Exception:
            # Display hotplug or CRTC change: reopen and retry once.
            try:
                self.capture.close()
            except Exception:
                pass
            self.capture = self._open_capture(self.device)
            image, signature = self.capture.grab()
        if (image.width, image.height) != (self.width, self.height):
            self.width, self.height = image.width, image.height
            self.last_signature = None
            self.input.send({"t": "resize", "w": self.width, "h": self.height})
            print(f"[rkdesk] display changed to {self.width}x{self.height}")
        if signature == self.last_signature:
            return None
        self.last_signature = signature
        return image

    # ------------------------------------------------------------- input
    def move(self, x: int, y: int) -> None:
        self.input.send({"t": "move", "x": x, "y": y})

    def button(self, button: int, down: bool) -> None:
        self.input.send({"t": "button", "b": button, "down": down})

    def wheel(self, delta: float) -> None:
        self.input.send({"t": "wheel", "dy": delta})

    def key(self, event: dict) -> None:
        self.input.send({"t": "key", **event})

    def text(self, value: str) -> None:
        self.input.send({"t": "text", "text": value})

    def close(self) -> None:
        try:
            self.capture.close()
        except Exception:
            pass
