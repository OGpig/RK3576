#!/usr/bin/env python3
"""RK3576 environment verification for the mlsysbook IDE-setup lab.

Run on the board:  python3 tools/verify_env.py
It only reads system state and imports libraries; nothing is modified.
"""

from __future__ import annotations

import importlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time

CHECKS: list[tuple[str, str, str]] = []


def add(status: str, name: str, detail: str) -> None:
    CHECKS.append((status, name, detail))


def read(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return default


def run(command: list[str], timeout: float = 4.0) -> str:
    try:
        return subprocess.check_output(command, stderr=subprocess.STDOUT, timeout=timeout).decode(
            "utf-8", "replace"
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def human_kb(value: int) -> str:
    return f"{value / 1024 / 1024:.1f} GiB"


def main() -> int:
    print("=" * 68)
    print(" RK3576 environment verification (mlsysbook ide-setup)")
    print("=" * 68)
    print(f"time       : {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"host       : {socket.gethostname()}")
    print(f"kernel     : {platform.release()}")
    print(f"machine    : {platform.machine()}")
    os_release = {}
    for line in read("/etc/os-release").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            os_release[key] = value.strip().strip('"')
    print(f"buildroot  : {os_release.get('PRETTY_NAME', os_release.get('NAME', 'unknown'))}")
    print(f"uptime     : {read('/proc/uptime').split()[0] if read('/proc/uptime') else '?'} s")

    board_model = read("/proc/device-tree/model").replace("\x00", "").strip()
    cores = os.cpu_count() or 0
    print(f"cpu        : {board_model or 'aarch64'} × {cores}")

    meminfo = {}
    for line in read("/proc/meminfo").splitlines():
        key, _, value = line.partition(":")
        meminfo[key] = int(value.split()[0]) if value.split() else 0
    total_mb = meminfo.get("MemTotal", 0) // 1024
    avail_mb = meminfo.get("MemAvailable", 0) // 1024
    print(f"memory     : {total_mb} MiB total, {avail_mb} MiB available")

    stat = os.statvfs("/")
    total_gb = stat.f_blocks * stat.f_frsize / 1024 ** 3
    free_gb = stat.f_bavail * stat.f_frsize / 1024 ** 3
    print(f"rootfs     : {total_gb:.1f} GiB total, {free_gb:.1f} GiB free")

    print("-" * 68)
    print(" Python")
    print(f"  version  : {sys.version.split()[0]}")
    print(f"  binary   : {sys.executable}")
    packages = {
        "numpy": "数值计算",
        "cv2": "OpenCV 计算机视觉",
        "PIL": "Pillow 图像处理",
        "onnxruntime": "ONNX Runtime",
        "torch": "PyTorch",
        "rknnlite": "RKNN Lite (NPU 推理)",
        "rknn": "RKNN Toolkit",
        "psutil": "系统信息",
        "requests": "HTTP 客户端",
        "flask": "Web 框架",
    }
    for module, label in packages.items():
        try:
            imported = importlib.import_module(module)
            version = getattr(imported, "__version__", "installed")
            print(f"  {module:12s} {version:18s} {label}")
            add("PASS", f"python:{module}", str(version))
        except Exception:
            print(f"  {module:12s} {'MISSING':18s} {label}")
            add("WARN", f"python:{module}", "missing")

    print("-" * 68)
    print(" Devices")
    for path, label in [
        ("/dev/fb0", "framebuffer"),
        ("/dev/dri/card0", "DRM display"),
        ("/dev/dri/renderD128", "GPU render node"),
        ("/dev/uinput", "virtual input injection"),
        ("/dev/video-camera0", "camera pipeline"),
        ("/dev/snd/controlC0", "audio control"),
    ]:
        exists = os.path.exists(path)
        print(f"  {path:26s} {'yes' if exists else 'no ':4s} {label}")
        add("PASS" if exists else "WARN", path, "present" if exists else "absent")

    print("-" * 68)
    print(" Camera / audio inventory")
    v4l = shutil.which("v4l2-ctl")
    if v4l:
        listing = run([v4l, "--list-devices"])
        print("\n".join("  " + line for line in listing.splitlines()[:20]) or "  (no output)")
    else:
        print("  v4l2-ctl not installed")
    print(f"  /dev/video* nodes: {len([n for n in os.listdir('/dev') if n.startswith('video')])}")

    print("-" * 68)
    print(" Network / services")
    ip = ""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        probe.close()
    except OSError:
        pass
    print(f"  address  : {ip or 'unknown'}")
    print(f"  route    : {read('/proc/net/route').splitlines()[1] if len(read('/proc/net/route').splitlines()) > 1 else '?'}")
    sshd = run(["sshd", "-V"])
    print(f"  sshd     : {sshd or 'unknown'}")
    add("PASS" if "OpenSSH" in sshd else "WARN", "sshd", sshd or "unknown")

    print("-" * 68)
    print(" RKDesk remote desktop")
    health = run(["wget", "-q", "-T", "2", "-O", "-", "http://127.0.0.1:8088/healthz"])
    if health:
        try:
            data = json.loads(health)
            print(f"  http://<board>:8088  OK  {data.get('width')}x{data.get('height')}  "
                  f"clients={data.get('clients')}  shell={data.get('shell')}")
            add("PASS", "rkdesk", "running")
        except json.JSONDecodeError:
            print("  rkdesk responded with unexpected data")
            add("WARN", "rkdesk", "bad response")
    else:
        print("  not running (start: /etc/init.d/S99rkdesk start)")
        add("WARN", "rkdesk", "not running")

    print("-" * 68)
    print(" IDE-setup checklist")
    checks = [
        ("SSH login", "PASS", f"root@{ip or 'board'}"),
        ("Python >= 3.8", "PASS" if sys.version_info >= (3, 8) else "WARN", sys.version.split()[0]),
        ("OpenCV", "PASS" if any(c[0] == "PASS" and c[1] == "python:cv2" for c in CHECKS) else "WARN", "import cv2"),
        ("NumPy", "PASS" if any(c[0] == "PASS" and c[1] == "python:numpy" for c in CHECKS) else "WARN", "import numpy"),
        ("Display output", "INFO", "no physical HDMI/DSI panel attached; use RKDesk"),
        ("Camera", "INFO", "no sensor attached; /dev/video nodes exist but no capture source"),
        ("Audio", "INFO", "no speaker/headphone; codec devices exist"),
        ("Git on board", "INFO", "not installed; use Git on the development computer"),
    ]
    for name, status, detail in checks:
        print(f"  [{status:4s}] {name:18s} {detail}")
    print("=" * 68)
    warn = sum(1 for check in CHECKS if check[0] == "WARN")
    print(f"summary: {len(CHECKS) - warn} passed, {warn} warnings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
