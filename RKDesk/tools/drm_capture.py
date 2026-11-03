#!/usr/bin/env python3
"""Capture the real DRM/KMS scanout buffer of the RK3576 display.

Weston renders into GBM buffers, so /dev/fb0 stays black and the screenshooter
protocol is not authorized for external clients.  The CRTC's current
framebuffer is still exported as a dma-buf, which we can mmap and read.

Usage on the board:
    python3 drm_capture.py /tmp/shot.jpg
"""

from __future__ import annotations

import ctypes
import io
import mmap
import os
import sys

from PIL import Image

DRM_CONNECTED = 1
DRM_FORMAT_XRGB8888 = 0x34325258


class DrmModeModeInfo(ctypes.Structure):
    _fields_ = [
        ("clock", ctypes.c_uint32),
        ("hdisplay", ctypes.c_uint16), ("hsync_start", ctypes.c_uint16),
        ("hsync_end", ctypes.c_uint16), ("htotal", ctypes.c_uint16),
        ("hskew", ctypes.c_uint16),
        ("vdisplay", ctypes.c_uint16), ("vsync_start", ctypes.c_uint16),
        ("vsync_end", ctypes.c_uint16), ("vtotal", ctypes.c_uint16),
        ("vscan", ctypes.c_uint16),
        ("vrefresh", ctypes.c_uint32), ("flags", ctypes.c_uint32),
        ("type", ctypes.c_uint32), ("name", ctypes.c_char * 32),
    ]


class DrmModeRes(ctypes.Structure):
    _fields_ = [
        ("count_fbs", ctypes.c_int), ("fbs", ctypes.POINTER(ctypes.c_uint32)),
        ("count_crtcs", ctypes.c_int), ("crtcs", ctypes.POINTER(ctypes.c_uint32)),
        ("count_connectors", ctypes.c_int), ("connectors", ctypes.POINTER(ctypes.c_uint32)),
        ("count_encoders", ctypes.c_int), ("encoders", ctypes.POINTER(ctypes.c_uint32)),
        ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
        ("min_height", ctypes.c_uint32), ("max_height", ctypes.c_uint32),
    ]


class DrmModeConnector(ctypes.Structure):
    _fields_ = [
        ("connector_id", ctypes.c_uint32),
        ("encoder_id", ctypes.c_uint32),
        ("connector_type", ctypes.c_uint32),
        ("connector_type_id", ctypes.c_uint32),
        ("connection", ctypes.c_int),
        ("mmWidth", ctypes.c_uint32), ("mmHeight", ctypes.c_uint32),
        ("subpixel", ctypes.c_int),
        ("count_modes", ctypes.c_int), ("modes", ctypes.POINTER(DrmModeModeInfo)),
        ("count_props", ctypes.c_int), ("props", ctypes.POINTER(ctypes.c_uint32)),
        ("prop_values", ctypes.POINTER(ctypes.c_uint64)),
        ("count_encoders", ctypes.c_int), ("encoders", ctypes.POINTER(ctypes.c_uint32)),
    ]


class DrmModeEncoder(ctypes.Structure):
    _fields_ = [
        ("encoder_id", ctypes.c_uint32), ("encoder_type", ctypes.c_uint32),
        ("crtc_id", ctypes.c_uint32), ("possible_crtcs", ctypes.c_uint32),
        ("possible_clones", ctypes.c_uint32),
    ]


class DrmModeCrtc(ctypes.Structure):
    _fields_ = [
        ("crtc_id", ctypes.c_uint32), ("buffer_id", ctypes.c_uint32),
        ("x", ctypes.c_uint32), ("y", ctypes.c_uint32),
        ("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
        ("mode_valid", ctypes.c_int), ("mode", DrmModeModeInfo),
        ("gamma_size", ctypes.c_int),
    ]


class DrmModeFB(ctypes.Structure):
    _fields_ = [
        ("fb_id", ctypes.c_uint32), ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32), ("pitch", ctypes.c_uint32),
        ("bpp", ctypes.c_uint32), ("depth", ctypes.c_uint32),
        ("handle", ctypes.c_uint32),
    ]


class DrmCapture:
    def __init__(self, device: str = "/dev/dri/card0") -> None:
        self.lib = ctypes.CDLL("libdrm.so.2")
        self._prototypes()
        self.fd = os.open(device, os.O_RDWR)
        self.crtc_id = self._find_crtc()

    def _prototypes(self) -> None:
        lib = self.lib
        lib.drmModeGetResources.argtypes = [ctypes.c_int]
        lib.drmModeGetResources.restype = ctypes.POINTER(DrmModeRes)
        lib.drmModeFreeResources.argtypes = [ctypes.POINTER(DrmModeRes)]
        lib.drmModeGetConnector.argtypes = [ctypes.c_int, ctypes.c_uint32]
        lib.drmModeGetConnector.restype = ctypes.POINTER(DrmModeConnector)
        lib.drmModeFreeConnector.argtypes = [ctypes.POINTER(DrmModeConnector)]
        lib.drmModeGetEncoder.argtypes = [ctypes.c_int, ctypes.c_uint32]
        lib.drmModeGetEncoder.restype = ctypes.POINTER(DrmModeEncoder)
        lib.drmModeFreeEncoder.argtypes = [ctypes.POINTER(DrmModeEncoder)]
        lib.drmModeGetCrtc.argtypes = [ctypes.c_int, ctypes.c_uint32]
        lib.drmModeGetCrtc.restype = ctypes.POINTER(DrmModeCrtc)
        lib.drmModeFreeCrtc.argtypes = [ctypes.POINTER(DrmModeCrtc)]
        lib.drmModeGetFB.argtypes = [ctypes.c_int, ctypes.c_uint32]
        lib.drmModeGetFB.restype = ctypes.POINTER(DrmModeFB)
        lib.drmModeFreeFB.argtypes = [ctypes.POINTER(DrmModeFB)]
        lib.drmPrimeHandleToFD.argtypes = [ctypes.c_int, ctypes.c_uint32,
                                           ctypes.c_uint32, ctypes.POINTER(ctypes.c_int)]
        lib.drmPrimeHandleToFD.restype = ctypes.c_int

    def _find_crtc(self) -> int:
        res = self.lib.drmModeGetResources(self.fd)
        if not res:
            raise RuntimeError("drmModeGetResources failed")
        try:
            best = (0, 0)
            for index in range(res.contents.count_crtcs):
                crtc_id = res.contents.crtcs[index]
                crtc = self.lib.drmModeGetCrtc(self.fd, crtc_id)
                if not crtc:
                    continue
                try:
                    area = crtc.contents.width * crtc.contents.height
                    if crtc.contents.buffer_id and area > best[0]:
                        best = (area, crtc_id)
                finally:
                    self.lib.drmModeFreeCrtc(crtc)
            if not best[1]:
                raise RuntimeError("no active CRTC with a framebuffer")
            return best[1]
        finally:
            self.lib.drmModeFreeResources(res)

    def grab(self) -> Image.Image:
        crtc = self.lib.drmModeGetCrtc(self.fd, self.crtc_id)
        if not crtc:
            raise RuntimeError("drmModeGetCrtc failed")
        try:
            fb_id = crtc.contents.buffer_id
            width = crtc.contents.width
            height = crtc.contents.height
        finally:
            self.lib.drmModeFreeCrtc(crtc)
        fb = self.lib.drmModeGetFB(self.fd, fb_id)
        if not fb:
            raise RuntimeError("drmModeGetFB failed")
        try:
            width = fb.contents.width or width
            height = fb.contents.height or height
            pitch = fb.contents.pitch
            handle = fb.contents.handle
        finally:
            self.lib.drmModeFreeFB(fb)
        prime_fd = ctypes.c_int(-1)
        if self.lib.drmPrimeHandleToFD(self.fd, handle, 0, ctypes.byref(prime_fd)) != 0:
            raise RuntimeError("drmPrimeHandleToFD failed")
        size = pitch * height
        try:
            mapped = mmap.mmap(prime_fd.value, size, mmap.MAP_SHARED, mmap.PROT_READ)
        finally:
            os.close(prime_fd.value)
        try:
            image = Image.frombuffer("RGB", (width, height), mapped, "raw", "BGRX", pitch, 1)
            return image.copy()
        finally:
            mapped.close()

    def close(self) -> None:
        os.close(self.fd)


def main() -> int:
    output = sys.argv[1] if len(sys.argv) > 1 else "/tmp/drm-shot.jpg"
    capture = DrmCapture()
    try:
        image = capture.grab()
        image.save(output, quality=80)
        print(f"saved {output} {image.width}x{image.height}")
    finally:
        capture.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
