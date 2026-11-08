#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Start ``maya_stub.py`` detached and return — the shape ``maya_ctl ensure`` has.

``ensure`` launches Maya through Popen and exits; a watchdog relaunch must
therefore wait for the *state files*, not for the command to finish.  Pointing
``maya_watchdog.py --ensure-cmd`` at this file rehearses that contract without
Maya.

    python maya_stub_ensure.py --state-dir "%LOCALAPPDATA%\\dsh-maya-remote" [--port 7001]
"""

from __future__ import print_function

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STUB = os.path.join(HERE, "maya_stub.py")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Launch the Maya stub detached, like ensure does.")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--port", type=int, default=7001)
    parser.add_argument("--hang", action="store_true", help="start the stub in hang mode")
    args = parser.parse_args(argv)

    command = [sys.executable, STUB, "--port", str(args.port), "--state-dir", args.state_dir]
    if args.hang:
        command.append("--hang")
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    print(json.dumps({"ok": True, "action": "started", "pid": process.pid, "stub": STUB}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
