#!/usr/bin/env python3
"""OxRender local PNG receiver - standard library only.

Run with: python oxrender_receiver.py
Receives raw RGBA8 screenshots from the OxRender Studio plugin and saves PNGs
under ~/Pictures/OxRenderExports. The server binds only to 127.0.0.1.
"""
from __future__ import annotations

from datetime import datetime
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import re
import struct
import zlib
import math
import sys
import ctypes
import urllib.request
import urllib.error
from urllib.parse import quote
import uuid
from ctypes import wintypes

HOST = "127.0.0.1"
PORT = 8765
TOKEN = "oxrender-local-v1"
MAX_BODY_BYTES = 64 * 1024 * 1024
MAX_DIMENSION = 4096
OUTPUT_DIR = Path.home() / "Pictures" / "OxRenderExports"
STAGING_DIR = OUTPUT_DIR / ".staging"
CONFIG_PATH = Path(__file__).with_name("oxrender_config.json")
LAST_CAPTURE_RAW_PATH: Path | None = None
LAST_CAPTURE_RAW_NAME = "OxRender"


# StudioCaptureService currently reports "Feature not supported yet" in some
# Studio builds. On Windows, use a local desktop capture of Studio's actual
# viewport as a fallback. No remote host is exposed; this service binds loopback.
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
BI_RGB = 0
HALFTONE = 4
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _configure_process_dpi_awareness() -> str:
    """Use physical desktop pixels consistently for window rectangles and GDI capture.

    Without DPI awareness, Windows may virtualize HWND rectangles to logical pixels
    while the desktop DC captures physical pixels. That mismatch makes the helper crop
    a smaller region and stretch it, which looks zoomed-in on scaled displays.
    """
    if sys.platform != "win32":
        return "not applicable"

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = wintypes.BOOL
        if set_context(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return "Per-monitor V2"
        win_error = ctypes.get_last_error()
        if win_error == 5:  # ERROR_ACCESS_DENIED: process awareness already established.
            try:
                get_context = user32.GetThreadDpiAwarenessContext
                get_context.argtypes = []
                get_context.restype = ctypes.c_void_p
                get_awareness = user32.GetAwarenessFromDpiAwarenessContext
                get_awareness.argtypes = [ctypes.c_void_p]
                get_awareness.restype = ctypes.c_int
                awareness = int(get_awareness(get_context()))
                names = {
                    0: "DPI-unaware (already configured)",
                    1: "System-aware (already configured)",
                    2: "Per-monitor aware (already configured)",
                }
                return names.get(awareness, "already configured by Windows")
            except Exception:
                return "already configured by Windows"
    except Exception:
        pass

    # Fallback for Windows versions without SetProcessDpiAwarenessContext.
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        set_awareness = shcore.SetProcessDpiAwareness
        set_awareness.argtypes = [ctypes.c_int]
        set_awareness.restype = ctypes.c_long
        result = int(set_awareness(2))  # PROCESS_PER_MONITOR_DPI_AWARE
        if result == 0:
            return "Per-monitor"
        if (result & 0xFFFFFFFF) == 0x80070005:
            return "already configured by Windows"
    except Exception:
        pass

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_aware = user32.SetProcessDPIAware
        set_aware.argtypes = []
        set_aware.restype = wintypes.BOOL
        if set_aware():
            return "System-aware fallback"
    except Exception:
        pass
    return "Windows default (could not change process awareness)"


# This must run before the first window-rectangle or screen-capture API call.
DPI_AWARENESS_MODE = _configure_process_dpi_awareness()


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [("rgbBlue", wintypes.BYTE), ("rgbGreen", wintypes.BYTE), ("rgbRed", wintypes.BYTE), ("rgbReserved", wintypes.BYTE)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


def _get_text(hwnd: int) -> str:
    if sys.platform != "win32":
        return ""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _get_client_screen_rect(user32, hwnd: int) -> tuple[int, int, int, int] | None:
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    top_left = POINT(0, 0)
    bottom_right = POINT(width, height)
    if not user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
        return None
    if not user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
        return None
    return top_left.x, top_left.y, bottom_right.x, bottom_right.y


def _find_studio_root_client_rect() -> tuple[int, int, int, int]:
    """Return the foreground Roblox Studio root client rectangle in desktop pixels."""
    if sys.platform != "win32":
        raise RuntimeError("Viewport geometry is currently supported on Windows only.")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL

    foreground = user32.GetForegroundWindow()
    candidates: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    @enum_proc
    def enum_top(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _get_text(hwnd).lower()
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
        if "roblox studio" in title or "robloxstudiobeta" in class_buffer.value.lower():
            candidates.append(hwnd)
        return True

    user32.EnumWindows(enum_top, 0)
    root = foreground if foreground in candidates else (candidates[0] if candidates else None)
    if root is None:
        raise RuntimeError("Could not find the Roblox Studio root window for the resolution guide.")
    rect = _get_client_screen_rect(user32, root)
    if rect is None:
        raise RuntimeError("Could not read the Roblox Studio client window size.")
    return rect


def _find_studio_viewport_rect(expected_width: int, expected_height: int) -> tuple[int, int, int, int]:
    if sys.platform != "win32":
        raise RuntimeError("The viewport fallback is currently supported on Windows only.")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    target_aspect = expected_width / max(1, expected_height)
    target_area = expected_width * expected_height
    foreground = user32.GetForegroundWindow()
    roots: list[int] = []
    candidates: list[tuple[int, tuple[int, int, int, int], bool]] = []

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    enum_child_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [wintypes.HWND, enum_child_proc, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL

    def add_window(hwnd: int, is_root: bool) -> None:
        if not user32.IsWindowVisible(hwnd):
            return
        if is_root and user32.IsIconic(hwnd):
            return
        rect = _get_client_screen_rect(user32, hwnd)
        if not rect:
            return
        left, top, right, bottom = rect
        width, height = right - left, bottom - top
        if width < 320 or height < 220:
            return
        # Exclude windows whose client area lies wholly outside the display bounds.
        screen_width = user32.GetSystemMetrics(0)
        screen_height = user32.GetSystemMetrics(1)
        if right <= 0 or bottom <= 0 or left >= screen_width or top >= screen_height:
            return
        candidates.append((hwnd, rect, is_root))

    @enum_proc
    def enum_top(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _get_text(hwnd).lower()
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, len(cls_buf))
        class_name = cls_buf.value.lower()
        # Studio window titles usually end with "- Roblox Studio". Accept the
        # foreground window as a secondary candidate when title formats differ.
        if "roblox studio" in title or "robloxstudiobeta" in class_name or hwnd == foreground:
            roots.append(hwnd)
        return True

    user32.EnumWindows(enum_top, 0)
    if not roots:
        raise RuntimeError("Could not find a visible Roblox Studio window. Bring Studio to the foreground and retry.")

    # Prefer the Studio window currently in the foreground, then other Studio windows.
    roots.sort(key=lambda h: 0 if h == foreground else 1)
    for root in roots:
        add_window(root, True)

        @enum_child_proc
        def enum_child(child, _lparam):
            add_window(child, False)
            return True

        user32.EnumChildWindows(root, enum_child, 0)

    if not candidates:
        raise RuntimeError("Could not find a visible 3D viewport window inside Roblox Studio.")

    scored: list[tuple[float, tuple[int, int, int, int], int, str]] = []
    for hwnd, rect, is_root in candidates:
        left, top, right, bottom = rect
        width, height = right - left, bottom - top
        aspect = width / max(1, height)
        area = width * height
        aspect_error = abs(math.log(max(0.001, aspect / target_aspect)))
        area_error = abs(math.log(max(1, area) / max(1, target_area)))
        # The render child typically matches Camera.ViewportSize more closely than
        # the full Studio client; avoid choosing the full app window when a child fits.
        score = aspect_error * 5.0 + area_error * 1.25 + (0.18 if is_root else 0.0)
        scored.append((score, rect, hwnd, _get_text(hwnd)))

    scored.sort(key=lambda item: item[0])
    score, rect, hwnd, title = scored[0]
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    actual_aspect = width / max(1, height)
    if score > 2.2 or abs(math.log(max(0.001, actual_aspect / target_aspect))) > 0.38:
        raise RuntimeError(
            "Could not confidently identify the 3D viewport window. "
            f"Best candidate was {width}x{height} (expected about {expected_width}x{expected_height}). "
            "Maximize Studio, bring the place viewport to the front, and retry."
        )
    expected_text = f"{expected_width}x{expected_height}"
    print(
        f"[OxRender] DPI mode: {DPI_AWARENESS_MODE}. "
        f"Camera viewport requested {expected_text}; using desktop capture rectangle "
        f"{width}x{height} at ({left}, {top}) (score {score:.3f})."
    )
    return rect


def _capture_screen_rect_rgba(rect: tuple[int, int, int, int], output_width: int, output_height: int) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("OS viewport capture requires Windows.")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.GetDC.restype = ctypes.c_void_p
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.StretchBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.SetStretchBltMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdi32.SetBrushOrgEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(POINT)]

    left, top, right, bottom = rect
    source_width = right - left
    source_height = bottom - top
    if source_width < 1 or source_height < 1:
        raise RuntimeError("The detected 3D viewport has invalid dimensions.")

    # Center-crop the actual viewport to match the selected render aspect ratio.
    target_aspect = output_width / output_height
    source_aspect = source_width / source_height
    crop_width, crop_height = source_width, source_height
    if source_aspect > target_aspect:
        crop_width = max(1, int(source_height * target_aspect))
    else:
        crop_height = max(1, int(source_width / target_aspect))
    crop_left = left + (source_width - crop_width) // 2
    crop_top = top + (source_height - crop_height) // 2

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise RuntimeError("Windows could not open the desktop screen DC.")
    memory_dc = None
    bitmap = None
    old_bitmap = None
    bits = ctypes.c_void_p()
    try:
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            raise RuntimeError("Windows could not create a capture surface.")
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = output_width
        info.bmiHeader.biHeight = -output_height  # top-down image
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        info.bmiHeader.biSizeImage = output_width * output_height * 4
        bitmap = gdi32.CreateDIBSection(memory_dc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            raise RuntimeError("Windows could not allocate the screenshot pixel buffer.")
        old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
        gdi32.SetStretchBltMode(memory_dc, HALFTONE)
        gdi32.SetBrushOrgEx(memory_dc, 0, 0, None)
        copied = gdi32.StretchBlt(
            memory_dc, 0, 0, output_width, output_height,
            screen_dc, crop_left, crop_top, crop_width, crop_height,
            SRCCOPY | CAPTUREBLT,
        )
        if not copied:
            raise ctypes.WinError(ctypes.get_last_error())
        bgra = ctypes.string_at(bits, output_width * output_height * 4)
        rgba = bytearray(len(bgra))
        rgba[0::4] = bgra[2::4]
        rgba[1::4] = bgra[1::4]
        rgba[2::4] = bgra[0::4]
        rgba[3::4] = b"\xff" * (output_width * output_height)
        return bytes(rgba)
    finally:
        if old_bitmap and memory_dc:
            gdi32.SelectObject(memory_dc, old_bitmap)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)


def remove_green_screen_rgba(source: bytes, width: int, height: int) -> bytes:
    """Create an alpha matte from OxRender's controlled neon-green background.

    Pixels in the green-dominant region connected to the image border are treated
    as backdrop. A green shadow catcher darkens the key, so alpha is estimated from
    the key's green dominance and its original green contribution is un-matted to
    black/foreground RGB. One-pixel red/blue-biased edge pixels receive a soft matte
    to reduce key-colored fringes. This is a chroma-key technique, not AI segmentation.
    """
    expected = width * height * 4
    if width < 1 or height < 1 or len(source) != expected:
        raise ValueError(f"Invalid RGBA source for green-screen removal: {width}x{height}, {len(source)} bytes")

    count = width * height
    key_mask = bytearray(count)
    for index in range(count):
        offset = index * 4
        red, green, blue = source[offset], source[offset + 1], source[offset + 2]
        dominant = green - max(red, blue)
        if green >= 34 and dominant >= 18:
            key_mask[index] = 1

    # Flood-fill key-colored regions from all four edges. This helps protect a
    # similarly green model surface that is completely surrounded by non-key pixels.
    reachable = bytearray(count)
    queue: deque[int] = deque()

    def add_seed(index: int) -> None:
        if key_mask[index] and not reachable[index]:
            reachable[index] = 1
            queue.append(index)

    for x in range(width):
        add_seed(x)
        add_seed((height - 1) * width + x)
    for y in range(height):
        add_seed(y * width)
        add_seed(y * width + width - 1)

    while queue:
        index = queue.popleft()
        x = index % width
        if x > 0:
            neighbor = index - 1
            if key_mask[neighbor] and not reachable[neighbor]:
                reachable[neighbor] = 1
                queue.append(neighbor)
        if x + 1 < width:
            neighbor = index + 1
            if key_mask[neighbor] and not reachable[neighbor]:
                reachable[neighbor] = 1
                queue.append(neighbor)
        if index >= width:
            neighbor = index - width
            if key_mask[neighbor] and not reachable[neighbor]:
                reachable[neighbor] = 1
                queue.append(neighbor)
        if index + width < count:
            neighbor = index + width
            if key_mask[neighbor] and not reachable[neighbor]:
                reachable[neighbor] = 1
                queue.append(neighbor)

    # Find a narrow border ring so antialiased red/blue object edges can be
    # unmatted even when they are not themselves green-dominant enough for flood fill.
    edge_ring = bytearray(count)
    for index, is_outside in enumerate(reachable):
        if not is_outside:
            continue
        x = index % width
        if x > 0 and not reachable[index - 1]:
            edge_ring[index - 1] = 1
        if x + 1 < width and not reachable[index + 1]:
            edge_ring[index + 1] = 1
        if index >= width and not reachable[index - width]:
            edge_ring[index - width] = 1
        if index + width < count and not reachable[index + width]:
            edge_ring[index + width] = 1

    output = bytearray(source)
    for index in range(count):
        offset = index * 4
        red, green, blue, old_alpha = source[offset], source[offset + 1], source[offset + 2], source[offset + 3]
        max_rb = max(red, blue)
        dominant = green - max_rb
        process_as_key = bool(reachable[index])
        # Also clear the exact key within enclosed cutouts (for example, the hole
        # inside a ring or handle) even if flood fill cannot reach it from the frame.
        if not process_as_key and green >= 220 and dominant >= 220:
            process_as_key = True

        if process_as_key:
            # Most green background and shadow pixels have very little R/B. If the
            # green component is much greater than both, green dominance estimates
            # the screen contribution; otherwise use the classic RGB-distance matte.
            if green > max_rb * 2.25:
                alpha = 1.0 - max(0, dominant) / 255.0
            else:
                alpha = max(red, blue, 255 - green) / 255.0
        elif edge_ring[index] and max_rb > 0:
            min_rb = min(red, blue)
            # Restrict the edge fringe pass to red/blue-biased colors, avoiding
            # accidental translucency on solid grey/black model edges.
            if min_rb < max_rb * 0.72 and green >= max_rb - 16:
                alpha = max(red, blue, 255 - green) / 255.0
            else:
                continue
        else:
            continue

        alpha = max(0.0, min(1.0, alpha))
        if alpha <= 0.012:
            output[offset] = 0
            output[offset + 1] = 0
            output[offset + 2] = 0
            output[offset + 3] = 0
            continue

        # Un-premultiply the green-screen contribution to avoid green fringes.
        output[offset] = max(0, min(255, int(round(red / alpha))))
        output[offset + 1] = max(0, min(255, int(round((green - (1.0 - alpha) * 255.0) / alpha))))
        output[offset + 2] = max(0, min(255, int(round(blue / alpha))))
        output[offset + 3] = min(255, max(0, int(round(alpha * 255.0))))

    return bytes(output)


def _resize_rgba_nearest(source: bytes, source_width: int, source_height: int, target_width: int, target_height: int) -> bytes:
    if (source_width, source_height) == (target_width, target_height):
        return source
    result = bytearray(target_width * target_height * 4)
    for y in range(target_height):
        source_y = min(source_height - 1, int((y + 0.5) * source_height / target_height))
        source_row = source_y * source_width * 4
        target_row = y * target_width * 4
        for x in range(target_width):
            source_x = min(source_width - 1, int((x + 0.5) * source_width / target_width))
            source_offset = source_row + source_x * 4
            target_offset = target_row + x * 4
            result[target_offset:target_offset + 4] = source[source_offset:source_offset + 4]
    return bytes(result)


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def encode_rgba_png(width: int, height: int, rgba: bytes) -> bytes:
    if width < 1 or height < 1 or width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ValueError(f"Invalid image dimensions: {width}x{height}")
    expected = width * height * 4
    if len(rgba) != expected:
        raise ValueError(f"RGBA byte count mismatch: got {len(rgba)}, expected {expected}")

    stride = width * 4
    filtered = bytearray((stride + 1) * height)
    for y in range(height):
        destination = y * (stride + 1)
        filtered[destination] = 0  # PNG filter: None
        start = y * stride
        filtered[destination + 1 : destination + 1 + stride] = rgba[start : start + stride]

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # RGBA, 8-bit
    compressed = zlib.compress(bytes(filtered), level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", compressed)
        + png_chunk(b"IEND", b"")
    )


_PLACEHOLDER_API_KEYS = {
    "",
    "your_api_key",
    "changeme",
    "paste_your_photoroom_api_key_here",
    "paste_your_clipdrop_api_key_here",
    "paste_your_removebg_api_key_here",
    "paste_your_remove_bg_api_key_here",
}
_PROVIDER_KEY_FIELDS = {
    "photoroom": "photoroom_api_key",
    "clipdrop": "clipdrop_api_key",
    "removebg": "removebg_api_key",
}

def _provider_has_api_key(provider: str, config: dict) -> bool:
    field = _PROVIDER_KEY_FIELDS.get(provider)
    if not field:
        return False
    value = str(config.get(field, "")).strip()
    return value.lower() not in _PLACEHOLDER_API_KEYS

def resolve_background_removal_provider(config: dict, *, log_fallback: bool = False) -> str:
    """Use the selected provider, falling back to another provider with a real key.

    This prevents a stale PhotoRoom selection from blocking remove.bg when the user
    has already configured a valid remove.bg key in the same config file.
    """
    requested = str(config.get("background_removal_provider", "removebg")).strip().lower()
    if requested not in _PROVIDER_KEY_FIELDS:
        raise RuntimeError(
            f"Unknown background_removal_provider '{requested}'. Use 'removebg', 'photoroom', or 'clipdrop' in oxrender_config.json."
        )
    if _provider_has_api_key(requested, config):
        return requested

    # Prefer remove.bg if the selected provider has no key, then try the other services.
    for candidate in ("removebg", "photoroom", "clipdrop"):
        if candidate != requested and _provider_has_api_key(candidate, config):
            if log_fallback:
                print(
                    f"[OxRender] Config selects '{requested}', but no usable {requested} API key is set. "
                    f"Using '{candidate}' because its API key is configured."
                )
            return candidate
    return requested


def load_config() -> dict:
    """Load local API configuration. Keys are kept out of the Luau plugin."""
    config: dict = {}
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config.update(loaded)
        except Exception as exc:
            raise RuntimeError(f"Could not read {CONFIG_PATH.name}: {exc}") from exc

    # Environment variables override the local config file.
    provider = os.environ.get("OXRENDER_BG_REMOVAL_PROVIDER")
    photoroom_key = os.environ.get("OXRENDER_PHOTOROOM_API_KEY")
    clipdrop_key = os.environ.get("OXRENDER_CLIPDROP_API_KEY")
    removebg_key = os.environ.get("OXRENDER_REMOVEBG_API_KEY")
    if provider:
        config["background_removal_provider"] = provider
    if photoroom_key:
        config["photoroom_api_key"] = photoroom_key
    if clipdrop_key:
        config["clipdrop_api_key"] = clipdrop_key
    if removebg_key:
        config["removebg_api_key"] = removebg_key
    return config


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode("utf-8")


def _post_background_removal_api(provider: str, source_png: bytes, config: dict) -> bytes:
    """Send the captured screenshot to a real background-removal API."""
    boundary = "----OxRenderBoundary" + uuid.uuid4().hex
    body = bytearray()

    if provider == "photoroom":
        api_key = str(config.get("photoroom_api_key", "")).strip()
        if not api_key or api_key.lower() in {"paste_your_photoroom_api_key_here", "your_api_key", "changeme"}:
            raise RuntimeError(
                "The selected provider is PhotoRoom, but no PhotoRoom API key is configured. "
                "To use remove.bg instead, set background_removal_provider to \"removebg\" and enter your key in "
                "oxrender_config.json as removebg_api_key (https://www.remove.bg/api). "
                "Otherwise, add a PhotoRoom key as photoroom_api_key."
            )
        url = "https://sdk.photoroom.com/v1/segment"
        for field_name, field_value in (
            ("format", "png"),
            ("size", "full"),
            ("crop", "false"),
            ("despill", "true" if bool(config.get("photoroom_despill", True)) else "false"),
        ):
            body.extend(_multipart_field(boundary, field_name, field_value))
        api_key_header = "x-api-key"
    elif provider == "clipdrop":
        api_key = str(config.get("clipdrop_api_key", "")).strip()
        if not api_key or api_key.lower() in {"paste_your_clipdrop_api_key_here", "your_api_key", "changeme"}:
            raise RuntimeError(
                "Transparent mode is configured for Clipdrop, but no API key is set. "
                "Create a key from https://clipdrop.co/apis/docs/remove-background and put it in "
                "oxrender_config.json as clipdrop_api_key. Check Clipdrop's current pricing and quota before use."
            )
        url = "https://clipdrop-api.co/remove-background/v1"
        body.extend(_multipart_field(boundary, "transparency_handling", "discard_alpha_layer"))
        api_key_header = "x-api-key"
    elif provider == "removebg":
        api_key = str(config.get("removebg_api_key", "")).strip()
        if not api_key or api_key.lower() in {"paste_your_removebg_api_key_here", "paste_your_remove_bg_api_key_here", "your_api_key", "changeme"}:
            raise RuntimeError(
                "Transparent mode is configured for remove.bg, but no API key is set. "
                "Get a key from https://www.remove.bg/api, then put it in oxrender_config.json as removebg_api_key. "
                "The default removebg_size is 'preview' for low-resolution testing; check your account's free quota and pricing."
            )
        size_value = str(config.get("removebg_size", "preview")).strip().lower()
        valid_sizes = {"preview", "full", "auto", "50mp"}
        if size_value not in valid_sizes:
            raise RuntimeError(
                f"Invalid removebg_size '{size_value}'. Use 'preview' for low-resolution testing, or 'auto', 'full', or '50mp' if available to your account."
            )
        if size_value == "50mp":
            size_value = "50MP"
        url = "https://api.remove.bg/v1.0/removebg"
        body.extend(_multipart_field(boundary, "size", size_value))
        api_key_header = "X-Api-Key"
    else:
        raise RuntimeError(
            f"Unknown background_removal_provider '{provider}'. Use 'photoroom', 'clipdrop', or 'removebg' in oxrender_config.json."
        )

    body.extend(
        f"--{boundary}\r\n".encode("ascii")
        + b'Content-Disposition: form-data; name="image_file"; filename="oxrender-capture.png"\r\n'
        + b"Content-Type: image/png\r\n\r\n"
        + source_png
        + b"\r\n"
        + f"--{boundary}--\r\n".encode("ascii")
    )
    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={
            api_key_header: api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "image/png",
            "User-Agent": "OxRender-Local-Receiver/1.2",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            result = response.read(MAX_BODY_BYTES + 1)
            if len(result) > MAX_BODY_BYTES:
                raise RuntimeError("Background-removal service returned an image larger than the local 64 MB limit.")
            content_type = response.headers.get("Content-Type", "").lower()
            if "image/png" not in content_type and not result.startswith(b"\x89PNG\r\n\x1a\n"):
                message = result[:1000].decode("utf-8", errors="replace")
                raise RuntimeError(f"{provider.title()} did not return a PNG image (HTTP {response.status}): {message}")
            if not result.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError(f"{provider.title()} response was labelled PNG but had an invalid PNG signature.")
            return result
    except urllib.error.HTTPError as exc:
        details = exc.read(4000).decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            hint = " Check that the API key is correct and active."
        elif exc.code == 402:
            hint = " The API account may have no remaining credits or billing may be required."
        elif exc.code == 413:
            hint = " The screenshot may exceed the provider's upload or resolution limit."
        else:
            hint = ""
        raise RuntimeError(f"{provider.title()} background removal failed with HTTP {exc.code}: {details}{hint}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach {provider.title()} background-removal service: {exc.reason}. Check your internet connection."
        ) from exc


def decode_png_rgba(png_data: bytes) -> tuple[int, int, bytes]:
    """Decode common 8-bit PNG color formats using only the Python standard library."""
    signature = b"\x89PNG\r\n\x1a\n"
    if not png_data.startswith(signature):
        raise ValueError("Background-removal service returned an invalid PNG signature.")

    offset = len(signature)
    width = height = bit_depth = color_type = interlace = None
    idat_parts: list[bytes] = []
    palette: bytes | None = None
    transparency: bytes | None = None
    while offset + 12 <= len(png_data):
        chunk_length = struct.unpack_from(">I", png_data, offset)[0]
        chunk_type = png_data[offset + 4 : offset + 8]
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_length
        if chunk_end + 4 > len(png_data):
            raise ValueError("PNG contains a truncated chunk.")
        chunk = png_data[chunk_start:chunk_end]
        if chunk_type == b"IHDR":
            if len(chunk) != 13:
                raise ValueError("PNG has an invalid IHDR chunk.")
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", chunk)
            if compression != 0 or filter_method != 0:
                raise ValueError("PNG uses an unsupported compression or filter method.")
        elif chunk_type == b"IDAT":
            idat_parts.append(chunk)
        elif chunk_type == b"PLTE":
            palette = chunk
        elif chunk_type == b"tRNS":
            transparency = chunk
        elif chunk_type == b"IEND":
            break
        offset = chunk_end + 4

    if width is None or height is None or bit_depth is None or color_type is None or interlace is None:
        raise ValueError("PNG is missing its IHDR chunk.")
    if width < 1 or height < 1 or width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ValueError(f"Background-removal output dimensions are unsupported: {width}x{height}.")
    if bit_depth != 8 or interlace != 0:
        raise ValueError(f"Background-removal service returned a PNG format this helper cannot decode (bit depth {bit_depth}, interlace {interlace}).")

    channels_by_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    channels = channels_by_type.get(color_type)
    if channels is None:
        raise ValueError(f"Background-removal service returned unsupported PNG color type {color_type}.")
    if color_type == 3 and (not palette or len(palette) % 3 != 0):
        raise ValueError("Indexed PNG has an invalid palette.")

    compressed = zlib.decompress(b"".join(idat_parts))
    row_bytes = width * channels
    expected = (row_bytes + 1) * height
    if len(compressed) != expected:
        raise ValueError(f"PNG decoded data length mismatch: got {len(compressed)}, expected {expected}.")

    recon = bytearray(row_bytes * height)
    cursor = 0
    for y in range(height):
        filter_type = compressed[cursor]
        cursor += 1
        row_start = y * row_bytes
        for x in range(row_bytes):
            value = compressed[cursor + x]
            left = recon[row_start + x - channels] if x >= channels else 0
            above = recon[row_start + x - row_bytes] if y > 0 else 0
            upper_left = recon[row_start + x - row_bytes - channels] if y > 0 and x >= channels else 0
            if filter_type == 0:
                pass
            elif filter_type == 1:
                value = (value + left) & 0xFF
            elif filter_type == 2:
                value = (value + above) & 0xFF
            elif filter_type == 3:
                value = (value + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                p = left + above - upper_left
                pa = abs(p - left)
                pb = abs(p - above)
                pc = abs(p - upper_left)
                predictor = left if pa <= pb and pa <= pc else above if pb <= pc else upper_left
                value = (value + predictor) & 0xFF
            else:
                raise ValueError(f"PNG uses unsupported row filter {filter_type}.")
            recon[row_start + x] = value
        cursor += row_bytes

    rgba = bytearray(width * height * 4)
    palette_entries = len(palette) // 3 if palette else 0
    for pixel_index in range(width * height):
        si = pixel_index * channels
        di = pixel_index * 4
        if color_type == 6:
            rgba[di:di + 4] = recon[si:si + 4]
        elif color_type == 2:
            rgba[di:di + 3] = recon[si:si + 3]
            rgba[di + 3] = 255
        elif color_type == 0:
            grey = recon[si]
            rgba[di:di + 3] = bytes((grey, grey, grey))
            rgba[di + 3] = 255
        elif color_type == 4:
            grey, alpha = recon[si], recon[si + 1]
            rgba[di:di + 4] = bytes((grey, grey, grey, alpha))
        elif color_type == 3:
            palette_index = recon[si]
            if palette_index >= palette_entries:
                raise ValueError("Indexed PNG contains a palette index outside the palette.")
            base = palette_index * 3
            rgba[di:di + 3] = palette[base:base + 3]
            rgba[di + 3] = transparency[palette_index] if transparency and palette_index < len(transparency) else 255
    return width, height, bytes(rgba)





def sanitize_filename(value: str) -> str:
    value = os.path.basename(value.strip())
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("._-")
    return (value[:64] or "OxRender")


class Handler(BaseHTTPRequestHandler):
    server_version = "OxRenderReceiver/1.0"

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print("[OxRender] " + (fmt % args))

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True, "service": "OxRender PNG receiver"})
        else:
            self._send_json(404, {"ok": False, "error": "Not found"})

    def _send_rgba_preview(self, rgba: bytes, width: int, height: int, destination: Path, provider_label: str) -> None:
        # Return up to 1024px so the in-Studio preview and Decal image retain more detail;
        # the full-resolution transparent PNG is still saved separately.
        preview_width = min(width, 1024)
        preview_height = max(1, int(round(height * preview_width / width)))
        if preview_height > 1024:
            preview_height = 1024
            preview_width = max(1, int(round(width * preview_height / height)))
        rgba_preview = _resize_rgba_nearest(rgba, width, height, preview_width, preview_height)

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(rgba_preview)))
        self.send_header("X-OxRender-Width", str(preview_width))
        self.send_header("X-OxRender-Height", str(preview_height))
        self.send_header("X-OxRender-Full-Width", str(width))
        self.send_header("X-OxRender-Full-Height", str(height))
        self.send_header("X-OxRender-Saved-Filename", destination.name)
        self.send_header("X-OxRender-Saved-Path", quote(str(destination), safe=""))
        self.send_header("X-OxRender-Provider", quote(provider_label, safe=""))
        self.end_headers()
        self.wfile.write(rgba_preview)

    def _remove_background_and_save(self, source_rgba: bytes, width: int, height: int, base_name: str) -> None:
        """Run the configured provider, save the full-resolution transparent PNG, and return preview RGBA."""
        global LAST_CAPTURE_RAW_PATH, LAST_CAPTURE_RAW_NAME
        processed_rgba, png_data, out_width, out_height, provider_label = remove_background_with_external_service(
            source_rgba, width, height
        )
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = OUTPUT_DIR / f"{sanitize_filename(base_name)}_transparent_{stamp}.png"
        destination.write_bytes(png_data)
        LAST_CAPTURE_RAW_PATH = destination
        LAST_CAPTURE_RAW_NAME = sanitize_filename(base_name)
        self._send_rgba_preview(processed_rgba, out_width, out_height, destination, provider_label)
        print(
            f"[OxRender] Background removed with {provider_label}; transparent PNG saved to "
            f"{destination} ({out_width}x{out_height})."
        )

    def _handle_remove_bg_from_rgba(self) -> None:
        """Process RGBA8 pixels from StudioCaptureService that are held in plugin memory."""
        try:
            width = int(self.headers.get("X-OxRender-Width", "0"))
            height = int(self.headers.get("X-OxRender-Height", "0"))
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > MAX_BODY_BYTES:
                raise ValueError(f"Invalid image body length: {length}")
            if width < 1 or height < 1 or width > MAX_DIMENSION or height > MAX_DIMENSION:
                raise ValueError(f"Invalid image dimensions: {width}x{height}")
            expected = width * height * 4
            if length != expected:
                raise ValueError(f"RGBA byte count mismatch: request has {length} bytes, expected {expected}")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError(f"Incomplete image upload: received {len(raw)} of {length} bytes")
            base_name = sanitize_filename(self.headers.get("X-OxRender-Filename", "OxRender"))
            self._remove_background_and_save(raw, width, height, base_name)
        except Exception as exc:
            print(f"[OxRender] Remove BG failed: {exc}")
            self._send_json(400, {"ok": False, "error": str(exc)})

    def _handle_remove_bg_latest(self) -> None:
        """Process the latest full-resolution capture, recovering from a helper restart if needed."""
        try:
            source_path = LAST_CAPTURE_RAW_PATH
            source_name = LAST_CAPTURE_RAW_NAME

            # A helper restart clears module globals, but the capture file remains on
            # disk. Recover the most recent staged transparent-mode source first.
            if source_path is None or not source_path.is_file():
                # Search both locations and choose the newest source capture. A helper
                # restart clears the in-memory pointer, but must not make an existing
                # capture unusable. Exclude processed outputs to avoid repeatedly
                # sending an already-transparent image back through background removal.
                source_candidates: list[Path] = []
                if STAGING_DIR.exists():
                    source_candidates.extend(STAGING_DIR.glob("*_raw.png"))
                if OUTPUT_DIR.exists():
                    source_candidates.extend(
                        path for path in OUTPUT_DIR.glob("*.png")
                        if "_transparent_" not in path.stem.lower()
                    )
                source_candidates = [path for path in source_candidates if path.is_file()]
                if source_candidates:
                    source_path = max(source_candidates, key=lambda path: path.stat().st_mtime)

                if source_path is None or not source_path.is_file():
                    raise RuntimeError(
                        "No saved source capture was found. Click Capture Render and wait until the capture completes before using Remove BG. "
                        "The helper searched its in-memory capture and the OxRenderExports source folders."
                    )

                stem = source_path.stem
                if stem.endswith("_raw"):
                    stem = stem[:-4]
                name_match = re.match(r"^(.*)_\d{8}_\d{6}$", stem)
                source_name = name_match.group(1) if name_match else stem
                print(f"[OxRender] Recovered source capture for Remove BG from disk: {source_path}")

            png_data = source_path.read_bytes()
            width, height, rgba = decode_png_rgba(png_data)
            self._remove_background_and_save(rgba, width, height, source_name)
        except Exception as exc:
            print(f"[OxRender] Remove BG failed: {exc}")
            self._send_json(400, {"ok": False, "error": str(exc)})

    def _handle_viewport_geometry(self) -> None:
        """Report the real Studio viewport rectangle relative to the Studio client window."""
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > 64 * 1024:
                raise ValueError("Invalid viewport geometry request size.")
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            viewport_width = int(request.get("viewportWidth", 0))
            viewport_height = int(request.get("viewportHeight", 0))
            if not (1 <= viewport_width <= MAX_DIMENSION and 1 <= viewport_height <= MAX_DIMENSION):
                raise ValueError(f"Invalid viewport size: {viewport_width}x{viewport_height}")

            viewport_rect = _find_studio_viewport_rect(viewport_width, viewport_height)
            client_rect = _find_studio_root_client_rect()
            view_left, view_top, view_right, view_bottom = viewport_rect
            client_left, client_top, client_right, client_bottom = client_rect
            left = max(view_left, client_left)
            top = max(view_top, client_top)
            right = min(view_right, client_right)
            bottom = min(view_bottom, client_bottom)
            if right <= left or bottom <= top:
                raise RuntimeError("The detected 3D viewport is outside the Studio client area.")

            client_width = client_right - client_left
            client_height = client_bottom - client_top
            geometry = {
                "ok": True,
                "x": left - client_left,
                "y": top - client_top,
                "width": right - left,
                "height": bottom - top,
                "clientWidth": client_width,
                "clientHeight": client_height,
            }
            self._send_json(200, geometry)
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})

    def _handle_viewport_capture(self) -> None:
        global LAST_CAPTURE_RAW_PATH, LAST_CAPTURE_RAW_NAME
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > 64 * 1024:
                raise ValueError("Invalid viewport capture request size.")
            raw = self.rfile.read(length)
            request = json.loads(raw.decode("utf-8"))
            expected_token = request.pop("token", TOKEN)
            if expected_token != TOKEN:
                raise ValueError("Invalid local helper token in capture request.")
            viewport_width = int(request.get("viewportWidth", 0))
            viewport_height = int(request.get("viewportHeight", 0))
            output_width = int(request.get("outputWidth", 0))
            output_height = int(request.get("outputHeight", 0))
            transparent_background = bool(request.get("transparentBackground", False))
            base_name = sanitize_filename(str(request.get("filename", "OxRender")))
            for dimension, value in (("viewport width", viewport_width), ("viewport height", viewport_height), ("output width", output_width), ("output height", output_height)):
                if value < 1 or value > MAX_DIMENSION:
                    raise ValueError(f"Invalid {dimension}: {value}")

            rect = _find_studio_viewport_rect(viewport_width, viewport_height)
            captured_rgba = _capture_screen_rect_rgba(rect, output_width, output_height)
            png_data = encode_rgba_png(output_width, output_height, captured_rgba)

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if transparent_background:
                # Save the raw keyed source out of the visible exports folder. Remove BG
                # processes this after capture, so API failures never destroy the capture.
                STAGING_DIR.mkdir(parents=True, exist_ok=True)
                destination = STAGING_DIR / f"{base_name}_{stamp}_raw.png"
                mode_label = "raw chroma-key source staged; background removal is a separate step"
            else:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                destination = OUTPUT_DIR / f"{base_name}_{stamp}.png"
                mode_label = "opaque RGBA"

            destination.write_bytes(png_data)
            LAST_CAPTURE_RAW_PATH = destination
            LAST_CAPTURE_RAW_NAME = base_name

            preview_width = min(output_width, 512)
            preview_height = max(1, int(round(output_height * preview_width / output_width)))
            if preview_height > 512:
                preview_height = 512
                preview_width = max(1, int(round(output_width * preview_height / output_height)))
            rgba_preview = _resize_rgba_nearest(captured_rgba, output_width, output_height, preview_width, preview_height)

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(rgba_preview)))
            self.send_header("X-OxRender-Width", str(preview_width))
            self.send_header("X-OxRender-Height", str(preview_height))
            # Return the actual saved source path in both normal and transparent modes.
            # In transparent mode it points to the staged chroma-key PNG; the final alpha
            # PNG path is returned separately by the background-removal response.
            self.send_header("X-OxRender-Saved-Filename", destination.name)
            self.send_header("X-OxRender-Saved-Path", quote(str(destination), safe=""))
            self.send_header("X-OxRender-Capture-Mode", "transparent-source" if transparent_background else "final-png")
            self.end_headers()
            self.wfile.write(rgba_preview)
            if transparent_background:
                print(f"[OxRender] Captured source staged for Remove BG at {destination} ({output_width}x{output_height}).")
            else:
                print(f"[OxRender] Captured Studio viewport to {destination} ({output_width}x{output_height}, {mode_label}); sent {preview_width}x{preview_height} preview pixels.")
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        global LAST_CAPTURE_RAW_PATH, LAST_CAPTURE_RAW_NAME
        if self.headers.get("X-OxRender-Key", "") != TOKEN:
            self._send_json(403, {"ok": False, "error": "Invalid local helper token"})
            return
        if self.path == "/viewport-geometry":
            self._handle_viewport_geometry()
            return
        if self.path == "/capture-viewport":
            self._handle_viewport_capture()
            return
        if self.path == "/remove-bg":
            self._handle_remove_bg_from_rgba()
            return
        if self.path == "/remove-bg-latest":
            self._handle_remove_bg_latest()
            return
        if self.path != "/export":
            self._send_json(404, {"ok": False, "error": "Not found"})
            return
        try:
            width = int(self.headers.get("X-OxRender-Width", "0"))
            height = int(self.headers.get("X-OxRender-Height", "0"))
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > MAX_BODY_BYTES:
                raise ValueError(f"Invalid request body length: {length}")
            if width < 1 or height < 1 or width > MAX_DIMENSION or height > MAX_DIMENSION:
                raise ValueError(f"Invalid image dimensions: {width}x{height}")
            expected = width * height * 4
            if length != expected:
                raise ValueError(f"RGBA byte count mismatch: request has {length} bytes, expected {expected}")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError(f"Incomplete upload: received {len(raw)} of {length} bytes")

            base_name = sanitize_filename(self.headers.get("X-OxRender-Filename", "OxRender"))
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            destination = OUTPUT_DIR / f"{base_name}_{stamp}.png"
            png_data = encode_rgba_png(width, height, raw)
            destination.write_bytes(png_data)
            LAST_CAPTURE_RAW_PATH = destination
            LAST_CAPTURE_RAW_NAME = base_name
            self._send_json(200, {
                "ok": True,
                "path": str(destination),
                "width": width,
                "height": height,
                "bytes": len(png_data),
            })
            print(f"Saved {destination} ({width}x{height}, {len(png_data):,} bytes)")
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("OxRender local capture helper is running.")
    print(f"Windows DPI awareness: {DPI_AWARENESS_MODE}")
    print("Viewport capture uses physical desktop coordinates to avoid Windows display-scaling zoom/crop.")
    print(f"PNG export endpoint: http://{HOST}:{PORT}/export")
    print(f"Viewport guide geometry endpoint: http://{HOST}:{PORT}/viewport-geometry")
    print(f"Viewport fallback endpoint: http://{HOST}:{PORT}/capture-viewport")
    print(f"Remove BG endpoint (RGBA payload): http://{HOST}:{PORT}/remove-bg")
    print(f"Remove BG endpoint (latest helper capture): http://{HOST}:{PORT}/remove-bg-latest")
    print(f"PNG output folder: {OUTPUT_DIR}")
    print(f"Configuration file: {CONFIG_PATH}")
    try:
        config = load_config()
        provider = resolve_background_removal_provider(config, log_fallback=True)
        key_name = {
            "photoroom": "photoroom_api_key",
            "clipdrop": "clipdrop_api_key",
            "removebg": "removebg_api_key",
        }.get(provider, "")
        key_value = str(config.get(key_name, "")).strip() if key_name else ""
        placeholders = {
            "", "paste_your_photoroom_api_key_here", "paste_your_clipdrop_api_key_here",
            "paste_your_removebg_api_key_here", "paste_your_remove_bg_api_key_here",
            "your_api_key", "changeme",
        }
        key_present = bool(key_name) and key_value.lower() not in placeholders
        print(f"Transparent background provider: {provider} (API key {'configured' if key_present else 'NOT configured'})")
        if provider == "removebg":
            print(f"remove.bg output size: {config.get('removebg_size', 'preview')} (preview is intended for low-resolution testing)")
    except Exception as exc:
        print(f"Background-removal config warning: {exc}")
    print("Keep this terminal open while OxRender is open. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping OxRender PNG receiver...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
