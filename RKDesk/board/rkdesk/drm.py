"""DRM/KMS scanout capture for RK3576.

Weston draws into GBM buffers, so /dev/fb0 is black.  The CRTC's current
framebuffer is exported as a dma-buf, which we mmap to read the pixels that are
actually being scanned out to HDMI/DSI.
"""

from __future__ import annotations

import ctypes
import hashlib
import mmap
import os

from PIL import Image


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
        self.width, self.height = self._crtc_size()

    def _prototypes(self) -> None:
        lib = self.lib
        lib.drmModeGetResources.argtypes = [ctypes.c_int]
        lib.drmModeGetResources.restype = ctypes.POINTER(DrmModeRes)
        lib.drmModeFreeResources.argtypes = [ctypes.POINTER(DrmModeRes)]
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

    def _crtc_size(self) -> tuple[int, int]:
        crtc = self.lib.drmModeGetCrtc(self.fd, self.crtc_id)
        if not crtc:
            return 800, 1280
        try:
            return crtc.contents.width, crtc.contents.height
        finally:
            self.lib.drmModeFreeCrtc(crtc)

    def grab(self) -> tuple[Image.Image, bytes]:
        crtc = self.lib.drmModeGetCrtc(self.fd, self.crtc_id)
        if not crtc:
            raise RuntimeError("drmModeGetCrtc failed")
        try:
            fb_id = crtc.contents.buffer_id
            crtc_width = crtc.contents.width
            crtc_height = crtc.contents.height
        finally:
            self.lib.drmModeFreeCrtc(crtc)
        fb = self.lib.drmModeGetFB(self.fd, fb_id)
        if not fb:
            raise RuntimeError("drmModeGetFB failed")
        try:
            width = fb.contents.width or crtc_width
            height = fb.contents.height or crtc_height
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
            signature = hashlib.blake2b(mapped, digest_size=10).digest()
            image = Image.frombuffer("RGB", (width, height), mapped, "raw", "BGRX", pitch, 1)
            copy = image.copy()
        finally:
            mapped.close()
        self.width, self.height = width, height
        return copy, signature

    def close(self) -> None:
        os.close(self.fd)
