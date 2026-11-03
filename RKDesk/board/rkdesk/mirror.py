"""Mirror the board's real DRM scanout and forward input to the input daemon."""

from __future__ import annotations

import json
import os
import re
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
        self.geometry: tuple[int, int] | None = None
        self.lock = threading.RLock()

    def set_geometry(self, width: int, height: int) -> None:
        with self.lock:
            self.geometry = (width, height)
            if self.sock is not None:
                try:
                    self.sock.sendall(self._geometry_payload())
                except OSError:
                    self._drop()

    def _geometry_payload(self) -> bytes:
        if not self.geometry:
            return b""
        width, height = self.geometry
        return (json.dumps({"t": "resize", "w": width, "h": height},
                           separators=(",", ":")) + "\n").encode()

    def _connect(self) -> bool:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(self.path)
            sock.settimeout(None)
            self.sock = sock
            payload = self._geometry_payload()
            if payload:
                sock.sendall(payload)
            threading.Thread(target=self._reader, args=(sock,), daemon=True).start()
            print("[rkdesk] input channel connected", flush=True)
            return True
        except OSError:
            self.sock = None
            return False

    def _reader(self, sock: socket.socket) -> None:
        try:
            while True:
                if not sock.recv(4096):
                    break
        except OSError:
            pass
        finally:
            with self.lock:
                if self.sock is sock:
                    self.sock = None
                    print("[rkdesk] input channel closed", flush=True)

    def _drop(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

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
                    self._drop()
            print("[rkdesk] input send failed", flush=True)
        return False


class MirrorSource:
    def __init__(self, input_socket: str = "/var/run/rkdesk-input.sock",
                 device: str = "/dev/dri/card0", start_weston: bool = True) -> None:
        self.input = InputClient(input_socket)
        self.device = device
        if start_weston and not os.path.exists("/var/run/wayland-0"):
            self._ensure_weston()
        self.capture = self._open_capture(device)
        self.width, self.height = self.capture.width, self.capture.height
        self.rotation = 0
        self.last_signature: bytes | None = None
        self.last_refresh = 0.0
        self._update_geometry(force=True)
        self.input.set_geometry(self.width, self.height)
        print(f"[rkdesk] mirror {self.width}x{self.height} rotation={self.rotation}")

    @staticmethod
    def _weston_process_running() -> bool:
        try:
            output = subprocess.check_output(["ps", "-eo", "args"],
                                             stderr=subprocess.DEVNULL, timeout=2).decode("utf-8", "replace")
            return "python3 /usr/bin/weston" in output
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _wait_for_socket(timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists("/var/run/wayland-0"):
                return True
            time.sleep(0.2)
        return False

    def _ensure_weston(self) -> None:
        # At boot S49weston starts in parallel with this service.  Wait for it
        # instead of spawning a second compositor on the same DRM device.
        if self._weston_process_running():
            print("[rkdesk] waiting for the existing weston", flush=True)
            if self._wait_for_socket(8.0):
                return
            print("[rkdesk] weston socket timeout, restarting", flush=True)
            subprocess.run(["/etc/init.d/S49weston", "restart"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            self._wait_for_socket(6.0)
            return
        print("[rkdesk] starting weston", flush=True)
        subprocess.run(["/etc/init.d/S49weston", "start"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        self._wait_for_socket(8.0)

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

    @staticmethod
    def _query_geometry():
        env = os.environ.copy()
        env.update({"XDG_RUNTIME_DIR": "/var/run", "WAYLAND_DISPLAY": "wayland-0"})
        try:
            text = subprocess.check_output(["wayland-info"], env=env,
                                           stderr=subprocess.DEVNULL, timeout=3).decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return None
        transform = re.search(r"output_transform:\s*(normal|90°|180°|270°)", text)
        size = re.search(r"logical_width:\s*(\d+),\s*logical_height:\s*(\d+)", text)
        if not transform or not size:
            return None
        rotation = {"normal": 0, "90°": 90, "180°": 180, "270°": 270}[transform.group(1)]
        return rotation, int(size.group(1)), int(size.group(2))

    def _update_geometry(self, force: bool = False) -> None:
        info = self._query_geometry()
        if not info:
            return
        rotation, width, height = info
        if self.capture.width > self.capture.height:
            # Landscape scanout (HDMI): the display is already upright.
            rotation = 0
            width, height = self.capture.width, self.capture.height
        if not force and rotation == self.rotation and (width, height) == (self.width, self.height):
            return
        self.rotation, self.width, self.height = rotation, width, height
        self.last_signature = None
        self.input.set_geometry(width, height)
        print(f"[rkdesk] output geometry {width}x{height} transform={rotation}")

    def _rotate(self, image: Image.Image) -> Image.Image:
        if self.rotation == 90:
            return image.rotate(-90, expand=True)   # 90° clockwise
        if self.rotation == 180:
            return image.rotate(180, expand=True)
        if self.rotation == 270:
            return image.rotate(90, expand=True)    # 90° counter-clockwise
        return image

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
            self._update_geometry()
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
        raw_w, raw_h = image.size
        if raw_w > raw_h:
            # HDMI scanout is already landscape; ignore a DSI-only transform.
            if self.rotation != 0:
                self.rotation = 0
                self.last_signature = None
                print("[rkdesk] landscape scanout, rotation disabled")
            display = image
        else:
            display = self._rotate(image)
        image = display
        if (image.width, image.height) != (self.width, self.height):
            self.width, self.height = image.width, image.height
            self.last_signature = None
            self.input.set_geometry(self.width, self.height)
            print(f"[rkdesk] display changed to {self.width}x{self.height}")
        if signature == self.last_signature:
            return None
        self.last_signature = signature
        return image

    # ------------------------------------------------------------- input
    def move(self, x: int, y: int) -> None:
        """Map display coordinates to the absolute device's native axes."""
        width, height = self.width, self.height
        if self.rotation == 90:
            dx = y * width / max(1, height)
            dy = (1 - x / max(1, width)) * height
        elif self.rotation == 270:
            dx = (1 - y / max(1, height)) * width
            dy = x / max(1, width) * height
        elif self.rotation == 180:
            dx = width - x
            dy = height - y
        else:
            dx, dy = x, y
        dx = max(0, min(width, int(round(dx))))
        dy = max(0, min(height, int(round(dy))))
        self.input.send({"t": "move", "x": dx, "y": dy})

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
