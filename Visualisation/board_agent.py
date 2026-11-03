#!/usr/bin/env python3
"""Minimal RK3576 board-side remote display/input agent.

This agent is intentionally dependency-light: it uses the existing Buildroot
Python + Pillow stack, reads the Weston DRM framebuffer, and injects input
through /dev/uinput. It is a prototype transport, not an authenticated
production remote-desktop service.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import struct
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - board runtime check
    raise SystemExit("Pillow is required: python3 -m pip install Pillow") from exc


DEFAULT_WIDTH = 800
DEFAULT_HEIGHT = 1280
DEFAULT_BPP = 4
MAGIC = b"RKUI1"

def parse_size() -> tuple[int, int]:
    raw = Path("/sys/class/graphics/fb0/virtual_size")
    try:
        width, height = [int(part) for part in raw.read_text().strip().split(",", 1)]
        return width, height
    except (OSError, ValueError):
        return DEFAULT_WIDTH, DEFAULT_HEIGHT


def framebuffer_frame(width: int, height: int, quality: int) -> bytes:
    expected = width * height * DEFAULT_BPP
    with open("/dev/fb0", "rb", buffering=0) as fb:
        raw = fb.read(expected)
    if len(raw) < expected:
        raw += bytes(expected - len(raw))

    # Rockchip DRM fb0 is commonly XRGB8888. Pillow's raw decoder maps the
    # four little-endian bytes into RGB while dropping the unused X byte.
    image = Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", 0, 1)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=max(25, min(90, quality)), optimize=False)
    return output.getvalue()


def send_packet(writer: socket.socket, kind: bytes, payload: bytes) -> None:
    writer.sendall(MAGIC + kind + struct.pack("!I", len(payload)) + payload)


def recv_exact(reader: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        data = reader.recv(remaining)
        if not data:
            raise ConnectionError("peer closed")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def send_json(writer: socket.socket, value: dict[str, Any]) -> None:
    send_packet(writer, b"J", json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


class UInput:
    # linux/input-event-codes.h
    EV_SYN = 0x00
    EV_KEY = 0x01
    EV_REL = 0x02
    EV_ABS = 0x03
    SYN_REPORT = 0
    REL_X = 0x00
    REL_Y = 0x01
    ABS_X = 0x00
    ABS_Y = 0x01
    BTN_LEFT = 0x110
    BTN_RIGHT = 0x111
    BTN_MIDDLE = 0x112
    KEY_ESC = 1
    KEY_ENTER = 28
    KEY_BACKSPACE = 14
    KEY_TAB = 15
    KEY_SPACE = 57
    KEY_LEFT = 105
    KEY_RIGHT = 106
    KEY_UP = 103
    KEY_DOWN = 108
    KEY_SHIFT = 42
    KEY_CTRL = 29
    KEY_ALT = 56
    KEY_META = 125

    def __init__(self, path: str = "/dev/uinput") -> None:
        self.path = path
        self.fd: int | None = None
        self.enabled = False
        try:
            import fcntl
            self._fcntl = fcntl
            self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            self._configure()
            self.enabled = True
        except (OSError, ImportError) as exc:
            print(f"uinput disabled: {exc}")
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    def _ioctl(self, request: int, arg: Any = 0) -> None:
        assert self.fd is not None
        self._fcntl.ioctl(self.fd, request, arg)

    def _configure(self) -> None:
        # uinput ioctl constants for Linux. These are stable ABI values.
        UI_SET_EVBIT = 0x40045564
        UI_SET_KEYBIT = 0x40045565
        UI_SET_RELBIT = 0x40045566
        UI_DEV_CREATE = 0x5501
        self._ioctl(UI_SET_EVBIT, self.EV_KEY)
        self._ioctl(UI_SET_EVBIT, self.EV_REL)
        self._ioctl(UI_SET_KEYBIT, self.BTN_LEFT)
        self._ioctl(UI_SET_KEYBIT, self.BTN_RIGHT)
        self._ioctl(UI_SET_KEYBIT, self.BTN_MIDDLE)
        for code in range(1, 240):
            try:
                self._ioctl(UI_SET_KEYBIT, code)
            except OSError:
                pass
        self._ioctl(UI_SET_RELBIT, self.REL_X)
        self._ioctl(UI_SET_RELBIT, self.REL_Y)

        name = b"RK3576 Remote Input" + bytes(80 - len("RK3576 Remote Input"))
        # struct uinput_user_dev: name[80], id[4H], ff_effects_max, abs arrays.
        packet = struct.pack("80sHHHHI", name, 0x03, 0x3576, 0x0001, 0x0001, 0)
        packet += bytes(64 * 4 * 4)
        os.write(self.fd, packet)
        self._ioctl(UI_DEV_CREATE)
        time.sleep(0.15)

    def _event(self, event_type: int, code: int, value: int) -> None:
        if not self.enabled or self.fd is None:
            return
        now = time.time()
        sec = int(now)
        usec = int((now - sec) * 1_000_000)
        os.write(self.fd, struct.pack("llHHi", sec, usec, event_type, code, value))

    def sync(self) -> None:
        self._event(self.EV_SYN, self.SYN_REPORT, 0)

    def move(self, dx: int, dy: int) -> None:
        self._event(self.EV_REL, self.REL_X, max(-127, min(127, dx)))
        self._event(self.EV_REL, self.REL_Y, max(-127, min(127, dy)))
        self.sync()

    def button(self, code: int, down: bool) -> None:
        self._event(self.EV_KEY, code, 1 if down else 0)
        self.sync()

    def key(self, code: int, down: bool) -> None:
        self._event(self.EV_KEY, code, 1 if down else 0)
        self.sync()

    def close(self) -> None:
        if self.fd is not None:
            try:
                self._ioctl(0x5502)  # UI_DEV_DESTROY
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


KEY_MAP = {
    "Escape": UInput.KEY_ESC,
    "Enter": UInput.KEY_ENTER,
    "Backspace": UInput.KEY_BACKSPACE,
    "Tab": UInput.KEY_TAB,
    " ": UInput.KEY_SPACE,
    "ArrowLeft": UInput.KEY_LEFT,
    "ArrowRight": UInput.KEY_RIGHT,
    "ArrowUp": UInput.KEY_UP,
    "ArrowDown": UInput.KEY_DOWN,
    "Shift": UInput.KEY_SHIFT,
    "Control": UInput.KEY_CTRL,
    "Alt": UInput.KEY_ALT,
    "Meta": UInput.KEY_META,
}
# Linux input-event-codes.h uses a non-alphabetical QWERTY key layout.
KEY_MAP.update({
    "A": 30, "B": 48, "C": 46, "D": 32, "E": 18, "F": 33, "G": 34,
    "H": 35, "I": 23, "J": 36, "K": 37, "L": 38, "M": 50, "N": 49,
    "O": 24, "P": 25, "Q": 16, "R": 19, "S": 31, "T": 20, "U": 22,
    "V": 47, "W": 17, "X": 45, "Y": 21, "Z": 44,
})
for index, digit in enumerate("1234567890", start=2):
    KEY_MAP[digit] = index


class AgentHandler(BaseHTTPRequestHandler):
    agent: "BoardAgent"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json({"ok": True, "width": self.agent.width, "height": self.agent.height})
            return
        if self.path == "/frame.jpg":
            frame = self.agent.get_frame()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/input":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            event = json.loads(self.rfile.read(length))
            self.agent.handle_input(event)
            self._json({"ok": True})
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            self._json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _json(self, value: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


class BoardAgent:
    def __init__(self, host: str, port: int, width: int, height: int, quality: int) -> None:
        self.host = host
        self.port = port
        self.width = width
        self.height = height
        self.quality = quality
        self.uinput = UInput()
        self.frame_lock = threading.Lock()
        self.frame_cache: tuple[float, bytes] | None = None

    def get_frame(self) -> bytes:
        now = time.monotonic()
        with self.frame_lock:
            if self.frame_cache and now - self.frame_cache[0] < 0.08:
                return self.frame_cache[1]
            frame = framebuffer_frame(self.width, self.height, self.quality)
            self.frame_cache = (now, frame)
            return frame

    def handle_input(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "move":
            self.uinput.move(int(event.get("dx", 0)), int(event.get("dy", 0)))
        elif kind == "button":
            button = {"left": UInput.BTN_LEFT, "right": UInput.BTN_RIGHT, "middle": UInput.BTN_MIDDLE}.get(event.get("button"), UInput.BTN_LEFT)
            self.uinput.button(button, bool(event.get("down")))
        elif kind == "key":
            key = str(event.get("key", ""))
            code = KEY_MAP.get(key, KEY_MAP.get(key.upper()))
            if code is not None:
                self.uinput.key(code, bool(event.get("down")))
        elif kind == "text":
            for character in str(event.get("text", "")):
                code = KEY_MAP.get(character, KEY_MAP.get(character.upper()))
                if code is not None:
                    self.uinput.key(code, True)
                    self.uinput.key(code, False)

    def serve(self) -> None:
        server = ThreadingHTTPServer((self.host, self.port), AgentHandler)
        AgentHandler.agent = self
        print(f"RK3576 board agent listening on http://{self.host}:{self.port}")
        try:
            server.serve_forever()
        finally:
            server.server_close()
            self.uinput.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--quality", type=int, default=65)
    args = parser.parse_args()
    width, height = parse_size()
    # The fb0 driver exposes 32 bpp, and this process is expected to run as root.
    BoardAgent(args.host, args.port, width, height, args.quality).serve()


if __name__ == "__main__":
    main()
