#!/usr/bin/env python3
"""Create virtual input devices for RKDesk (stdlib only).

Two devices are exposed:
  * "RKDesk Tablet"   - absolute pointer (tablet) for positioning and left click
  * "RKDesk Keyboard" - full keyboard for key events

The tablet is used instead of a relative mouse so that browser coordinates map
1:1 to the board's 800x1280 panel.
"""

from __future__ import annotations

import fcntl
import os
import struct
import time

# linux/uinput.h
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
ABS_PRESSURE = 0x18
ABS_DISTANCE = 0x19
BTN_TOOL_PEN = 0x140
BTN_TOUCH = 0x14A
BTN_STYLUS = 0x14B
BTN_STYLUS2 = 0x14C
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
REL_WHEEL, REL_HWHEEL = 0x08, 0x06
INPUT_PROP_POINTER = 0x00
INPUT_PROP_DIRECT = 0x01

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


def _setup_device(fd: int, name: str, axes: dict[int, tuple[int, int, int]]) -> None:
    """Configure a uinput device with the modern API (resolution included)."""
    # struct uinput_setup { struct input_id id; char name[80]; __u32 ff_effects_max; }
    setup = struct.pack("<HHHH80sI", 0x03, 0x1D6B, 0x0001, 0x0001, name.encode(), 0)
    fcntl.ioctl(fd, UI_DEV_SETUP, setup)
    for code, (minimum, maximum, resolution) in axes.items():
        # struct uinput_abs_setup { __u16 code; __u16 pad; struct input_absinfo; }
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
        time.sleep(0.2)

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


class Tablet(UInput):
    def __init__(self, width: int, height: int, name: str = "RKDesk Tablet") -> None:
        self.width, self.height = width, height
        super().__init__(name, {EV_KEY, EV_ABS},
                         {BTN_TOOL_PEN, BTN_TOUCH, BTN_STYLUS, BTN_STYLUS2},
                         axes={ABS_X: (0, 32767, 10), ABS_Y: (0, 32767, 10),
                               ABS_PRESSURE: (0, 1023, 0), ABS_DISTANCE: (0, 100, 0)},
                         props={INPUT_PROP_DIRECT})
        self.in_proximity = False

    def move(self, x: int, y: int) -> None:
        if not self.in_proximity:
            self.emit(EV_KEY, BTN_TOOL_PEN, 1)
            self.in_proximity = True
        sx = int(max(0, min(self.width, x)) * 32767 / self.width)
        sy = int(max(0, min(self.height, y)) * 32767 / self.height)
        self.emit(EV_ABS, ABS_X, sx)
        self.emit(EV_ABS, ABS_Y, sy)
        self.emit(EV_ABS, ABS_DISTANCE, 0)
        self.sync()

    def button(self, down: bool, right: bool = False) -> None:
        self.emit(EV_KEY, BTN_STYLUS if right else BTN_TOUCH, 1 if down else 0)
        self.emit(EV_ABS, ABS_PRESSURE, 800 if down and not right else 0)
        self.sync()

    def leave(self) -> None:
        if self.in_proximity:
            self.emit(EV_KEY, BTN_TOOL_PEN, 0)
            self.sync()
            self.in_proximity = False


class Mouse(UInput):
    def __init__(self, name: str = "RKDesk Wheel") -> None:
        super().__init__(name, {EV_KEY, EV_REL},
                         {BTN_LEFT, BTN_RIGHT, BTN_MIDDLE},
                         rels={REL_WHEEL, REL_HWHEEL})

    def wheel(self, delta: int) -> None:
        self.emit(EV_REL, REL_WHEEL, delta)
        self.sync()

    def button(self, code: int, down: bool) -> None:
        self.emit(EV_KEY, code, 1 if down else 0)
        self.sync()


class Keyboard(UInput):
    def __init__(self, name: str = "RKDesk Keyboard") -> None:
        super().__init__(name, {EV_KEY}, set(KEY_CODES.values()))

    def key(self, code: int, down: bool) -> None:
        self.emit(EV_KEY, code, 1 if down else 0)
        self.sync()

    def type_text(self, text: str) -> None:
        for char in text:
            shift = char in "~!@#$%^&*()_+{}|:\"<>?"
            base = char.upper() if char.isalpha() else char
            if base == " ":
                code = KEY_CODES["SPACE"]
            elif base in SHIFT_MAP.values():
                code = next((KEY_CODES[key] for key, value in SHIFT_MAP.items() if value == base), None)
                shift = True
            else:
                code = KEY_CODES.get(base) or KEY_CODES.get(base.upper())
            if code is None:
                continue
            if shift:
                self.key(KEY_CODES["LEFTSHIFT"], True)
            self.key(code, True)
            self.key(code, False)
            if shift:
                self.key(KEY_CODES["LEFTSHIFT"], False)


if __name__ == "__main__":
    import sys

    tablet = Tablet(800, 1280)
    keyboard = Keyboard()
    print("devices created; sleeping 60s", flush=True)
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        time.sleep(5)
        tablet.move(770, 200)
        time.sleep(0.4)
        tablet.button(True)
        time.sleep(0.1)
        tablet.button(False)
        print("clicked terminal icon", flush=True)
    time.sleep(60)
    tablet.close()
    keyboard.close()
