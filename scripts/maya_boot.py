# -*- coding: utf-8 -*-
"""Runs INSIDE Maya, at startup, to open the remote-control command port.

Launched by ``maya_ctl.py ensure`` as Maya's ``-command`` argument, so it must
survive every supported interpreter: Maya 2018/2019 run Python 2.7, Maya
2023+ run Python 3.  Nothing here uses f-strings, keyword-only args, or
``pathlib``.

What it does:

1. opens Maya's ``commandPort`` (Python source type when the build accepts it);
2. writes ``session.json`` so callers know pid, version, port and source type;
3. keeps ``heartbeat.json`` fresh from a plain I/O thread, including the current
   scene path, so a watchdog can reopen the right file after a crash;
4. ships a tiny request runner used by the client: the client drops a payload
   file and sends a one-line ``exec`` over the port, and the payload writes its
   result as JSON next to itself.
"""

from __future__ import print_function

import io
import json
import os
import sys
import threading
import time
import traceback

STATE_DIR = os.environ.get("MAYA_REMOTE_STATE") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "dsh-maya-remote"
)
REQUESTS_DIR = os.path.join(STATE_DIR, "requests")
SESSION_PATH = os.path.join(STATE_DIR, "session.json")
HEARTBEAT_PATH = os.path.join(STATE_DIR, "heartbeat.json")
BOOT_LOG = os.path.join(STATE_DIR, "maya_boot.log")

PORT = int(os.environ.get("MAYA_REMOTE_PORT") or "7001")
REQUESTED_SCENE = os.environ.get("MAYA_REMOTE_SCENE") or ""
HEARTBEAT_SECONDS = float(os.environ.get("MAYA_REMOTE_HEARTBEAT") or "2")


def log(message):
    """Append one line to the Maya-side boot log; never raise."""
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        line = "%s %s\n" % (stamp, message)
        handle = io.open(BOOT_LOG, "a", encoding="utf-8")
        try:
            handle.write(line)
        finally:
            handle.close()
    except Exception:
        pass


def write_json(path, payload):
    """Atomic JSON write: a reader must never see a half-written file."""
    temporary = path + ".tmp"
    handle = io.open(temporary, "w", encoding="utf-8")
    try:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        handle.close()
    try:
        os.replace(temporary, path)
    except AttributeError:  # Python 2 has no os.replace
        if os.path.exists(path):
            os.remove(path)
        os.rename(temporary, path)


def import_maya():
    import maya.cmds as cmds

    return cmds


def open_port(cmds, port):
    """Open the command port; return (name, source_type) actually used."""
    name = ":%d" % port
    attempts = [
        ({"sourceType": "python"}, "python"),
        ({"stp": "python"}, "python"),
        ({}, "mel"),
    ]
    last_error = None
    for kwargs, source_type in attempts:
        try:
            cmds.commandPort(name=name, **kwargs)
            log("commandPort opened name=%s sourceType=%s kwargs=%s" % (name, source_type, sorted(kwargs)))
            return name, source_type
        except Exception as error:  # noqa: BLE001 - report the last one
            last_error = error
            log("commandPort attempt failed kwargs=%s: %r" % (sorted(kwargs), error))
    raise RuntimeError("could not open commandPort on %s: %r" % (name, last_error))


def current_scene(cmds):
    try:
        path = cmds.file(query=True, sceneName=True)
        return path or ""
    except Exception:
        return ""


def maya_version(cmds):
    try:
        return str(cmds.about(version=True))
    except Exception:
        return ""


def start_heartbeat(cmds, session):
    """Write heartbeat.json from a plain I/O thread.

    The thread must never touch the Maya API: ``cmds.file(query=True)`` from a
    worker crashes Maya 2025 a few seconds after startup.  The scene path is
    therefore refreshed on the main thread (idle event) into a plain dict, and
    the thread only reads that string and writes files.
    """
    stop = threading.Event()
    scene_state = {"scene": session.get("scene") or ""}

    def refresh_scene():
        try:
            scene_state["scene"] = current_scene(cmds)
        except Exception:
            pass

    try:
        import maya.utils as maya_utils
        # scriptJob is not safe this early (-command runs before the message
        # system is up); register it once Maya is idle instead.
        maya_utils.executeDeferred(lambda: cmds.scriptJob(idleEvent=refresh_scene, protected=True))
    except Exception:
        # No idle hook: keep the boot-time value rather than calling Maya here.
        pass

    def beat():
        while not stop.is_set():
            try:
                write_json(
                    HEARTBEAT_PATH,
                    {
                        "pid": os.getpid(),
                        "port": session["port"],
                        "scene": scene_state["scene"],
                        "at": time.time(),
                    },
                )
            except Exception:
                pass
            stop.wait(HEARTBEAT_SECONDS)

    thread = threading.Thread(target=beat)
    thread.daemon = True
    thread.start()
    return thread


def main():
    if not os.path.isdir(REQUESTS_DIR):
        os.makedirs(REQUESTS_DIR)

    log("boot start pid=%s python=%s port=%s" % (os.getpid(), sys.version.split()[0], PORT))
    cmds = import_maya()

    try:
        name, source_type = open_port(cmds, PORT)
    except Exception as error:
        log("FATAL %r" % (error,))
        write_json(
            os.path.join(STATE_DIR, "boot_error.json"),
            {"pid": os.getpid(), "error": str(error), "port": PORT, "python": sys.version.split()[0]},
        )
        raise

    scene = current_scene(cmds) or REQUESTED_SCENE
    session = {
        "pid": os.getpid(),
        "port": PORT,
        "portName": name,
        "sourceType": source_type,
        "mayaVersion": maya_version(cmds),
        "python": sys.version.split()[0],
        "scene": scene,
        "stateDir": STATE_DIR,
        "exe": sys.executable,
        "startedAt": time.time(),
    }
    write_json(SESSION_PATH, session)
    start_heartbeat(cmds, session)
    log("boot ready %s" % json.dumps(session))
    print("[maya-remote] command port %s (%s) ready, pid %s" % (name, source_type, os.getpid()))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("boot crashed:\n%s" % traceback.format_exc())
        raise
