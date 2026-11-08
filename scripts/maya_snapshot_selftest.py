#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-test for the diagnostic retention rules — no Maya, no capture involved.

    python maya_snapshot_selftest.py

Controls file timestamps instead of waiting a day, so every branch is exercised:
the plain rolling window, a fresh confirmation suspending it, an expired
confirmation, a custom window, and the retention file never deleting itself.
"""

from __future__ import print_function

import io
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import maya_snapshot as snap  # noqa: E402

HOUR = 3600.0
FAILURES = []


def make_dir(files):
    root = tempfile.mkdtemp(prefix="retention-")
    shots = os.path.join(root, "diagnostics")
    os.makedirs(shots)
    now = time.time()
    for name, age_hours in files.items():
        path = os.path.join(shots, name)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        stamp = now - age_hours * HOUR
        os.utime(path, (stamp, stamp))
    return root, shots


def check(label, got, expected):
    ok = got == expected
    print("  %-56s %s" % (label, "PASS" if ok else "FAIL  got=%s expected=%s" % (got, expected)))
    if not ok:
        FAILURES.append(label)


def remaining(shots):
    return sorted(name for name in os.listdir(shots) if name != snap.RETENTION_FILE)


def main():
    print("=== unattended: only the last 24 hours survive ===")
    root, shots = make_dir({"a-1h.png": 1, "b-23h.png": 23, "c-25h.png": 25, "d-48h.png": 48})
    report = snap.prune_diagnostics(shots)
    check("keep 1h/23h, drop 25h/48h", remaining(shots), ["a-1h.png", "b-23h.png"])
    check("pruned list", sorted(report["pruned"]), ["c-25h.png", "d-48h.png"])
    check("kept count", report["kept"], 2)
    shutil.rmtree(root)

    print("=== a fresh confirmation restarts the clock ===")
    root, shots = make_dir({"a-1h.png": 1, "old-30h.png": 30, "ancient-72h.png": 72})
    snap.acknowledge(shots)
    report = snap.prune_diagnostics(shots)
    check("even old files survive", remaining(shots), ["a-1h.png", "ancient-72h.png", "old-30h.png"])
    check("suspendedUntil reported", report["suspendedUntil"] is not None, True)
    shutil.rmtree(root)

    print("=== an expired confirmation (25h ago) resumes the plain rule ===")
    root, shots = make_dir({"a-1h.png": 1, "old-30h.png": 30})
    snap.acknowledge(shots, now=time.time() - 25 * HOUR)
    snap.prune_diagnostics(shots)
    check("old file goes", remaining(shots), ["a-1h.png"])
    shutil.rmtree(root)

    print("=== custom window ===")
    root, shots = make_dir({"a-1h.png": 1, "old-30h.png": 30, "ancient-72h.png": 72})
    snap.write_retention(shots, {"keepHours": 48.0, "ackedAt": None})
    snap.prune_diagnostics(shots)
    check("48h window keeps 30h, drops 72h", remaining(shots), ["a-1h.png", "old-30h.png"])
    shutil.rmtree(root)

    print("=== the retention file never deletes itself ===")
    root, shots = make_dir({"a-1h.png": 1, "old-30h.png": 30})
    snap.acknowledge(shots, now=time.time() - 100 * HOUR)
    snap.prune_diagnostics(shots)
    check("retention.json survives", os.path.isfile(snap.retention_path(shots)), True)
    check("30h gone, 1h kept", remaining(shots), ["a-1h.png"])
    shutil.rmtree(root)

    print("=== summary ===")
    if FAILURES:
        print("  FAILED: %s" % FAILURES)
        return 1
    print("  all PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
