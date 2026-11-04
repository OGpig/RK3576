"""Audio/video plumbing for RKDesk.

Audio is deliberately PCM over a dedicated WebSocket: 48 kHz stereo downwards
(board -> browser) and 48 kHz mono upwards (browser -> virtual microphone).
PulseAudio provides both the monitor source and the virtual microphone; ALSA
is used as a read-only fallback when PulseAudio is unavailable.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import threading
import time
import wave

RATE = 48000
CAP_CHANNELS = 2
MIC_CHANNELS = 1
FRAME_MS = 20
CAP_FRAME = int(RATE * CAP_CHANNELS * 2 * FRAME_MS / 1000)   # 3840 bytes
MIC_FRAME = int(RATE * MIC_CHANNELS * 2 * FRAME_MS / 1000)   # 1920 bytes
TONE_PATH = "/tmp/rkdesk-tone.wav"
PREV_SOURCE_FILE = "/var/run/rkdesk-mic.previous"


def _base_env() -> dict:
    env = os.environ.copy()
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    return env


class Pulse:
    def __init__(self) -> None:
        self.env = None
        self.info = ""
        if not shutil.which("pactl"):
            return
        candidates = [
            _base_env(),
            {**_base_env(), "PULSE_SERVER": "unix:/tmp/pulse-socket"},
            {**_base_env(), "XDG_RUNTIME_DIR": "/var/run"},
        ]
        for env in candidates:
            try:
                result = subprocess.run(["pactl", "info"], env=env, capture_output=True,
                                        text=True, timeout=3)
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0:
                self.env = env
                self.info = result.stdout
                break

    @property
    def ok(self) -> bool:
        return self.env is not None

    def refresh(self) -> bool:
        if self.ok:
            return True
        candidates = [
            _base_env(),
            {**_base_env(), "PULSE_SERVER": "unix:/tmp/pulse-socket"},
            {**_base_env(), "XDG_RUNTIME_DIR": "/var/run"},
        ]
        for env in candidates:
            try:
                result = subprocess.run(["pactl", "info"], env=env, capture_output=True,
                                        text=True, timeout=3)
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0:
                self.env = env
                self.info = result.stdout
                return True
        return False

    def run(self, args: list[str], timeout: float = 4.0) -> subprocess.CompletedProcess:
        if not self.ok:
            raise RuntimeError("PulseAudio is not available")
        return subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=timeout)

    def get_default_sink(self) -> str:
        try:
            result = self.run(["pactl", "get-default-sink"])
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass
        for line in self.info.splitlines():
            if line.startswith("Default Sink:"):
                return line.split(":", 1)[1].strip()
        return ""

    def get_default_source(self) -> str:
        try:
            result = self.run(["pactl", "get-default-source"])
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass
        for line in self.info.splitlines():
            if line.startswith("Default Source:"):
                return line.split(":", 1)[1].strip()
        return ""

    def set_default_source(self, name: str) -> None:
        if not name:
            return
        try:
            self.run(["pactl", "set-default-source", name])
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass

    def sources(self) -> list[dict]:
        try:
            result = self.run(["pactl", "list", "short", "sources"])
        except (OSError, subprocess.SubprocessError, RuntimeError):
            return []
        if result.returncode != 0:
            return []
        items = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            name = fields[1]
            kind = "monitor" if name.endswith(".monitor") else "input"
            label = "系统声音" if kind == "monitor" else "板端麦克风"
            if kind == "monitor" and name.count(".") > 1:
                label = "系统声音"
            items.append({"name": name, "kind": kind, "label": label,
                          "driver": fields[2] if len(fields) > 2 else ""})
        return items

    def modules(self) -> list[tuple[int, str]]:
        try:
            result = self.run(["pactl", "list", "short", "modules"])
        except (OSError, subprocess.SubprocessError, RuntimeError):
            return []
        modules = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) >= 3 and fields[0].isdigit():
                modules.append((int(fields[0]), fields[2]))
        return modules

    def unload_module(self, module_id: int) -> None:
        try:
            self.run(["pactl", "unload-module", str(module_id)])
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass

    def load_module(self, args: list[str]) -> int | None:
        try:
            result = self.run(["pactl", "load-module"] + args)
        except (OSError, subprocess.SubprocessError, RuntimeError):
            return None
        if result.returncode != 0:
            return None
        try:
            return int(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None


class VirtualMic:
    """A null sink + remap source exposed as 'RKDesk Microphone'."""

    def __init__(self, pulse: Pulse) -> None:
        self.pulse = pulse
        self.proc: subprocess.Popen | None = None
        self.sink_module: int | None = None
        self.source_module: int | None = None
        self.previous_source = ""
        self.active = False
        self.last_error = ""

    def _clean_stale(self) -> None:
        for module_id, args in self.pulse.modules():
            if "rkdesk_mic" in args:
                self.pulse.unload_module(module_id)

    def restore_if_crashed(self) -> None:
        """Restore the default source left behind by a killed server."""
        try:
            with open(PREV_SOURCE_FILE, "r", encoding="utf-8") as handle:
                previous = handle.read().strip()
        except OSError:
            previous = ""
        if previous and previous != "rkdesk_mic_source":
            self.pulse.set_default_source(previous)
        try:
            os.unlink(PREV_SOURCE_FILE)
        except OSError:
            pass
        self._clean_stale()

    def start(self) -> tuple[bool, str]:
        if not self.pulse.ok:
            return False, "PulseAudio 不可用，无法创建虚拟麦克风"
        if self.active and self.proc and self.proc.poll() is None:
            return True, ""
        self._clean_stale()
        self.previous_source = self.pulse.get_default_source()
        try:
            with open(PREV_SOURCE_FILE, "w", encoding="utf-8") as handle:
                handle.write(self.previous_source)
        except OSError:
            pass

        self.sink_module = self.pulse.load_module([
            "module-null-sink", "sink_name=rkdesk_mic",
            "sink_properties=device.description=RKDesk_Microphone",
        ])
        if self.sink_module is None:
            self.last_error = "无法加载 module-null-sink"
            return False, self.last_error
        self.source_module = self.pulse.load_module([
            "module-remap-source", "master=rkdesk_mic.monitor",
            "source_name=rkdesk_mic_source",
            "source_properties=device.description=RKDesk_Microphone",
        ])
        if self.source_module is None:
            self.pulse.unload_module(self.sink_module)
            self.sink_module = None
            if self.previous_source:
                self.pulse.set_default_source(self.previous_source)
            try:
                os.unlink(PREV_SOURCE_FILE)
            except OSError:
                pass
            self.last_error = "无法加载 module-remap-source"
            return False, self.last_error

        self.pulse.set_default_source("rkdesk_mic_source")
        try:
            self.proc = subprocess.Popen(
                ["pacat", "--device=rkdesk_mic", "--format=s16le",
                 f"--rate={RATE}", f"--channels={MIC_CHANNELS}", "--raw",
                 "--latency-msec=30"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, bufsize=0, env=self.pulse.env,
            )
        except OSError as error:
            self.last_error = f"pacat 启动失败: {error}"
            self._teardown()
            if self.previous_source:
                self.pulse.set_default_source(self.previous_source)
            try:
                os.unlink(PREV_SOURCE_FILE)
            except OSError:
                pass
            return False, self.last_error
        self.active = True
        self.last_error = ""
        return True, ""

    def write(self, pcm: bytes) -> None:
        if not self.active or not self.proc or not self.proc.stdin:
            return
        if self.proc.poll() is not None:
            self.active = False
            return
        try:
            self.proc.stdin.write(pcm)
        except (BrokenPipeError, OSError):
            self.active = False

    def _teardown(self) -> None:
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
            self.proc = None
        if self.source_module is not None:
            self.pulse.unload_module(self.source_module)
            self.source_module = None
        if self.sink_module is not None:
            self.pulse.unload_module(self.sink_module)
            self.sink_module = None
        self.active = False

    def stop(self) -> None:
        if not self.active and not self.sink_module and not self.source_module:
            return
        self._teardown()
        previous = self.previous_source
        if previous and previous != "rkdesk_mic_source":
            self.pulse.set_default_source(previous)
        try:
            os.unlink(PREV_SOURCE_FILE)
        except OSError:
            pass


class AudioCapture:
    def __init__(self, pulse: Pulse, source: str, on_chunk) -> None:
        self.pulse = pulse
        self.source = source
        self.on_chunk = on_chunk
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.stopping = False
        self.error = ""

    def start(self) -> bool:
        if self.pulse.ok:
            args = ["parec", "--device", self.source, "--format=s16le",
                    f"--rate={RATE}", f"--channels={CAP_CHANNELS}", "--raw",
                    "--latency-msec=30"]
            env = self.pulse.env
        else:
            if not shutil.which("arecord"):
                self.error = "没有 PulseAudio / arecord"
                return False
            args = ["arecord", "-D", "default", "-f", "S16_LE", "-r", str(RATE),
                    "-c", str(CAP_CHANNELS), "-t", "raw", "-q"]
            env = _base_env()
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=0, env=env)
        except OSError as error:
            self.error = str(error)
            return False
        self.thread = threading.Thread(target=self._reader, daemon=True,
                                       name="av-capture")
        self.thread.start()
        return True

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        while not self.stopping:
            try:
                chunk = self._read_exact(CAP_FRAME)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            try:
                self.on_chunk(chunk)
            except Exception:
                break

    def _read_exact(self, size: int) -> bytes:
        assert self.proc and self.proc.stdout
        buffer = bytearray()
        while len(buffer) < size and not self.stopping:
            chunk = self.proc.stdout.read(size - len(buffer))
            if not chunk:
                break
            buffer.extend(chunk)
        return bytes(buffer)

    def stop(self) -> None:
        self.stopping = True
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
            self.proc = None


class AVHub:
    def __init__(self, https_port: int = 8443) -> None:
        self.pulse = Pulse()
        self.mic = VirtualMic(self.pulse)
        self.https_port = https_port
        self.clients: set = set()
        self.lock = threading.RLock()
        self.capture: AudioCapture | None = None
        self.capture_source = ""
        self.mic_owner = None
        self.last_error = ""
        if self.pulse.ok:
            self.mic.restore_if_crashed()

    def status(self) -> dict:
        if not self.pulse.ok:
            self.pulse.refresh()
        sources = self.pulse.sources() if self.pulse.ok else []
        default_source = self.pulse.get_default_source() if self.pulse.ok else ""
        monitor = self._default_monitor(sources)
        if not self.capture_source:
            self.capture_source = monitor or (sources[0]["name"] if sources else "")
        return {
            "t": "av",
            "pulse": self.pulse.ok,
            "server": "pulseaudio" if self.pulse.ok else "alsa",
            "sources": sources,
            "default_source": default_source,
            "audio_source": self.capture_source,
            "virtual_mic": self.mic.active,
            "virtual_mic_supported": self.pulse.ok,
            "https_port": self.https_port,
            "last_error": self.last_error,
            "note": self.last_error or (
                "PulseAudio · 虚拟麦克风 RKDesk Microphone" if self.pulse.ok
                else "未检测到 PulseAudio，浏览器麦克风不可用"
            ),
        }

    @staticmethod
    def _default_monitor(sources: list[dict]) -> str:
        for item in sources:
            if item["kind"] == "monitor":
                return item["name"]
        return ""

    def add_client(self, client) -> None:
        with self.lock:
            self.clients.add(client)
        client.send_text(json.dumps(self.status(), separators=(",", ":")))

    def remove_client(self, client) -> None:
        with self.lock:
            self.clients.discard(client)
            empty = not self.clients
            owner_left = client is self.mic_owner
            if owner_left:
                self.mic_owner = None
        if owner_left:
            self.mic.stop()
        if empty:
            self.stop_audio()
            self.mic.stop()

    def _broadcast_status(self) -> None:
        payload = json.dumps(self.status(), separators=(",", ":"))
        for client in list(self.clients):
            client.send_text(payload)

    def handle(self, client, message: dict) -> None:
        kind = message.get("t")
        if kind == "audio_start":
            source = str(message.get("source") or "")
            self.start_audio(source)
            self._broadcast_status()
        elif kind == "audio_stop":
            self.stop_audio()
            self._broadcast_status()
        elif kind == "mic_start":
            self.pulse.refresh()
            ok, error = self.mic.start()
            if ok:
                self.mic_owner = client
            self.last_error = "" if ok else error
            self._broadcast_status()
        elif kind == "mic_stop":
            self.mic.stop()
            self.mic_owner = None
            self.last_error = ""
            self._broadcast_status()
        elif kind == "test_tone":
            self.play_test_tone()
        elif kind == "status":
            client.send_text(json.dumps(self.status(), separators=(",", ":")))

    def handle_binary(self, client, payload: bytes) -> None:
        if len(payload) < 2:
            return
        if len(payload) > MIC_FRAME * 6:
            payload = payload[:MIC_FRAME * 6]
        self.mic.write(payload)

    def start_audio(self, source: str = "") -> None:
        with self.lock:
            sources = self.pulse.sources() if self.pulse.ok else []
            valid = {item["name"] for item in sources}
            if not source or (valid and source not in valid):
                source = self._default_monitor(sources) or (sources[0]["name"] if sources else "")
            if self.capture and self.capture_source == source:
                return
            self.stop_audio()
            if not source:
                self.last_error = "没有可用的录音设备"
                return
            capture = AudioCapture(self.pulse, source, self._on_audio)
            if not capture.start():
                self.last_error = f"无法打开音频源: {capture.error}"
                return
            self.capture = capture
            self.capture_source = source
            self.last_error = ""

    def stop_audio(self) -> None:
        with self.lock:
            if self.capture:
                self.capture.stop()
                self.capture = None

    def _on_audio(self, chunk: bytes) -> None:
        for client in list(self.clients):
            try:
                client.send_audio(chunk)
            except Exception:
                pass

    def play_test_tone(self) -> None:
        try:
            self._ensure_tone()
        except Exception as error:
            self.last_error = f"测试音生成失败: {error}"
            return
        if self.pulse.ok:
            args = ["paplay", TONE_PATH]
            env = self.pulse.env
        elif shutil.which("aplay"):
            args = ["aplay", "-q", TONE_PATH]
            env = _base_env()
        else:
            self.last_error = "没有 paplay/aplay"
            return
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env)
        except OSError as error:
            self.last_error = f"测试音播放失败: {error}"

    @staticmethod
    def _ensure_tone() -> None:
        if os.path.exists(TONE_PATH) and os.path.getsize(TONE_PATH) > 1000:
            return
        with wave.open(TONE_PATH, "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(RATE)
            frames = bytearray()
            for index in range(int(RATE * 1.2)):
                value = int(0.25 * 32767 * math.sin(2 * math.pi * 440 * index / RATE))
                frames += struct.pack("<hh", value, value)
            handle.writeframes(frames)

    def close(self) -> None:
        self.stop_audio()
        self.mic.stop()
