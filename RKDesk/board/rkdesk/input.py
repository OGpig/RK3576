"""Virtual input devices for RKDesk mirror mode (stdlib only)."""

from __future__ import annotations

import fcntl
import os
import struct
import time

UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E
UI_DEV_SETUP = 0x405C5503
UI_ABS_SETUP = 0x401C5504
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0x00, 0x01, 0x02, 0x03
SYN_REPORT = 0

ABS_X, ABS_Y = 0x00, 0x01
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
REL_WHEEL, REL_HWHEEL = 0x08, 0x06
INPUT_PROP_POINTER = 0x00

KEY_CODES = {
    "ESC": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8,
    "8": 9, "9": 10, "0": 11, "MINUS": 12, "EQUAL": 13, "BACKSPACE": 14,
    "TAB": 15, "Q": 16, "W": 17, "E": 18, "R": 19, "T": 20, "Y": 21,
    "U": 22, "I": 23, "O": 24, "P": 25, "LEFTBRACE": 26, "RIGHTBRACE": 27,
    "ENTER": 28, "LEFTCTRL": 29, "A": 30, "S": 31, "D": 32, "F": 33,
    "G": 34, "H": 35, "J": 36, "K": 37, "L": 38, "SEMICOLON": 39,
    "APOSTROPHE": 40, "GRAVE": 41, "LEFTSHIFT": 42, "BACKSLASH": 43,
    "Z": 44, "X": 45, "C": 46, "V": 47, "B": 48, "N": 49, "M": 50,
    "COMMA": 51, "DOT": 52, "SLASH": 53, "RIGHTSHIFT": 54, "LEFTALT": 56,
    "SPACE": 57, "CAPSLOCK": 58, "F1": 59, "F2": 60, "F3": 61, "F4": 62,
    "F5": 63, "F6": 64, "F7": 65, "F8": 66, "F9": 67, "F10": 68,
    "F11": 87, "F12": 88, "RIGHTCTRL": 97, "RIGHTALT": 100,
    "HOME": 102, "UP": 103, "PAGEUP": 104, "LEFT": 105, "RIGHT": 106,
    "END": 107, "DOWN": 108, "PAGEDOWN": 109, "INSERT": 110, "DELETE": 111,
    "LEFTMETA": 125, "RIGHTMETA": 126,
}

SHIFT_MAP = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&",
    "8": "*", "9": "(", "0": ")", "MINUS": "_", "EQUAL": "+",
    "LEFTBRACE": "{", "RIGHTBRACE": "}", "SEMICOLON": ":", "APOSTROPHE": '"',
    "GRAVE": "~", "BACKSLASH": "|", "COMMA": "<", "DOT": ">", "SLASH": "?",
}

PLAIN_MAP = {
    "-": "MINUS", "=": "EQUAL", "[": "LEFTBRACE", "]": "RIGHTBRACE",
    ";": "SEMICOLON", "'": "APOSTROPHE", "`": "GRAVE", "\\": "BACKSLASH",
    ",": "COMMA", ".": "DOT", "/": "SLASH", " ": "SPACE",
}


def _setup_device(fd: int, name: str, axes: dict[int, tuple[int, int, int]]) -> None:
    # struct uinput_setup { struct input_id id; char name[80]; __u32 ff_effects_max; }
    setup = struct.pack("<HHHH80sI", 0x03, 0x1D6B, 0x0001, 0x0001, name.encode(), 0)
    fcntl.ioctl(fd, UI_DEV_SETUP, setup)
    for code, (minimum, maximum, resolution) in axes.items():
        absinfo = struct.pack("H2x6i", code, 0, minimum, maximum, 0, 0, resolution)
        fcntl.ioctl(fd, UI_ABS_SETUP, absinfo)


class UInput:
    def __init__(self, name: str, events: set[int], keys: set[int],
                 rels: set[int] = frozenset(),
                 axes: dict[int, tuple[int, int, int]] | None = None,
                 props: set[int] = frozenset()) -> None:
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        for event in events:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, event)
        for key in keys:
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, key)
        for rel in rels:
            fcntl.ioctl(self.fd, UI_SET_RELBIT, rel)
        for axis in (axes or {}):
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, axis)
        for prop in props:
            fcntl.ioctl(self.fd, UI_SET_PROPBIT, prop)
        _setup_device(self.fd, name, axes or {})
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        time.sleep(0.15)

    def emit(self, event_type: int, code: int, value: int) -> None:
        now = time.time()
        os.write(self.fd, struct.pack("<qqHHi", int(now), int((now % 1) * 1_000_000),
                                      event_type, code, value))

    def sync(self) -> None:
        self.emit(EV_SYN, SYN_REPORT, 0)

    def close(self) -> None:
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)


class AbsMouse(UInput):
    """Absolute pointer with buttons and wheel (libinput: pointer)."""

    def __init__(self, width: int, height: int, name: str = "RKDesk AbsMouse") -> None:
        self.width, self.height = width, height
        super().__init__(
            name, {EV_KEY, EV_ABS, EV_REL},
            {BTN_LEFT, BTN_RIGHT, BTN_MIDDLE},
            rels={REL_WHEEL, REL_HWHEEL},
            axes={ABS_X: (0, 32767, 10), ABS_Y: (0, 32767, 10)},
            props={INPUT_PROP_POINTER},
        )

    def move(self, x: int, y: int) -> None:
        sx = int(max(0, min(self.width, x)) * 32767 / max(1, self.width))
        sy = int(max(0, min(self.height, y)) * 32767 / max(1, self.height))
        self.emit(EV_ABS, ABS_X, sx)
        self.emit(EV_ABS, ABS_Y, sy)
        self.sync()

    def button(self, code: int, down: bool) -> None:
        self.emit(EV_KEY, code, 1 if down else 0)
        self.sync()

    def wheel(self, delta: int) -> None:
        self.emit(EV_REL, REL_WHEEL, delta)
        self.sync()


class Keyboard(UInput):
    def __init__(self, name: str = "RKDesk Keyboard") -> None:
        super().__init__(name, {EV_KEY}, set(KEY_CODES.values()))
    def key(self, code: int, down: bool) -> None:
        self.emit(EV_KEY, code, 1 if down else 0)
        self.sync()

    def type_text(self, text: str) -> None:
        for char in text:
            if char == "\n" or char == "\r":
                self.key(KEY_CODES["ENTER"], True)
                self.key(KEY_CODES["ENTER"], False)
                time.sleep(0.004)
                continue
            if char == "\t":
                self.key(KEY_CODES["TAB"], True)
                self.key(KEY_CODES["TAB"], False)
                time.sleep(0.004)
                continue
            shift = char.isupper()
            if char in SHIFT_MAP.values():
                code = next((KEY_CODES[name] for name, value in SHIFT_MAP.items() if value == char), None)
                shift = True
            elif char in PLAIN_MAP:
                code = KEY_CODES[PLAIN_MAP[char]]
            elif char.isalpha():
                code = KEY_CODES.get(char.upper())
            else:
                code = KEY_CODES.get(char)
            if code is None:
                continue
            if shift:
                self.key(KEY_CODES["LEFTSHIFT"], True)
            self.key(code, True)
            self.key(code, False)
            if shift:
                self.key(KEY_CODES["LEFTSHIFT"], False)
            time.sleep(0.004)


SPECIAL = {
    "Enter": "ENTER", "Backspace": "BACKSPACE", "Tab": "TAB", "Escape": "ESC",
    "ArrowUp": "UP", "ArrowDown": "DOWN", "ArrowLeft": "LEFT", "ArrowRight": "RIGHT",
    "Home": "HOME", "End": "END", "PageUp": "PAGEUP", "PageDown": "PAGEDOWN",
    "Insert": "INSERT", "Delete": "DELETE",
    "F1": "F1", "F2": "F2", "F3": "F3", "F4": "F4", "F5": "F5", "F6": "F6",
    "F7": "F7", "F8": "F8", "F9": "F9", "F10": "F10", "F11": "F11", "F12": "F12",
}


class VirtualInput:
    """Owns the uinput devices and applies high-level input commands."""

    def __init__(self, width: int = 800, height: int = 1280) -> None:
        self.mouse = AbsMouse(width, height)
        self.keyboard = Keyboard()

    def move(self, x: int, y: int) -> None:
        self.mouse.move(x, y)

    def button(self, button: int, down: bool) -> None:
        code = {0: BTN_LEFT, 1: BTN_MIDDLE, 2: BTN_RIGHT}.get(button, BTN_LEFT)
        self.mouse.button(code, down)

    def wheel(self, delta: float) -> None:
        self.mouse.wheel(-int(delta))

    def _press(self, code: int, ctrl: bool, alt: bool, shift: bool, meta: bool) -> None:
        modifiers = []
        if ctrl:
            modifiers.append(KEY_CODES["LEFTCTRL"])
        if alt:
            modifiers.append(KEY_CODES["LEFTALT"])
        if shift:
            modifiers.append(KEY_CODES["LEFTSHIFT"])
        if meta:
            modifiers.append(KEY_CODES["LEFTMETA"])
        for modifier in modifiers:
            self.keyboard.key(modifier, True)
        self.keyboard.key(code, True)
        self.keyboard.key(code, False)
        for modifier in reversed(modifiers):
            self.keyboard.key(modifier, False)

    def key(self, event: dict) -> None:
        name = str(event.get("key", ""))
        text = event.get("text")
        if name in SPECIAL:
            code = KEY_CODES.get(SPECIAL[name])
            if code is not None:
                self._press(code, bool(event.get("ctrl")), bool(event.get("alt")),
                            bool(event.get("shift")), bool(event.get("meta")))
            return
        if text:
            self.keyboard.type_text(str(text))
            return
        if len(name) == 1:
            self.keyboard.type_text(name)

    def text(self, value: str) -> None:
        self.keyboard.type_text(value)

    def resize(self, width: int, height: int) -> None:
        self.mouse.width, self.mouse.height = width, height

    def close(self) -> None:
        self.mouse.close()
        self.keyboard.close()

    def handle(self, command: dict) -> None:
        kind = command.get("t")
        if kind == "move":
            self.move(int(command.get("x", 0)), int(command.get("y", 0)))
        elif kind == "button":
            self.button(int(command.get("b", 0)), bool(command.get("down")))
        elif kind == "wheel":
            self.wheel(float(command.get("dy", 0)))
        elif kind == "key":
            self.key(command)
        elif kind in ("text", "paste"):
            self.text(str(command.get("text", "")))
        elif kind == "resize":
            self.resize(int(command.get("w", 800)), int(command.get("h", 1280)))
