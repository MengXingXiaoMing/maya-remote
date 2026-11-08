#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Photograph a hung Maya before anyone kills it.

A hung Maya is usually one modal dialog away from fine, so the recovery order is
*look, then kill*: raise the window, capture it (and optionally the whole
virtual screen), and report what windows exist.  The pictures land in the state
directory and their paths come back as JSON, so the caller can open them.

    python maya_snapshot.py --state-dir <dir> --pid 1234
    python maya_snapshot.py --state-dir <dir> --pid 1234 --full-screen --no-raise

Stdlib only (ctypes + zlib): no Pillow, no DSH, nothing to install.  Windows
only, like the rest of this skill.
"""

from __future__ import print_function

import argparse
import ctypes
import ctypes.wintypes as wintypes
import io
import json
import os
import struct
import sys
import time
import zlib

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
PW_RENDERFULLCONTENT = 0x00000002
SW_RESTORE = 9
SW_MINIMIZE = 6
SW_SHOW = 5
HWND_TOP = 0
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

RETENTION_FILE = "retention.json"
DEFAULT_KEEP_HOURS = 24.0


# --------------------------------------------------------------------------
# retention
#
# Unattended runs must not fill the disk, so diagnostics live for a rolling
# window (24h by default) and are pruned automatically before every capture.
# A confirmation — a human saying "I have seen these" — restarts the clock, so
# confirming never destroys evidence and nothing is deleted while somebody is
# still looking at it.  Nothing here ever asks for that confirmation: it is an
# optional, idempotent call, and without it the plain window applies.


def retention_path(shots_dir):
    return os.path.join(shots_dir, RETENTION_FILE)


def read_retention(shots_dir):
    try:
        with io.open(retention_path(shots_dir), "r", encoding="utf-8") as handle:
            record = json.loads(handle.read())
    except Exception:
        record = {}
    if not isinstance(record, dict):
        record = {}
    record.setdefault("keepHours", DEFAULT_KEEP_HOURS)
    record.setdefault("ackedAt", None)
    return record


def write_retention(shots_dir, record):
    if not os.path.isdir(shots_dir):
        os.makedirs(shots_dir)
    with io.open(retention_path(shots_dir), "w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, indent=2))
    return record


def prune_diagnostics(shots_dir, keep_hours=None, ack=None, now=None, apply=True):
    """Delete diagnostics outside the window; return what happened.

    A confirmation younger than the window suspends pruning entirely: the
    countdown restarts from that confirmation.
    """
    now = time.time() if now is None else now
    record = read_retention(shots_dir)
    hours = float(keep_hours if keep_hours is not None else record.get("keepHours") or DEFAULT_KEEP_HOURS)
    acked_at = ack if ack is not None else record.get("ackedAt")
    window = hours * 3600.0

    report = {
        "keepHours": hours,
        "ackedAt": acked_at,
        "pruned": [],
        "kept": 0,
        "suspendedUntil": None,
        "cutoff": None,
    }
    if not os.path.isdir(shots_dir):
        return report

    if acked_at and (now - float(acked_at)) < window:
        # Confirmed recently: the clock restarted, so nothing is old enough yet.
        report["suspendedUntil"] = float(acked_at) + window
        report["kept"] = len(
            [n for n in os.listdir(shots_dir) if n != RETENTION_FILE and os.path.isfile(os.path.join(shots_dir, n))]
        )
        return report

    cutoff = now - window
    report["cutoff"] = cutoff
    for name in sorted(os.listdir(shots_dir)):
        if name == RETENTION_FILE:
            continue
        path = os.path.join(shots_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                if apply:
                    os.remove(path)
                report["pruned"].append(name)
            else:
                report["kept"] += 1
        except OSError:
            continue
    return report


def acknowledge(shots_dir, keep_hours=None, now=None):
    """The human confirmed they have seen the diagnostics: restart the clock."""
    now = time.time() if now is None else now
    record = read_retention(shots_dir)
    if keep_hours is not None:
        record["keepHours"] = float(keep_hours)
    record["ackedAt"] = now
    write_retention(shots_dir, record)
    return record


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


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def write_png(path, width, height, bgra):
    """BGRA bytes, top-down, straight into a PNG (stdlib zlib)."""
    rows = []
    stride = width * 4
    for y in range(height):
        line = bgra[y * stride : (y + 1) * stride]
        rgb = bytearray(width * 3)
        for x in range(width):
            b, g, r = line[x * 4], line[x * 4 + 1], line[x * 4 + 2]
            rgb[x * 3] = r
            rgb[x * 3 + 1] = g
            rgb[x * 3 + 2] = b
        rows.append(bytes(rgb))
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as handle:
        handle.write(png)
    return len(png)


def grab(draw, width, height):
    """Run `draw(memdc)` onto a fresh 32-bit bitmap and return its BGRA bytes."""
    screen_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)
    try:
        draw(mem_dc)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # negative: top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS)
        return buffer.raw
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)


def capture_full_screen():
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    data = grab(lambda mem: gdi32.BitBlt(mem, 0, 0, width, height, user32.GetDC(0), x, y, SRCCOPY), width, height)
    return width, height, data


def window_rect(hwnd):
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def capture_window(hwnd):
    left, top, width, height = window_rect(hwnd)
    if width <= 0 or height <= 0:
        return None
    data = grab(lambda mem: user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT), width, height)
    return width, height, data, (left, top)


def window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def windows_of_process(pid):
    """Visible top-level windows of one process, largest first."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            left, top, width, height = window_rect(hwnd)
            if width > 0 and height > 0:
                found.append(
                    {
                        "hwnd": int(hwnd),
                        "title": window_title(hwnd),
                        "rect": [left, top, width, height],
                        "area": width * height,
                    }
                )
        return True

    user32.EnumWindows(visit, 0)
    found.sort(key=lambda row: row["area"], reverse=True)
    return found


def foreground_window():
    hwnd = user32.GetForegroundWindow()
    owner = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    return {"hwnd": int(hwnd), "title": window_title(hwnd), "pid": int(owner.value)}


def raise_window(hwnd):
    """Best-effort foreground: Windows only grants it under conditions, so the
    thread-input attach trick is tried, then a minimize/restore cycle (which makes
    this window the most recent user-facing candidate), and the result is
    verified rather than assumed."""
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    current_thread = kernel32.GetCurrentThreadId()
    attached = False
    try:
        if target_thread != current_thread:
            attached = bool(user32.AttachThreadInput(current_thread, target_thread, True))
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, target_thread, False)
    time.sleep(0.6)
    if user32.GetForegroundWindow() == hwnd:
        return True

    user32.ShowWindow(hwnd, SW_MINIMIZE)
    time.sleep(0.3)
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.3)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    time.sleep(0.6)  # let the compositor repaint before the picture
    return user32.GetForegroundWindow() == hwnd


def diagnose(args):
    user32.SetProcessDPIAware()
    state_dir = os.path.abspath(args.state_dir)
    shots_dir = os.path.join(state_dir, "diagnostics")
    if not os.path.isdir(shots_dir):
        os.makedirs(shots_dir)

    keep_hours = getattr(args, "keep_hours", None)
    if keep_hours is not None:
        record = read_retention(shots_dir)
        record["keepHours"] = float(keep_hours)
        write_retention(shots_dir, record)
    if getattr(args, "ack", False):
        acknowledge(shots_dir, keep_hours)
    retention = prune_diagnostics(
        shots_dir, keep_hours=keep_hours, apply=not getattr(args, "no_prune", False)
    )

    if getattr(args, "report_only", False):
        entries = [
            {"name": name, "bytes": os.path.getsize(os.path.join(shots_dir, name))}
            for name in sorted(os.listdir(shots_dir))
            if os.path.isfile(os.path.join(shots_dir, name))
        ]
        return {
            "ok": True,
            "reportOnly": True,
            "stateDir": state_dir,
            "diagnosticsDir": shots_dir,
            "fileCount": len(entries),
            "totalBytes": sum(entry["bytes"] for entry in entries),
            "files": entries[:60],
            "retention": retention,
        }

    stamp = time.strftime("%Y%m%d-%H%M%S")
    pid = args.pid
    windows = windows_of_process(pid) if pid else []
    report = {
        "at": time.time(),
        "pid": pid,
        "stamp": stamp,
        "windows": windows,
        "foregroundBefore": foreground_window(),
        "raised": False,
        "shots": [],
        "notes": [],
        "retention": retention,
    }

    if not windows:
        report["notes"].append("no visible window for this pid — it is gone, or it runs without a UI")

    if args.raise_window and windows:
        report["raised"] = raise_window(windows[0]["hwnd"])
        if not report["raised"]:
            report["notes"].append("could not take the foreground (Windows refused); the capture still ran")

    if windows and not args.screen_only:
        # Every visible window, not just the biggest: the thing blocking Maya is
        # usually a modal dialog, which is its own top-level window.
        for index, window in enumerate(windows[: args.max_windows]):
            shot = capture_window(window["hwnd"])
            if not shot:
                report["notes"].append("window %r had no capturable area" % window["title"])
                continue
            width, height, data, position = shot
            suffix = "window" if index == 0 else "window%d" % (index + 1)
            path = os.path.join(shots_dir, "%s-%s.png" % (stamp, suffix))
            size = write_png(path, width, height, data)
            report["shots"].append(
                {
                    "kind": suffix,
                    "path": path,
                    "width": width,
                    "height": height,
                    "bytes": size,
                    "title": window["title"],
                    "at": list(position),
                }
            )
        if len(windows) > args.max_windows:
            report["notes"].append(
                "%d more window(s) not captured (raise --max-windows to include them)" % (len(windows) - args.max_windows)
            )

    if args.full_screen:
        width, height, data = capture_full_screen()
        path = os.path.join(shots_dir, "%s-screen.png" % stamp)
        size = write_png(path, width, height, data)
        report["shots"].append({"kind": "screen", "path": path, "width": width, "height": height, "bytes": size})

    report["foregroundAfter"] = foreground_window()
    report["ok"] = bool(report["shots"]) or not windows

    summary_path = os.path.join(shots_dir, "%s.json" % stamp)
    with io.open(summary_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2))
    report["summaryPath"] = summary_path
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Capture a hung Maya before killing it.")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--pid", type=int, default=0, help="Maya pid (from session.json)")
    parser.add_argument("--full-screen", action="store_true", help="also capture the whole virtual screen")
    parser.add_argument("--screen-only", action="store_true", help="skip the per-window capture")
    parser.add_argument("--no-raise", dest="raise_window", action="store_false", help="capture without stealing focus")
    parser.add_argument("--raise", dest="raise_window", action="store_true", help="bring the window to the front first")
    parser.add_argument("--max-windows", type=int, default=4, help="how many of the process's windows to capture")
    parser.add_argument("--keep-hours", type=float, default=None, help="retention window for diagnostics (persisted)")
    parser.add_argument("--ack", action="store_true", help="the human has seen the diagnostics: restart the clock")
    parser.add_argument("--no-prune", action="store_true", help="skip the prune that normally precedes a capture")
    parser.add_argument("--report-only", action="store_true", help="no capture: just report retention state")
    parser.set_defaults(raise_window=True)
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    report = diagnose(args)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        sys.stdout.write(json.dumps(report, ensure_ascii=True, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
