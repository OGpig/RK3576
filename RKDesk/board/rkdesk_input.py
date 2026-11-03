#!/usr/bin/env python3
"""RKDesk input daemon.

Creates the virtual absolute mouse and keyboard before Weston starts, then
accepts line-delimited JSON commands on a Unix socket.  Keeping the devices in
a long-lived process means restarting the web server does not disturb Weston.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rkdesk.input import VirtualInput  # noqa: E402

SOCKET_PATH = "/var/run/rkdesk-input.sock"
WAYLAND_SOCKET = "/var/run/wayland-0"


class InputDaemon:
    def __init__(self, path: str, width: int, height: int, restart_weston: bool) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.virtual = VirtualInput(width, height)
        self.running = True
        if restart_weston:
            self._ensure_weston()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self.server.bind(path)
        os.chmod(path, 0o600)
        self.server.listen(4)
        print(f"[rkdesk-input] listening on {path}", flush=True)

    @staticmethod
    def _ensure_weston() -> None:
        if not os.path.exists(WAYLAND_SOCKET):
            print("[rkdesk-input] weston not running yet; it will start later", flush=True)
            return
        age = time.time() - os.path.getmtime(WAYLAND_SOCKET)
        if age > 3:
            print("[rkdesk-input] restarting weston to pick up virtual input", flush=True)
            subprocess.run(["/etc/init.d/S49weston", "restart"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)

    def _client(self, connection: socket.socket) -> None:
        buffer = b""
        print("[rkdesk-input] client connected", flush=True)
        try:
            connection.sendall(b'{"t":"hello"}\n')
        except OSError:
            pass
        try:
            while self.running:
                data = connection.recv(65536)
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        command = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if command.get("t") != "move":
                        print(f"[rkdesk-input] cmd {command}", flush=True)
                    with self.lock:
                        self.virtual.handle(command)
        except OSError as error:
            print(f"[rkdesk-input] client error: {error}", flush=True)
        finally:
            print("[rkdesk-input] client disconnected", flush=True)
            try:
                connection.close()
            except OSError:
                pass

    def serve(self) -> None:
        while self.running:
            try:
                connection, _ = self.server.accept()
            except OSError:
                break
            threading.Thread(target=self._client, args=(connection,), daemon=True).start()

    def stop(self) -> None:
        self.running = False
        try:
            self.server.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass
        self.virtual.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="RKDesk virtual input daemon")
    parser.add_argument("--socket", default=SOCKET_PATH)
    parser.add_argument("--width", type=int, default=800)
    parser.add_argument("--height", type=int, default=1280)
    parser.add_argument("--no-restart-weston", action="store_true")
    args = parser.parse_args()

    daemon = InputDaemon(args.socket, args.width, args.height, not args.no_restart_weston)

    def shutdown(*_):
        daemon.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    daemon.serve()


if __name__ == "__main__":
    main()
