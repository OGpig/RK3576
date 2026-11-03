#!/usr/bin/env python3
"""Test an absolute pointer (ABS_X/Y + buttons) against Weston."""

import sys
import time

sys.path.insert(0, "/tmp")
import virtual_input as v  # noqa: E402

mouse = v.UInput(
    "RKDesk AbsMouse",
    {v.EV_KEY, v.EV_ABS, v.EV_REL},
    {v.BTN_LEFT, v.BTN_RIGHT, v.BTN_MIDDLE},
    rels={v.REL_WHEEL},
    axes={v.ABS_X: (0, 32767, 10), v.ABS_Y: (0, 32767, 10)},
    props={v.INPUT_PROP_POINTER},
)


def move(x, y):
    mouse.emit(v.EV_ABS, v.ABS_X, int(x * 32767 / 800))
    mouse.emit(v.EV_ABS, v.ABS_Y, int(y * 32767 / 1280))
    mouse.sync()


def click():
    mouse.emit(v.EV_KEY, v.BTN_LEFT, 1)
    mouse.sync()
    time.sleep(0.08)
    mouse.emit(v.EV_KEY, v.BTN_LEFT, 0)
    mouse.sync()


print("created", flush=True)
time.sleep(5)
move(770, 200)
time.sleep(0.3)
click()
time.sleep(0.25)
click()
print("double-clicked terminal icon", flush=True)
time.sleep(1.0)
move(40, 1240)
time.sleep(0.3)
click()
print("clicked panel button", flush=True)
time.sleep(60)
