#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Look at Maya's windows, then click the dialog that only looks like a hang.

A hung Maya is often just a modal dialog waiting for a click: a plugin security
warning, a missing-file prompt, a licensing notice.  Killing Maya there throws
away the session and the reproduction, so the recovery order is:

    diagnose  (maya_snapshot.py)  ->  targets  ->  grid  ->  click  ->  ping

This script owns the middle three steps.  It never guesses: every click has to
name a window (by pid + title, or an hwnd), and `--dry-run` reports what would
be clicked without moving the mouse.

    python maya_click.py targets --pid 1234
    python maya_click.py targets --pid 1234 --title "Security Warning"
    python maya_click.py grid    --pid 1234 --title "Security Warning" --step 50
    python maya_click.py click   --pid 1234 --title "Security Warning" --text Allow
    python maya_click.py click   --pid 1234 --title "Security Warning" --at 60,410
    python maya_click.py click   --pid 1234 --hwnd 265520 --screen-at 1700,1000 --dry-run

Two kinds of dialogs, two mechanisms:

* classic Win32 dialogs expose real child controls, so `--text Allow` finds the
  button's hwnd and sends BM_CLICK — no mouse movement at all;
* Qt dialogs (Maya 2023+ draws its own) have no child hwnds, so the screenshot
  from `grid` is the map: read the pixel position and pass it with `--at`,
  which is window-relative (the grid's origin is the window's top-left).

Stdlib only (ctypes), Windows only, like the rest of this skill.
"""

from __future__ import print_function

import argparse
import ctypes
import ctypes.wintypes as wintypes
import io
import json
import os
import sys
import threading
import time

import maya_snapshot as snap

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

BM_CLICK = 0x00F5
SMTO_ABORTIFHUNG = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
GW_OWNER = 4

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


# --------------------------------------------------------------------------
# window / control discovery


def rect_of(hwnd):
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top]


def text_of(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def class_of(hwnd):
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def all_visible_windows():
    found = []

    @EnumWindowsProc
    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        left, top, width, height = rect_of(hwnd)
        if width <= 0 or height <= 0:
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        found.append({
            "hwnd": int(hwnd),
            "pid": int(owner.value),
            "title": text_of(hwnd),
            "class": class_of(hwnd),
            "rect": [left, top, width, height],
            "area": width * height,
        })
        return True

    user32.EnumWindows(visit, 0)
    found.sort(key=lambda row: row["area"], reverse=True)
    return found


def children_of(hwnd, limit=60):
    """Visible, enabled child controls — the ones a click could land on."""
    found = []

    @EnumWindowsProc
    def visit(child, _):
        if len(found) >= limit:
            return False
        if not user32.IsWindowVisible(child):
            return True
        left, top, width, height = rect_of(child)
        if width <= 0 or height <= 0:
            return True
        parent = rect_of(hwnd)
        found.append({
            "hwnd": int(child),
            "text": text_of(child),
            "class": class_of(child),
            "rect": [left, top, width, height],
            # window-relative centre: what `click --at` expects
            "at": [left - parent[0] + width // 2, top - parent[1] + height // 2],
            "enabled": bool(user32.IsWindowEnabled(child)),
        })
        return True

    user32.EnumChildWindows(hwnd, visit, 0)
    return found


def resolve_window(args):
    """Pick one window: explicit hwnd, else pid (+ title filter), largest first."""
    if args.hwnd:
        if not user32.IsWindow(int(args.hwnd)):
            return None, {"error": "hwnd_not_found", "hwnd": args.hwnd}
        return {
            "hwnd": int(args.hwnd),
            "pid": 0,
            "title": text_of(int(args.hwnd)),
            "class": class_of(int(args.hwnd)),
            "rect": rect_of(int(args.hwnd)),
        }, None

    if args.pid:
        candidates = [row for row in all_visible_windows() if row["pid"] == args.pid]
    else:
        candidates = all_visible_windows()

    if args.title:
        needle = args.title.lower()
        candidates = [row for row in candidates if needle in (row["title"] or "").lower()]

    if not candidates:
        return None, {
            "error": "window_not_found",
            "pid": args.pid,
            "title": args.title,
            "hint": "run `targets` first; titles must match a substring, case-insensitive",
        }
    return candidates[0], None


# --------------------------------------------------------------------------
# clicking


GA_ROOT = 2
SW_RESTORE = 9


def root_of(hwnd):
    return int(user32.GetAncestor(int(hwnd), GA_ROOT) or hwnd)


def window_at(x, y):
    point = wintypes.POINT(int(x), int(y))
    hwnd = user32.WindowFromPoint(point)
    return int(hwnd or 0)


def activate(window):
    """Best-effort foreground so a coordinate click lands on the dialog.

    SetForegroundWindow sends messages to the target thread, so on a truly
    stuck Maya it can never return.  It therefore runs on a daemon thread with
    a short join: if the window cannot be raised in time the caller continues
    and the point-on-target check still guards the actual click.
    """
    hwnd = int(window["hwnd"])
    state = {"done": False}

    def worker():
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        state["done"] = True

    thread = threading.Thread(target=worker)
    thread.daemon = True
    thread.start()
    thread.join(0.8)
    time.sleep(0.1)
    return bool(state["done"]) and user32.GetForegroundWindow() == hwnd


def click_screen(x, y, dry_run=False):
    x, y = int(round(x)), int(round(y))
    before = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(before))
    if dry_run:
        return {"x": x, "y": y, "movedFrom": [before.x, before.y], "dryRun": True}
    user32.SetCursorPos(x, y)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.05)
    after = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(after))
    return {"x": x, "y": y, "movedFrom": [before.x, before.y], "cursorAfter": [after.x, after.y], "dryRun": False}


def click_control(child, dry_run=False):
    """BM_CLICK on a real control; no pointer, no focus stealing.

    SendMessage would wait forever when Maya is really stuck — the exact case
    this tool exists for — so the click is sent with a short timeout and then
    posted instead of delivered synchronously.
    """
    if dry_run:
        return {"hwnd": child["hwnd"], "text": child["text"], "at": child["at"], "dryRun": True, "method": "BM_CLICK"}
    result = ctypes.c_ulong(0)
    delivered = user32.SendMessageTimeoutW(
        int(child["hwnd"]), BM_CLICK, 0, 0, SMTO_ABORTIFHUNG, 1500, ctypes.byref(result)
    )
    method = "BM_CLICK"
    if not delivered:
        user32.PostMessageW(int(child["hwnd"]), BM_CLICK, 0, 0)
        method = "BM_CLICK(posted)"
    return {"hwnd": child["hwnd"], "text": child["text"], "at": child["at"],
            "dryRun": False, "method": method}


def find_control(parent_hwnd, label):
    needle = (label or "").lower()
    children = [row for row in children_of(parent_hwnd, limit=200) if row["text"]]
    exact = [row for row in children if row["text"].strip().lower() == needle]
    partial = [row for row in children if needle in row["text"].strip().lower()]
    for group in (exact, partial):
        for row in group:
            if row["enabled"]:
                return row
    return (exact or partial or [None])[0]


# --------------------------------------------------------------------------
# grid: the screenshot that turns "where do I click" into numbers


def draw_grid(width, height, bgra, step):
    """Red lines every `step` px, brighter every 5th, into a BGRA buffer."""
    step = max(10, int(step))
    stride = width * 4

    def paint(x, y, strong):
        offset = y * stride + x * 4
        if strong:
            bgra[offset:offset + 4] = bytes((0, 0, 255, 255))
        else:
            bgra[offset:offset + 4] = bytes((80, 80, 220, 255))

    for x in range(0, width, step):
        strong = (x // step) % 5 == 0
        for y in range(height):
            paint(x, y, strong)
        if strong and x + 1 < width:
            for y in range(height):
                paint(x + 1, y, strong)
    for y in range(0, height, step):
        strong = (y // step) % 5 == 0
        for x in range(width):
            paint(x, y, strong)
        if strong and y + 1 < height:
            for x in range(width):
                paint(x, y + 1, strong)
    return bgra


# --------------------------------------------------------------------------
# commands


def cmd_targets(args):
    if args.hwnd:
        windows = [{
            "hwnd": int(args.hwnd),
            "title": text_of(int(args.hwnd)),
            "class": class_of(int(args.hwnd)),
            "rect": rect_of(int(args.hwnd)),
        }]
    else:
        windows = all_visible_windows()
        if args.pid:
            windows = [row for row in windows if row["pid"] == args.pid]
        if args.title:
            needle = args.title.lower()
            windows = [row for row in windows if needle in (row["title"] or "").lower()]

    windows = windows[: args.max_windows]
    for window in windows:
        window["children"] = children_of(window["hwnd"]) if args.children else []

    clickable = []
    for window in windows:
        for child in window["children"]:
            if child["enabled"] and child["text"].strip():
                clickable.append({
                    "title": window["title"],
                    "text": child["text"].strip(),
                    "class": child["class"],
                    "at": child["at"],
                })

    return {
        "ok": True,
        "pid": args.pid,
        "windows": windows,
        "clickable": clickable,
        "hint": (
            "clickable 里的条目可用 `click --text 文字` 直接点；"
            "如果 clickable 是空的（Qt 自绘弹窗），用 `grid` 截图读坐标，再 `click --at x,y`"
        ),
    }


def capture_rect(left, top, width, height):
    """Copy what is on screen for that rectangle.

    A screen BitBlt is used instead of PrintWindow on purpose: PrintWindow asks
    the target thread to redraw itself, so a genuinely stuck Maya would block
    the screenshot — and the screenshot is exactly what we need *before* calling
    it stuck.
    """
    screen_dc = user32.GetDC(0)
    try:
        return snap.grab(
            lambda mem: gdi32.BitBlt(mem, 0, 0, width, height, screen_dc, left, top, snap.SRCCOPY),
            width, height,
        )
    finally:
        user32.ReleaseDC(0, screen_dc)


def cmd_grid(args):
    window, error = resolve_window(args)
    if error:
        return dict(error, ok=False)

    left, top, width, height = window["rect"]
    if width <= 0 or height <= 0:
        return {"ok": False, "error": "empty_window", "title": window["title"]}

    if not args.no_activate:
        activate(window)
        left, top, width, height = rect_of(window["hwnd"])

    data = capture_rect(left, top, width, height)
    buffer = bytearray(data)
    draw_grid(width, height, buffer, args.step)

    out_dir = os.path.abspath(args.out_dir or os.path.join(os.getcwd(), "maya_grid"))
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    name = "%s-%dx%d.png" % (time.strftime("%Y%m%d-%H%M%S"), width, height)
    path = os.path.join(out_dir, name)
    size = snap.write_png(path, width, height, bytes(buffer))

    return {
        "ok": True,
        "path": path,
        "bytes": size,
        "window": {
            "hwnd": window["hwnd"],
            "title": window["title"],
            "rect": window["rect"],
        },
        "origin": [left, top],
        "step": int(args.step),
        "hint": "红色细线每 %d px，亮红线每 %d px；坐标按窗口左上角算，直接给 click --at" % (
            args.step, args.step * 5),
    }


def cmd_click(args):
    window, error = resolve_window(args)
    if error:
        return dict(error, ok=False)

    if args.text:
        child = find_control(window["hwnd"], args.text)
        if child is None:
            return {
                "ok": False,
                "error": "control_not_found",
                "window": window["title"],
                "text": args.text,
                "children": [
                    {"text": row["text"], "class": row["class"], "at": row["at"]}
                    for row in children_of(window["hwnd"]) if row["text"].strip()
                ],
                "hint": "Qt 弹窗没有子控件；改用 grid 截图 + `click --at x,y`",
            }
        result = click_control(child, dry_run=args.dry_run)
        result.update({"ok": True, "window": window["title"], "mode": "text"})
        return result

    if args.screen_at or args.at:
        if args.screen_at:
            try:
                x, y = [int(part) for part in args.screen_at.split(",")]
            except Exception:
                return {"ok": False, "error": "bad_coordinate", "value": args.screen_at}
            mode = "screen"
            relative = None
            origin = [0, 0]
        else:
            try:
                rx, ry = [int(part) for part in args.at.split(",")]
            except Exception:
                return {"ok": False, "error": "bad_coordinate", "value": args.at}
            left, top = window["rect"][0], window["rect"][1]
            relative = [rx, ry]
            origin = [left, top]
            x, y = left + rx, top + ry
            mode = "window"

        if not args.dry_run and not args.no_activate:
            activate(window)
            origin = rect_of(window["hwnd"])[:2]
            if mode == "window" and relative:
                x, y = origin[0] + relative[0], origin[1] + relative[1]

        hit = window_at(x, y)
        hit_root = root_of(hit) if hit else 0
        target_root = root_of(window["hwnd"])
        on_target = hit_root == target_root

        # Clicking through another window is the one way this can do real damage,
        # so it is refused unless the caller says the risk is understood.
        if not on_target and not args.force and not args.dry_run:
            return {
                "ok": False,
                "error": "point_not_on_target",
                "target": {"hwnd": target_root, "title": window["title"]},
                "point": [x, y],
                "hit": {"hwnd": hit_root, "title": text_of(hit_root) if hit_root else ""},
                "hint": "该坐标现在被别的窗口挡住；让弹窗可见后重试，确认无误可加 --force",
            }

        result = click_screen(x, y, dry_run=args.dry_run)
        result.update({
            "ok": True,
            "window": window["title"],
            "mode": mode,
            "relative": relative,
            "windowOrigin": origin,
            "onTarget": on_target,
        })
        return result

    return {"ok": False, "error": "nothing_to_click", "hint": "pass --text, --at or --screen-at"}


# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(description="Inspect and click Maya's windows instead of killing it.")
    parser.add_argument("--pid", type=int, default=0, help="Maya pid")
    parser.add_argument("--hwnd", type=int, default=0, help="window handle (overrides --pid/--title)")
    parser.add_argument("--title", default=None, help="window title substring, case-insensitive")
    subparsers = parser.add_subparsers(dest="command")

    targets = subparsers.add_parser("targets", help="list windows and clickable controls")
    targets.add_argument("--max-windows", type=int, default=8)
    targets.add_argument("--no-children", dest="children", action="store_false", help="skip control enumeration")
    targets.set_defaults(children=True)

    grid = subparsers.add_parser("grid", help="screenshot with a coordinate grid")
    grid.add_argument("--step", type=int, default=50, help="grid spacing in pixels")
    grid.add_argument("--out-dir", default=None, help="where to write the PNG")
    grid.add_argument("--no-activate", dest="no_activate", action="store_true",
                      help="do not raise the window before grabbing the screen")

    click = subparsers.add_parser("click", help="click a control or a coordinate")
    click.add_argument("--text", default=None, help="control label substring (classic Win32 dialogs)")
    click.add_argument("--at", default=None, help="window-relative x,y (from the grid)")
    click.add_argument("--screen-at", default=None, help="absolute screen x,y")
    click.add_argument("--dry-run", action="store_true", help="report the target without clicking")
    click.add_argument("--no-activate", dest="no_activate", action="store_true",
                       help="do not raise the window before a coordinate click")
    click.add_argument("--force", action="store_true",
                       help="click even when another window is under that point")

    return parser


GLOBAL_OPTIONS = ("--pid", "--hwnd", "--title")


def reorder_argv(argv):
    """Accept `--pid 1 targets` and `targets --pid 1` alike.

    argparse only binds options declared on the top-level parser before the
    subcommand, so the shared window selectors are lifted to the front wherever
    the caller wrote them.
    """
    argv = list(argv)
    head, rest = [], []
    index = 0
    while index < len(argv):
        token = argv[index]
        name = token.split("=", 1)[0]
        if name in GLOBAL_OPTIONS:
            head.append(token)
            if "=" not in token and index + 1 < len(argv):
                head.append(argv[index + 1])
                index += 2
                continue
            index += 1
            continue
        rest.append(token)
        index += 1
    return head + rest


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    user32.SetProcessDPIAware()

    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(reorder_argv(argv))
    if not args.command:
        parser.print_help()
        return 2

    handler = {"targets": cmd_targets, "grid": cmd_grid, "click": cmd_click}[args.command]
    try:
        payload = handler(args)
    except Exception as exc:
        payload = {"ok": False, "error": "exception", "detail": str(exc)}

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
