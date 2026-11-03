"""RKDesk board-side server: virtual desktop + WebSocket transport + PTY."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import io
import json
import os
import pty
import queue
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

from .desktop import Desktop, HEIGHT, WIDTH
from .mirror import MirrorSource

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")
VERSION = "1.0.0"


def local_ip() -> str:
    """Best-effort primary IPv4 without sending any packet."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except OSError:
        return ""


# --------------------------------------------------------------------- frames
def ws_packet(opcode: int, payload: bytes) -> bytes:
    head = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        head += bytes([length])
    elif length < 65536:
        head += bytes([126]) + struct.pack("!H", length)
    else:
        head += bytes([127]) + struct.pack("!Q", length)
    return head + payload


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("peer closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class WSClient:
    def __init__(self, sock: socket.socket, address) -> None:
        self.sock = sock
        self.address = address
        self.queue: queue.Queue = queue.Queue(maxsize=3)
        self.alive = True
        self.dropped = 0
        self.thread = threading.Thread(target=self._sender, name=f"ws-{address[1]}", daemon=True)
        self.thread.start()

    def _sender(self) -> None:
        while self.alive:
            try:
                opcode, payload = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.sock.sendall(ws_packet(opcode, payload))
            except OSError:
                self.alive = False
                break

    def _put(self, opcode: int, payload: bytes) -> None:
        if not self.alive:
            return
        try:
            self.queue.put_nowait((opcode, payload))
        except queue.Full:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait((opcode, payload))
                self.dropped += 1
            except (queue.Empty, queue.Full):
                pass

    def send_frame(self, jpeg: bytes) -> None:
        self._put(0x2, jpeg)

    def send_text(self, text: str) -> None:
        self._put(0x1, text.encode())

    def close(self) -> None:
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


# ----------------------------------------------------------------------- pty
class Shell:
    def __init__(self, desktop: Desktop) -> None:
        self.desktop = desktop
        self.master: int | None = None
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def start(self) -> None:
        master, slave = pty.openpty()
        env = os.environ.copy()
        env.update({
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": "/root",
            "PS1": "\\033[36mroot@rk3576\\033[0m:\\033[34m\\w\\033[0m# ",
        })
        self.proc = subprocess.Popen(
            ["/bin/sh", "-i"], stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=os.setsid, env=env, cwd="/root", close_fds=True,
        )
        os.close(slave)
        self.master = master
        with self.desktop.lock:
            self.desktop.shell_alive = True
            self.desktop.term.clear()
        self.resize(*self.desktop.term_grid())
        self.write(b"export PS1=$(printf '\\033[36mroot@rk3576\\033[0m:\\033[34m\\w\\033[0m# ')\n")
        self.write(b"cd /root\n")
        self.write(b"clear\n")
        threading.Thread(target=self._reader, args=(master,), daemon=True, name="pty-reader").start()
        print(f"[rkdesk] shell started pid={self.proc.pid}")

    def _reader(self, master: int) -> None:
        try:
            while True:
                data = os.read(master, 65536)
                if not data:
                    break
                with self.desktop.lock:
                    self.desktop.term.feed(data)
                    if self.desktop.term.title:
                        self.desktop.set_title(self.desktop.term.title)
                    self.desktop.dirty = True
        except OSError:
            pass
        finally:
            with self.desktop.lock:
                self.desktop.shell_alive = False
                self.desktop.dirty = True
            print("[rkdesk] shell exited")
            try:
                os.close(master)
            except OSError:
                pass
            self.master = None

    def write(self, data: bytes) -> None:
        if not self.master:
            return
        try:
            os.write(self.master, data)
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        if not self.master:
            return
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except OSError:
                pass


# -------------------------------------------------------------------- server
class App:
    def __init__(self, args) -> None:
        self.args = args
        self.width = args.width
        self.height = args.height
        self.scale = max(0.4, min(1.0, args.scale))
        self.quality = args.quality
        self.target_fps = args.fps
        self.token = args.token
        self.mode = args.mode
        self.desktop = Desktop(args.width, args.height)
        self.desktop.show_cursor = args.show_cursor
        self.shell = Shell(self.desktop)
        self.mirror = None
        self.last_image = None
        self.force_frame = True
        if self.mode == "mirror":
            self.mirror = MirrorSource(input_socket=args.input_socket,
                                       device=args.device,
                                       start_weston=not args.no_restart_weston)
            self.width, self.height = self.mirror.width, self.mirror.height
            self.desktop.width, self.desktop.height = self.width, self.height
        else:
            self.desktop.on_terminal_input = self.shell.write
            self.desktop.on_terminal_resize = self.shell.resize
            self.desktop.on_shell_restart = self.shell.start
        self.clients: list[WSClient] = []
        self.clients_lock = threading.Lock()
        self.frame_lock = threading.Lock()
        self.latest_jpeg: bytes | None = None
        self.frame_version = 0
        self.running = True
        self.encode_ms = 0.0
        self.fps = 0.0
        self.started = time.time()

    # ---- stats
    def _sample_stats(self) -> None:
        last = None
        while self.running:
            try:
                with open("/proc/stat", "r") as handle:
                    fields = handle.readline().split()[1:]
                values = [int(x) for x in fields]
                idle = values[3] + (values[4] if len(values) > 4 else 0)
                total = sum(values)
                if last:
                    d_idle, d_total = idle - last[0], total - last[1]
                    cpu = 0.0 if d_total <= 0 else (1 - d_idle / d_total) * 100
                else:
                    cpu = 0.0
                last = (idle, total)

                mem_total = mem_avail = 0
                with open("/proc/meminfo", "r") as handle:
                    for line in handle:
                        if line.startswith("MemTotal:"):
                            mem_total = int(line.split()[1]) // 1024
                        elif line.startswith("MemAvailable:"):
                            mem_avail = int(line.split()[1]) // 1024
                used = max(0, mem_total - mem_avail)
                mem_pct = 0.0 if mem_total <= 0 else used / mem_total * 100

                temp = 0.0
                for zone in ("/sys/class/thermal/thermal_zone0/temp", "/sys/class/thermal/thermal_zone1/temp"):
                    try:
                        temp = max(temp, int(open(zone).read().strip()) / 1000.0)
                    except (OSError, ValueError):
                        pass
                load = os.getloadavg()
                with open("/proc/uptime") as handle:
                    seconds = int(float(handle.readline().split()[0]))
                days, rem = divmod(seconds, 86400)
                hours, rem = divmod(rem, 3600)
                uptime = f"{days}d {hours}h" if days else f"{hours}h {rem // 60}m"
                st = os.statvfs("/")
                total_gb = st.f_blocks * st.f_frsize / 1024 ** 3
                free_gb = st.f_bavail * st.f_frsize / 1024 ** 3
                ip = local_ip()
                with self.desktop.lock:
                    self.desktop.set_stats(
                        cpu=round(cpu, 1), mem=round(mem_pct, 1), mem_used=used, mem_total=mem_total,
                        temp=round(temp, 1), load=load, uptime=uptime,
                        disk_used=round(total_gb - free_gb, 1), disk_total=round(total_gb, 1),
                        ip=ip, host=socket.gethostname(),
                    )
                    self.desktop.clients = len(self.clients)
                    self.desktop.fps = self.fps
                    self.desktop.encode_ms = self.encode_ms
                self._broadcast_text(json.dumps({
                    "t": "stats", "cpu": round(cpu, 1), "mem": round(mem_pct, 1),
                    "temp": round(temp, 1), "clients": len(self.clients),
                    "fps": round(self.fps, 1), "uptime": uptime,
                    "disk_used": round(total_gb - free_gb, 1),
                    "disk_total": round(total_gb, 1),
                    "ip": ip, "host": socket.gethostname(),
                }, separators=(",", ":")))
            except Exception:
                traceback.print_exc()
            time.sleep(1.5)

    # ---- frame loop
    def _frame_loop(self) -> None:
        last_render = 0.0
        fps_mark = time.monotonic()
        fps_frames = 0
        while self.running:
            begin = time.perf_counter()
            image = None
            if self.mode == "mirror":
                try:
                    image = self.mirror.grab()
                    if image is not None:
                        self.last_image = image
                    elif self.force_frame:
                        image = self.last_image
                    self.force_frame = False
                except Exception as error:  # noqa: BLE001 - display may be hotplugged
                    print(f"[rkdesk] capture error: {error}")
                    time.sleep(0.5)
                    continue
            else:
                with self.desktop.lock:
                    due = self.desktop.dirty or (time.monotonic() - last_render) > 1.0
                    if due:
                        image = self.desktop.render()
                        self.desktop.dirty = False
                        last_render = time.monotonic()
            if image is not None:
                encode_start = time.perf_counter()
                if self.scale < 0.999:
                    image = image.resize(
                        (max(2, int(image.width * self.scale)), max(2, int(image.height * self.scale))),
                        Image.BILINEAR,
                    )
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=self.quality, optimize=False, subsampling=1)
                jpeg = buffer.getvalue()
                self.encode_ms = (time.perf_counter() - encode_start) * 1000
                with self.frame_lock:
                    self.latest_jpeg = jpeg
                    self.frame_version += 1
                for client in list(self.clients):
                    client.send_frame(jpeg)
                fps_frames += 1
            now = time.monotonic()
            if now - fps_mark >= 1.0:
                self.fps = fps_frames / (now - fps_mark)
                fps_frames = 0
                fps_mark = now
            elapsed = time.perf_counter() - begin
            time.sleep(max(0.001, (1.0 / self.target_fps) - elapsed))

    # ---- clients
    def add_client(self, client: WSClient) -> None:
        with self.clients_lock:
            self.clients.append(client)
        with self.desktop.lock:
            self.desktop.clients = len(self.clients)
            self.desktop.dirty = True
        self.force_frame = True
        client.send_text(json.dumps({
            "t": "hello", "version": VERSION, "width": int(self.width * self.scale),
            "height": int(self.height * self.scale), "fps": self.target_fps,
            "quality": self.quality, "shell": self.desktop.shell_alive,
            "mode": self.mode, "host": self.desktop.stats.get("host", ""),
            "ip": self.desktop.stats.get("ip", ""),
        }, separators=(",", ":")))
        with self.frame_lock:
            if self.latest_jpeg:
                client.send_frame(self.latest_jpeg)
        print(f"[rkdesk] client connected {client.address[0]}:{client.address[1]}")

    def remove_client(self, client: WSClient) -> None:
        with self.clients_lock:
            if client in self.clients:
                self.clients.remove(client)
        client.close()
        with self.desktop.lock:
            self.desktop.clients = len(self.clients)
            self.desktop.dirty = True
        print(f"[rkdesk] client left {client.address[0]}:{client.address[1]}")

    def _broadcast_text(self, text: str) -> None:
        for client in list(self.clients):
            client.send_text(text)

    # ---- input
    def handle_message(self, client: WSClient, message: dict) -> None:
        kind = message.get("t")
        if kind == "ping":
            client.send_text(json.dumps({"t": "pong", "ts": message.get("ts")}, separators=(",", ":")))
            return
        if kind == "quality":
            self.quality = max(30, min(92, int(message.get("value", self.quality))))
            self.force_frame = True
            return
        if self.mode == "mirror":
            if kind == "move":
                self.mirror.move(int(message.get("x", 0)), int(message.get("y", 0)))
            elif kind in ("down", "up"):
                self.mirror.move(int(message.get("x", 0)), int(message.get("y", 0)))
                self.mirror.button(int(message.get("b", 0)), kind == "down")
            elif kind == "wheel":
                self.mirror.wheel(float(message.get("dy", 0)))
            elif kind == "key":
                self.mirror.key(message)
            elif kind in ("text", "paste"):
                self.mirror.text(str(message.get("text", "")))
            return
        with self.desktop.lock:
            if kind == "move":
                self.desktop.pointer_move(int(message.get("x", 0)), int(message.get("y", 0)))
            elif kind == "down":
                self.desktop.pointer_down(int(message.get("x", 0)), int(message.get("y", 0)),
                                          int(message.get("b", 0)))
            elif kind == "up":
                self.desktop.pointer_up(int(message.get("x", 0)), int(message.get("y", 0)),
                                        int(message.get("b", 0)))
            elif kind == "wheel":
                self.desktop.wheel(int(message.get("x", 0)), int(message.get("y", 0)),
                                   float(message.get("dy", 0)))
            elif kind == "key":
                self.desktop.key(message)
            elif kind == "text":
                if self.desktop.focus and self.desktop.focus.kind == "terminal":
                    self.desktop.paste(str(message.get("text", "")))
            elif kind == "paste":
                self.desktop.paste(str(message.get("text", "")))
            elif kind == "open":
                self.desktop.ensure_window(str(message.get("window", "terminal")))
            elif kind == "restart-shell":
                if not self.desktop.shell_alive:
                    self.shell.start()

    def snapshot(self) -> bytes | None:
        with self.frame_lock:
            return self.latest_jpeg


APP: App | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "RKDesk/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        if self.path.startswith("/ws"):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- http
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/ws":
            self._websocket(parsed)
            return
        if path == "/healthz":
            self._json({
                "ok": True, "version": VERSION, "mode": APP.mode,
                "width": APP.width, "height": APP.height,
                "clients": len(APP.clients), "fps": round(APP.fps, 1),
                "encode_ms": round(APP.encode_ms, 1), "quality": APP.quality,
                "shell": APP.desktop.shell_alive,
                "uptime": int(time.time() - APP.started),
            })
            return
        if path == "/api/stats":
            self._json(APP.desktop.stats)
            return
        if path == "/api/debug":
            with APP.desktop.lock:
                mirror = None
                if APP.mirror:
                    mirror = {
                        "rotation": APP.mirror.rotation,
                        "display": [APP.mirror.width, APP.mirror.height],
                        "input_connected": APP.mirror.input.sock is not None,
                    }
                self._json({
                    "mode": APP.mode,
                    "display": [APP.width, APP.height],
                    "mirror": mirror,
                    "terminal": [APP.desktop.term.cols, APP.desktop.term.rows],
                    "focus": APP.desktop.focus.kind if APP.desktop.focus else None,
                    "drag": APP.desktop.drag[1] if APP.desktop.drag else None,
                    "windows": [
                        {"kind": w.kind, "x": w.x, "y": w.y, "w": w.w, "h": w.h, "visible": w.visible}
                        for w in APP.desktop.windows
                    ],
                })
            return
        if path in ("/api/screenshot.jpg", "/frame.jpg"):
            data = APP.snapshot()
            if not data:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "no frame yet")
                return
            self._bytes(data, "image/jpeg")
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/paste":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(min(length, 65536)).decode("utf-8", "replace")
            APP.desktop.paste(body)
            self._json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        if "/" in name or "\\" in name or name.startswith("."):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        full = os.path.join(WEB_DIR, name)
        if not os.path.isfile(full):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(os.path.splitext(name)[1], "application/octet-stream")
        with open(full, "rb") as handle:
            data = handle.read()
        self._bytes(data, content_type, cache="no-store" if name == "index.html" else "max-age=30")

    def _bytes(self, data: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def _json(self, value: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    # ---- websocket
    def _websocket(self, parsed) -> None:
        if APP.token:
            token = parse_qs(parsed.query).get("token", [""])[0]
            if token != APP.token:
                self.send_error(HTTPStatus.UNAUTHORIZED, "bad token")
                return
        key = self.headers.get("Sec-WebSocket-Key")
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if not key or upgrade != "websocket":
            self.send_error(HTTPStatus.BAD_REQUEST, "websocket required")
            return
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            self.wfile.flush()
        except OSError:
            return
        self.close_connection = True
        sock = self.connection
        sock.settimeout(None)
        client = WSClient(sock, self.client_address)
        APP.add_client(client)
        try:
            while client.alive:
                frame = self._read_frame(sock)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:
                    try:
                        sock.sendall(ws_packet(0x8, b""))
                    except OSError:
                        pass
                    break
                if opcode == 0x9:
                    client._put(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                if opcode in (0x1, 0x2):
                    try:
                        message = json.loads(payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(message, dict):
                        APP.handle_message(client, message)
        except (ConnectionError, OSError):
            pass
        finally:
            APP.remove_client(client)

    @staticmethod
    def _read_frame(sock: socket.socket):
        header = recv_exact(sock, 2)
        first, second = header[0], header[1]
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", recv_exact(sock, 8))[0]
        if length > 2 * 1024 * 1024:
            raise ConnectionError("frame too large")
        mask = recv_exact(sock, 4) if masked else b""
        payload = recv_exact(sock, length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload


def main() -> None:
    global APP
    parser = argparse.ArgumentParser(description="RKDesk remote desktop server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--mode", choices=("mirror", "virtual"), default="mirror",
                        help="mirror: real DRM screen; virtual: board-rendered desktop")
    parser.add_argument("--device", default="/dev/dri/card0")
    parser.add_argument("--input-socket", default="/var/run/rkdesk-input.sock")
    parser.add_argument("--no-restart-weston", action="store_true",
                        help="do not start Weston when the mirror service starts")
    parser.add_argument("--width", type=int, default=WIDTH)
    parser.add_argument("--height", type=int, default=HEIGHT)
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--quality", type=int, default=78)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--token", default="")
    parser.add_argument("--show-cursor", action="store_true")
    args = parser.parse_args()

    APP = App(args)
    if APP.mode == "virtual":
        APP.shell.start()
    threading.Thread(target=APP._frame_loop, daemon=True, name="frame-loop").start()
    threading.Thread(target=APP._sample_stats, daemon=True, name="stats").start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"[rkdesk] v{VERSION} mode={APP.mode} listening on http://{args.host}:{args.port} "
          f"({APP.width}x{APP.height}@{args.fps:g}fps q{args.quality})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        APP.running = False
        if APP.mode == "virtual":
            APP.shell.stop()
        if APP.mirror:
            APP.mirror.close()
        server.server_close()


if __name__ == "__main__":
    main()
