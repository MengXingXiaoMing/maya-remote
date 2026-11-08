#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Control a local Maya instance: detect, start, drive, restart.

The caller is any process on this machine — typically another AI agent.  Every
command prints one JSON object, so callers parse rather than scrape.

    python maya_ctl.py detect
    python maya_ctl.py ensure [--version 2025] [--scene "C:/x/scene.mb"] [--minimized]
    python maya_ctl.py status
    python maya_ctl.py exec --code "import maya.cmds as cmds; result = cmds.ls()"
    python maya_ctl.py exec --file test_script.py
    python maya_ctl.py restart
    python maya_ctl.py kill
    python maya_ctl.py watchdog --start | --stop | --status | --once

Why the requests travel as files
--------------------------------
Maya's ``commandPort`` replies with whatever the evaluated script prints and a
NUL terminator, which is fragile to parse and truncates easily.  So the client
writes a payload file, sends a one-line ``exec`` that runs it inside Maya, and
the payload writes its own result JSON next to itself.  The socket only has to
deliver one short line.
"""

from __future__ import print_function

import argparse
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
BOOT_SCRIPT = os.path.join(HERE, "maya_boot.py")
WATCHDOG_SCRIPT = os.path.join(HERE, "maya_watchdog.py")
SNAPSHOT_SCRIPT = os.path.join(HERE, "maya_snapshot.py")
CLICK_SCRIPT = os.path.join(HERE, "maya_click.py")

DEFAULT_STATE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "dsh-maya-remote"
)
DEFAULT_ROOTS = [r"C:\Program Files\Autodesk"]
PORT_CANDIDATES = list(range(7001, 7011))

# --------------------------------------------------------------------------
# state


def state_dir(args=None):
    override = getattr(args, "state_dir", None) if args else None
    root = override or os.environ.get("MAYA_REMOTE_STATE") or DEFAULT_STATE_DIR
    return os.path.abspath(root)


def requests_dir(root):
    path = os.path.join(root, "requests")
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def read_json(path):
    try:
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))
    except Exception:
        return None


def write_json(path, payload):
    temporary = path + ".tmp"
    with open(temporary, "wb") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    try:
        os.replace(temporary, path)
    except AttributeError:
        if os.path.exists(path):
            os.remove(path)
        os.rename(temporary, path)


def session_path(root):
    return os.path.join(root, "session.json")


# --------------------------------------------------------------------------
# processes


def pid_alive(pid):
    """OpenProcess with QUERY_LIMITED_INFORMATION: true while the pid exists."""
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not process:
        return False
    exit_code = ctypes.c_ulong()
    alive = ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))
    ctypes.windll.kernel32.CloseHandle(process)
    # 259 == STILL_ACTIVE
    return bool(alive) and exit_code.value == 259


def kill_tree(pid):
    if not pid:
        return False
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return True
    except Exception:
        return False


def port_open(port, host="127.0.0.1", timeout=0.6):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def first_free_port(candidates=None):
    for port in candidates or PORT_CANDIDATES:
        if not port_open(port):
            return port
    return None


# --------------------------------------------------------------------------
# Maya detection


def version_from_scene(path):
    """Read the Maya version out of a scene file.

    ``.ma`` is text and always carries ``//Maya 2025`` in its header.  ``.mb`` is
    binary, but its header block usually still holds the product string, so a
    short undecoded peek often works; when it does not, the caller falls back to
    the newest install.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return None
    text = head.decode("latin-1", "replace")
    for pattern in (
        r"requires maya \"(20\d\d)\"",  # ascii scene, requires line
        r"Maya\s+(?:ASCII\s+)?(20\d\d)",  # ascii scene header
        r"HEADVERS.{0,24}?(20\d\d)",  # binary scene version field
        r"\b(20\d\d)\b",  # last resort
    ):
        match = re.search(pattern, text, re.S)
        if match:
            return match.group(1)
    return None


def detect_installs(roots=None):
    """Every Maya under the given roots, newest last."""
    found = []
    for root in roots or DEFAULT_ROOTS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            match = re.match(r"^Maya(\d{4})$", name)
            if not match:
                continue
            exe = os.path.join(root, name, "bin", "maya.exe")
            if not os.path.isfile(exe):
                continue
            mayapy = os.path.join(root, name, "bin", "mayapy.exe")
            prefs = os.path.join(os.path.expanduser("~"), "Documents", "maya", match.group(1))
            found.append(
                {
                    "version": match.group(1),
                    "product": name,
                    "exe": exe,
                    "mayapy": mayapy if os.path.isfile(mayapy) else None,
                    "prefsDir": prefs,
                    "prefsExists": os.path.isdir(prefs),
                    "py2": match.group(1) in ("2018", "2019", "2020"),
                }
            )
    found.sort(key=lambda row: row["version"])
    return found


def choose_install(installs, requested):
    if not installs:
        raise SystemExit("no Maya installation found")
    if not requested or requested == "auto":
        return installs[-1]
    for row in installs:
        if row["version"] == str(requested):
            return row
    raise SystemExit("Maya %s is not installed (have: %s)" % (requested, ", ".join(r["version"] for r in installs)))


def running_maya_pids():
    """Pids of maya.exe processes, without needing psutil."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq maya.exe", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        ).stdout.decode("utf-8", "replace")
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().endswith("maya.exe"):
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


# --------------------------------------------------------------------------
# the wire


def payload_runner(request_id, root):
    """The script Maya executes: runs the user's code, captures output, writes JSON."""
    code_path = os.path.join(requests_dir(root), "%s.code.py" % request_id)
    result_path = os.path.join(requests_dir(root), "%s.json" % request_id)
    template = u'''# -*- coding: utf-8 -*-
from __future__ import print_function
import io, json, os, sys, time, traceback

CODE_PATH = r"%(code)s"
RESULT_PATH = r"%(result)s"


class _Capture(object):
    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)

    def flush(self):
        pass

    def value(self):
        return u"".join(self.parts)


def _scene():
    try:
        import maya.cmds as cmds
        return cmds.file(query=True, sceneName=True) or ""
    except Exception:
        return ""


def _finish(payload):
    temporary = RESULT_PATH + ".tmp"
    handle = io.open(temporary, "w", encoding="utf-8")
    try:
        handle.write(json.dumps(payload, ensure_ascii=False))
    finally:
        handle.close()
    try:
        os.replace(temporary, RESULT_PATH)
    except AttributeError:
        if os.path.exists(RESULT_PATH):
            os.remove(RESULT_PATH)
        os.rename(temporary, RESULT_PATH)


_out = _Capture()
_err = _Capture()
_saved = (sys.stdout, sys.stderr)
sys.stdout, sys.stderr = _out, _err
_started = time.time()
_ok = True
_error = None
_result = None
try:
    _source = io.open(CODE_PATH, encoding="utf-8").read()
    _namespace = {"__name__": "__maya_remote__", "__file__": CODE_PATH}
    exec(compile(_source, CODE_PATH, "exec"), _namespace)
    _result = _namespace.get("result")
except BaseException:
    _ok = False
    _error = traceback.format_exc()
finally:
    sys.stdout, sys.stderr = _saved

try:
    _encoded = json.dumps(_result, ensure_ascii=False)
except Exception:
    _encoded = json.dumps(repr(_result), ensure_ascii=False)

_finish({
    "id": %(ident)r,
    "ok": _ok,
    "exception": _error,
    "stdout": _out.value(),
    "stderr": _err.value(),
    "result": json.loads(_encoded),
    "scene": _scene(),
    "python": sys.version.split()[0],
    "elapsedMs": int((time.time() - _started) * 1000),
})
''' % {"code": code_path, "result": result_path, "ident": request_id}

    with open(os.path.join(requests_dir(root), "%s.runner.py" % request_id), "wb") as handle:
        handle.write(template.encode("utf-8"))
    return os.path.join(requests_dir(root), "%s.runner.py" % request_id), result_path


def exec_line(runner_path, source_type):
    """One line Maya evaluates; the runner does everything else.

    Two things about this line are load-bearing, both measured against Maya 2024:

    * It must be a **statement**, not a bare expression.  A payload like
      ``exec(compile(...))`` is evaluated for its return value *and* executed,
      so every side effect happens twice — a ``polyCube`` payload creates two
      cubes.  ``_ = exec(...)`` runs exactly once.
    * The explicit globals dict matters too: Maya evaluates a payload with
      separate globals and locals, so without it the runner's module-level names
      land in a locals mapping its own functions cannot see.
    """
    inner = (
        "_ = exec(compile(__import__('io').open(r'%s', encoding='utf-8').read(), 'maya_req', 'exec'), "
        "{'__name__': '__maya_remote_runner__'})" % runner_path
    )
    if source_type == "mel":
        return 'python("%s")' % inner
    return inner


def ask_maya(port, line, source_type="python", wait_reply=False, reply_timeout=1.0):
    """Send one line to the command port. Raises OSError when Maya is unreachable."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall((line + "\n").encode("utf-8"))
        if not wait_reply:
            return b""
        client.settimeout(reply_timeout)
        buffer = b""
        try:
            while b"\x00" not in buffer and len(buffer) < 65536:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buffer += chunk
        except socket.timeout:
            pass
        return buffer


def run_python(root, port, source_type, code, timeout=60.0):
    """Run code inside Maya; return the payload dict the runner wrote."""
    request_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    runner_path, result_path = payload_runner(request_id, root)
    with open(os.path.join(requests_dir(root), "%s.code.py" % request_id), "wb") as handle:
        handle.write(code.encode("utf-8"))

    for stale in (result_path, result_path + ".tmp"):
        if os.path.exists(stale):
            os.remove(stale)

    started = time.time()
    if not port_open(port):
        return {
            "ok": False,
            "error": "maya_unreachable",
            "detail": "nothing is listening on 127.0.0.1:%d (Maya is down)" % port,
            "id": request_id,
        }
    # Keep whatever Maya says about the payload: when the runner itself fails,
    # this reply is the only place the reason appears.
    try:
        reply = ask_maya(port, exec_line(runner_path, source_type), wait_reply=True, reply_timeout=2.0)
    except OSError as error:
        return {"ok": False, "error": "maya_unreachable", "detail": str(error), "id": request_id}
    reply_text = reply.split(b"\x00")[0].decode("utf-8", "replace").strip()

    while time.time() - started < timeout:
        payload = read_json(result_path)
        if payload is not None:
            payload["roundTripMs"] = int((time.time() - started) * 1000)
            if reply_text and reply_text != "None":
                payload["portReply"] = reply_text[:500]
            return payload
        time.sleep(0.05)

    return {
        "ok": False,
        "error": "maya_timeout",
        "detail": (
            "port accepted the command but produced no result in %.0fs — Maya is likely hung" % timeout
            + ("; the port replied: %s" % reply_text[:300] if reply_text and reply_text != "None" else "")
        ),
        "portReply": reply_text[:500],
        "id": request_id,
        "requestsDir": requests_dir(root),
    }


PING_CODE = (
    "try:\n"
    "    import maya.cmds as cmds\n"
    "    result = {'alive': True, 'version': str(cmds.about(version=True))}\n"
    "except Exception as error:\n"
    "    result = {'alive': True, 'version': 'unknown', 'note': str(error)}\n"
)


def probe(root, port, source_type, timeout=20.0):
    return run_python(root, port, source_type, PING_CODE, timeout=timeout)


# --------------------------------------------------------------------------
# commands


def cmd_detect(args):
    installs = detect_installs(args.root)
    pids = running_maya_pids()
    report = {
        "installs": installs,
        "newest": installs[-1]["version"] if installs else None,
        "runningMayaPids": pids,
        "stateDir": state_dir(args),
    }
    scene = getattr(args, "scene", None)
    if scene:
        sniffed = version_from_scene(scene)
        installed = {row["version"] for row in installs}
        report["scene"] = scene
        report["sceneVersion"] = sniffed
        report["wouldLaunch"] = sniffed if sniffed in installed else report["newest"]
        report["sceneVersionInstalled"] = sniffed in installed
    return report


def collect_status(args):
    root = state_dir(args)
    session = read_json(session_path(root))
    heartbeat = read_json(os.path.join(root, "heartbeat.json"))
    report = {
        "stateDir": root,
        "session": session,
        "heartbeat": heartbeat,
        "heartbeatAgeSec": (round(time.time() - heartbeat["at"], 1) if heartbeat and heartbeat.get("at") else None),
        "installs": [row["version"] for row in detect_installs(args.root)],
        "runningMayaPids": running_maya_pids(),
    }
    if not session:
        report.update({"alive": False, "reason": "no session.json — Maya was never started by this tool"})
        return report

    port = session.get("port") or 7001
    source_type = session.get("sourceType") or "python"
    report["pidAlive"] = pid_alive(session.get("pid"))
    report["portOpen"] = port_open(port)
    if report["pidAlive"] and report["portOpen"] and not args.no_ping:
        ping = probe(root, port, source_type, timeout=args.ping_timeout)
        report["ping"] = {
            "ok": bool(ping.get("ok")),
            "roundTripMs": ping.get("roundTripMs"),
            "version": (ping.get("result") or {}).get("version") if isinstance(ping.get("result"), dict) else None,
            "error": ping.get("error"),
        }
        report["alive"] = bool(ping.get("ok"))
    else:
        report["alive"] = bool(report["pidAlive"] and report["portOpen"])
    return report


def cmd_status(args):
    return collect_status(args)


def ensure(args):
    """Start Maya when needed; return the session once the port answers."""
    root = state_dir(args)
    if not os.path.isdir(root):
        os.makedirs(root)
    session = read_json(session_path(root))

    if session and not args.force:
        report = collect_status(args)
        if report.get("alive"):
            report["action"] = "already-running"
            return report

    installs = detect_installs(args.root)
    requested = args.version
    if (not requested or requested == "auto") and args.scene:
        sniffed = version_from_scene(args.scene)
        if sniffed and any(row["version"] == sniffed for row in installs):
            requested = sniffed
    install = choose_install(installs, requested)

    port = args.port or first_free_port()
    if not port:
        raise SystemExit("no free command port in %s" % (PORT_CANDIDATES,))

    scene = args.scene
    if not scene:
        heartbeat = read_json(os.path.join(root, "heartbeat.json"))
        scene = (heartbeat or {}).get("scene") or ""

    environment = dict(os.environ)
    environment.update(
        {
            "MAYA_REMOTE_PORT": str(port),
            "MAYA_REMOTE_STATE": root,
            "MAYA_REMOTE_SCENE": scene or "",
        }
    )

    mel_command = 'python("exec(compile(__import__(\'io\').open(r\'%s\', encoding=\'utf-8\').read(), \'maya_boot\', \'exec\'))")' % (
        BOOT_SCRIPT.replace("\\", "/"),
    )
    argv = [install["exe"]]
    if scene and os.path.isfile(scene):
        argv += ["-file", scene]
    argv += ["-command", mel_command]

    startup = None
    flags = 0
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 2 if args.minimized else 1  # SW_SHOWMINIMIZED / SW_SHOWNORMAL
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    # A fresh session file must be newer than this launch, or we would read the
    # previous run's record as if it were ours.
    for stale in (session_path(root), os.path.join(root, "boot_error.json")):
        if os.path.exists(stale):
            os.remove(stale)

    process = subprocess.Popen(
        argv,
        env=environment,
        close_fds=True,
        startupinfo=startup,
        creationflags=flags,
        cwd=os.path.dirname(install["exe"]),
    )

    deadline = time.time() + args.timeout
    # A cold Maya spends its first minute loading plugins with the main thread
    # busy, so an answer can take far longer than the steady-state round trip.
    start_ping_timeout = max(args.ping_timeout, 90.0)
    last = None
    while time.time() < deadline:
        if process.poll() is not None and not pid_alive(process.pid):
            boot_error = read_json(os.path.join(root, "boot_error.json"))
            return {
                "action": "failed",
                "ok": False,
                "exe": install["exe"],
                "exitCode": process.returncode,
                "bootError": boot_error,
                "last": last,
            }
        created = read_json(session_path(root))
        if created and created.get("pid") == process.pid:
            if port_open(port):
                ping = probe(root, port, created.get("sourceType") or "python", timeout=start_ping_timeout)
                if ping.get("ok"):
                    return {
                        "action": "started",
                        "ok": True,
                        "session": created,
                        "ping": ping.get("result"),
                        "launchSeconds": round(time.time() - (deadline - args.timeout), 1),
                    }
                last = ping
        time.sleep(0.5)

    return {
        "action": "timeout",
        "ok": False,
        "exe": install["exe"],
        "pid": process.pid,
        "port": port,
        "last": last,
        "hint": "check %s and %s" % (os.path.join(root, "maya_boot.log"), os.path.join(root, "boot_error.json")),
    }


def cmd_ensure(args):
    return ensure(args)


def cmd_exec(args):
    root = state_dir(args)
    session = read_json(session_path(root))
    if not session:
        return {"ok": False, "error": "no_session", "detail": "run `ensure` first"}
    if args.file:
        with open(args.file, "rb") as handle:
            code = handle.read().decode("utf-8-sig")  # editors love a BOM; compile() does not
    else:
        code = args.code if args.code is not None else sys.stdin.read()
        code = code.lstrip("\ufeff")
    payload = run_python(
        root,
        session.get("port") or 7001,
        session.get("sourceType") or "python",
        code,
        timeout=args.timeout,
    )
    if args.raw:
        return payload
    transport = payload.get("error") in ("maya_unreachable", "maya_timeout")
    exception = payload.get("exception")
    if payload.get("ok"):
        failure = None
    elif transport:
        failure = payload.get("error")
    else:
        failure = "python_exception"
    return {
        "ok": bool(payload.get("ok")),
        "error": failure,
        "detail": payload.get("detail"),
        "stdout": payload.get("stdout", ""),
        "stderr": payload.get("stderr", ""),
        "result": payload.get("result"),
        "traceback": exception,
        "elapsedMs": payload.get("elapsedMs"),
        "roundTripMs": payload.get("roundTripMs"),
        "scene": payload.get("scene"),
        "mayaPython": payload.get("python"),
        "requestId": payload.get("id"),
    }


def cmd_ping(args):
    root = state_dir(args)
    session = read_json(session_path(root))
    if not session:
        return {"ok": False, "error": "no_session", "detail": "run `ensure` first"}
    payload = probe(root, session.get("port") or 7001, session.get("sourceType") or "python", timeout=args.timeout)
    return {
        "ok": bool(payload.get("ok")),
        "error": payload.get("error"),
        "detail": payload.get("detail"),
        "roundTripMs": payload.get("roundTripMs"),
        "result": payload.get("result"),
    }


def cmd_kill(args):
    root = state_dir(args)
    session = read_json(session_path(root))
    killed = []
    if session and session.get("pid"):
        killed.append(session["pid"])
        kill_tree(session["pid"])
    if args.all:
        for pid in running_maya_pids():
            if pid not in killed:
                killed.append(pid)
                kill_tree(pid)
    return {"ok": True, "killed": killed, "mode": "all" if args.all else "session"}


def cmd_restart(args):
    root = state_dir(args)
    previous = read_json(session_path(root)) or {}
    heartbeat = read_json(os.path.join(root, "heartbeat.json")) or {}
    scene = args.scene or heartbeat.get("scene") or previous.get("scene") or ""
    kill_result = cmd_kill(args)
    time.sleep(2.0)
    args.scene = scene
    args.force = True
    args.version = args.version or previous.get("mayaVersion") or "auto"
    ensured = ensure(args)
    return {"killed": kill_result["killed"], "scene": scene, "ensure": ensured}


def cmd_diagnose(args):
    """Photograph Maya before anyone kills it: raise, capture, report."""
    root = state_dir(args)
    session = read_json(session_path(root)) or {}
    pid = args.pid or session.get("pid")
    if not pid:
        return {"ok": False, "error": "no_session", "detail": "run `ensure` first, or pass --pid"}

    argv = [sys.executable, SNAPSHOT_SCRIPT, "--state-dir", root, "--pid", str(pid)]
    if args.full_screen:
        argv.append("--full-screen")
    if args.screen_only:
        argv.append("--screen-only")
    if args.no_raise:
        argv.append("--no-raise")
    if args.max_windows:
        argv += ["--max-windows", str(args.max_windows)]

    completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    text = completed.stdout.decode("utf-8", "replace")
    try:
        report = json.loads(text)
    except Exception:
        return {
            "ok": False,
            "error": "snapshot_unparsable",
            "stdout": text[:2000],
            "stderr": completed.stderr.decode("utf-8", "replace")[:2000],
        }
    report["pid"] = pid
    report["session"] = {
        "mayaVersion": session.get("mayaVersion"),
        "port": session.get("port"),
        "scene": session.get("scene"),
    }
    return report


def _click_tool(extra):
    """Run maya_click.py and return its JSON, so the API stays one shape."""
    argv = [sys.executable, CLICK_SCRIPT] + list(extra)
    completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    text = completed.stdout.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except Exception:
        return {
            "ok": False,
            "error": "click_tool_unparsable",
            "stdout": text[:2000],
            "stderr": completed.stderr.decode("utf-8", "replace")[:2000],
        }


def _window_args(args):
    """Shared --pid/--hwnd/--title prefix; pid falls back to the recorded session."""
    root = state_dir(args)
    session = read_json(session_path(root)) or {}
    pid = getattr(args, "pid", 0) or session.get("pid")
    argv = []
    if pid:
        argv += ["--pid", str(pid)]
    if getattr(args, "hwnd", 0):
        argv += ["--hwnd", str(getattr(args, "hwnd"))]
    if getattr(args, "title", None):
        argv += ["--title", getattr(args, "title")]
    return argv


def cmd_targets(args):
    """List windows and the controls a click could land on."""
    extra = _window_args(args) + ["targets", "--max-windows", str(args.max_windows)]
    if getattr(args, "no_children", False):
        extra.append("--no-children")
    return _click_tool(extra)


def cmd_grid(args):
    """Screenshot the dialog with a coordinate grid."""
    extra = _window_args(args) + ["grid", "--step", str(args.step)]
    if getattr(args, "out_dir", None):
        extra += ["--out-dir", args.out_dir]
    if getattr(args, "no_activate", False):
        extra.append("--no-activate")
    return _click_tool(extra)


def cmd_click(args):
    """Click a dialog control / coordinate instead of killing Maya."""
    prefix = _window_args(args)
    extra = prefix + ["click"]
    if getattr(args, "text", None):
        extra += ["--text", args.text]
    if getattr(args, "at", None):
        extra += ["--at", args.at]
    if getattr(args, "screen_at", None):
        extra += ["--screen-at", args.screen_at]
    if getattr(args, "dry_run", False):
        extra.append("--dry-run")
    if getattr(args, "no_activate", False):
        extra.append("--no-activate")
    if getattr(args, "force", False):
        extra.append("--force")
    if len(extra) <= len(prefix) + 1:
        return {"ok": False, "error": "nothing_to_click", "hint": "pass --text, --at or --screen-at"}
    return _click_tool(extra)


def cmd_diagnostics(args):
    """Diagnostic retention: report it, confirm it, prune it.

    The window is 24h by default.  A confirmation restarts the clock, so saying
    "I have seen these" never deletes anything — and nothing here ever asks for
    that confirmation; without one the plain window applies.
    """
    root = state_dir(args)
    argv = [sys.executable, SNAPSHOT_SCRIPT, "--state-dir", root, "--report-only"]
    if args.ack:
        argv.append("--ack")
    if args.keep_hours is not None:
        argv += ["--keep-hours", str(args.keep_hours)]
    if args.no_prune:
        argv.append("--no-prune")

    completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    text = completed.stdout.decode("utf-8", "replace")
    try:
        report = json.loads(text)
    except Exception:
        return {
            "ok": False,
            "error": "diagnostics_unparsable",
            "stdout": text[:2000],
            "stderr": completed.stderr.decode("utf-8", "replace")[:2000],
        }
    return report


def cmd_watchdog(args):
    root = state_dir(args)
    config_path = os.path.join(root, "watchdog.json")

    if args.stop:
        config = read_json(config_path) or {}
        pid = config.get("pid")
        if pid and pid_alive(pid):
            kill_tree(pid)
            return {"ok": True, "stopped": pid}
        return {"ok": True, "stopped": None, "detail": "watchdog was not running"}

    if args.status:
        config = read_json(config_path) or {}
        return {
            "ok": True,
            "config": config,
            "running": bool(config.get("pid") and pid_alive(config["pid"])),
            "status": read_json(os.path.join(root, "status.json")),
        }

    argv = [
        sys.executable,
        WATCHDOG_SCRIPT,
        "--state-dir",
        root,
        "--port",
        str(args.port or 0),
        "--poll",
        str(args.poll),
        "--hang-timeout",
        str(args.hang_timeout),
        "--max-restarts",
        str(args.max_restarts),
    ]
    if args.version:
        argv += ["--version", str(args.version)]
    if args.once:
        argv += ["--once"]

    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        argv,
        close_fds=True,
        creationflags=flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not args.once:
        time.sleep(1.0)
        config = read_json(config_path) or {}
        config.update({"pid": process.pid, "startedAt": time.time(), "argv": argv})
        write_json(config_path, config)
    return {"ok": True, "watchdogPid": process.pid, "once": bool(args.once), "argv": argv}


# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(description="Control a local Maya instance from outside.")
    parser.add_argument("--state-dir", default=None, help="state directory (default %%LOCALAPPDATA%%\\dsh-maya-remote)")
    subparsers = parser.add_subparsers(dest="command")

    detect = subparsers.add_parser("detect", help="list installed Maya versions")
    detect.add_argument("--root", action="append", default=None, help="installation root to scan (repeatable)")
    detect.add_argument("--scene", default=None, help="report which version this scene file wants (no launch)")

    status = subparsers.add_parser("status", help="session, process and port state")
    status.add_argument("--root", action="append", default=None)
    status.add_argument("--no-ping", action="store_true", help="skip the in-Maya round trip")
    status.add_argument("--ping-timeout", type=float, default=20.0)

    ensure_cmd = subparsers.add_parser("ensure", help="start Maya when it is not running")
    ensure_cmd.add_argument("--root", action="append", default=None)
    ensure_cmd.add_argument("--version", default="auto", help="Maya version, or auto for the newest")
    ensure_cmd.add_argument("--scene", default=None, help="scene file to open on launch")
    ensure_cmd.add_argument("--port", type=int, default=0, help="command port (default: first free of 7001-7010)")
    ensure_cmd.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for the port")
    ensure_cmd.add_argument("--minimized", action="store_true", help="start Maya minimized")
    ensure_cmd.add_argument("--force", action="store_true", help="relaunch even when already running")
    ensure_cmd.add_argument("--ping-timeout", type=float, default=20.0)

    exec_cmd = subparsers.add_parser("exec", help="run Python inside Maya")
    exec_cmd.add_argument("--code", default=None, help="Python source; stdin when omitted")
    exec_cmd.add_argument("--file", default=None, help="Python file to run inside Maya")
    exec_cmd.add_argument("--timeout", type=float, default=60.0)
    exec_cmd.add_argument("--raw", action="store_true", help="print the raw payload")

    ping = subparsers.add_parser("ping", help="round-trip check against the running Maya")
    ping.add_argument("--timeout", type=float, default=20.0)

    kill = subparsers.add_parser("kill", help="stop Maya")
    kill.add_argument("--all", action="store_true", help="every maya.exe, not just the recorded one")

    restart = subparsers.add_parser("restart", help="kill and relaunch, reopening the recorded scene")
    restart.add_argument("--root", action="append", default=None)
    restart.add_argument("--version", default=None)
    restart.add_argument("--scene", default=None)
    restart.add_argument("--port", type=int, default=0)
    restart.add_argument("--timeout", type=float, default=180.0)
    restart.add_argument("--minimized", action="store_true")
    restart.add_argument("--ping-timeout", type=float, default=20.0)

    diagnose = subparsers.add_parser(
        "diagnose", help="raise Maya and photograph it — evidence before anyone kills it"
    )
    diagnose.add_argument("--pid", type=int, default=0, help="Maya pid; defaults to the recorded session")
    diagnose.add_argument("--full-screen", action="store_true", help="also capture the whole virtual screen")
    diagnose.add_argument("--screen-only", action="store_true", help="skip the per-window captures")
    diagnose.add_argument("--no-raise", action="store_true", help="capture without stealing focus")
    diagnose.add_argument("--max-windows", type=int, default=4, help="how many windows of the process to capture")

    targets = subparsers.add_parser(
        "targets", help="list Maya's windows and the controls a click could hit"
    )
    targets.add_argument("--pid", type=int, default=0, help="Maya pid; defaults to the recorded session")
    targets.add_argument("--hwnd", type=int, default=0, help="window handle instead of pid")
    targets.add_argument("--title", default=None, help="window title substring (case-insensitive)")
    targets.add_argument("--max-windows", type=int, default=8)
    targets.add_argument("--no-children", dest="no_children", action="store_true",
                         help="skip control enumeration")

    grid = subparsers.add_parser(
        "grid", help="screenshot the dialog with a coordinate grid for `click --at`"
    )
    grid.add_argument("--pid", type=int, default=0)
    grid.add_argument("--hwnd", type=int, default=0)
    grid.add_argument("--title", default=None)
    grid.add_argument("--step", type=int, default=50, help="grid spacing in pixels")
    grid.add_argument("--out-dir", default=None, help="where to write the PNG")
    grid.add_argument("--no-activate", dest="no_activate", action="store_true",
                      help="do not raise the window before grabbing the screen")

    click = subparsers.add_parser(
        "click", help="click a dialog control / coordinate instead of killing Maya"
    )
    click.add_argument("--pid", type=int, default=0)
    click.add_argument("--hwnd", type=int, default=0)
    click.add_argument("--title", default=None)
    click.add_argument("--text", default=None, help="control label substring (classic Win32 dialogs)")
    click.add_argument("--at", default=None, help="window-relative x,y (from the grid)")
    click.add_argument("--screen-at", default=None, help="absolute screen x,y")
    click.add_argument("--dry-run", action="store_true", help="report the target without clicking")
    click.add_argument("--no-activate", dest="no_activate", action="store_true",
                       help="do not raise the window before a coordinate click")
    click.add_argument("--force", action="store_true",
                       help="click even when another window is under that point")

    diagnostics = subparsers.add_parser(
        "diagnostics", help="diagnostic retention: report, acknowledge, prune"
    )
    diagnostics.add_argument("--ack", action="store_true", help="the user confirmed the pictures: restart the clock")
    diagnostics.add_argument("--keep-hours", type=float, default=None, help="retention window in hours (persisted)")
    diagnostics.add_argument("--no-prune", action="store_true", help="report without deleting anything")

    watchdog = subparsers.add_parser("watchdog", help="auto-restart loop")
    watchdog.add_argument("--start", action="store_true")
    watchdog.add_argument("--stop", action="store_true")
    watchdog.add_argument("--status", action="store_true")
    watchdog.add_argument("--once", action="store_true", help="run a single check and exit")
    watchdog.add_argument("--version", default=None)
    watchdog.add_argument("--port", type=int, default=0)
    watchdog.add_argument("--poll", type=float, default=5.0)
    watchdog.add_argument("--hang-timeout", type=float, default=45.0)
    watchdog.add_argument("--max-restarts", type=int, default=20)

    return parser


COMMANDS = {
    "detect": cmd_detect,
    "status": cmd_status,
    "ensure": cmd_ensure,
    "exec": cmd_exec,
    "ping": cmd_ping,
    "kill": cmd_kill,
    "restart": cmd_restart,
    "diagnose": cmd_diagnose,
    "targets": cmd_targets,
    "grid": cmd_grid,
    "click": cmd_click,
    "diagnostics": cmd_diagnostics,
    "watchdog": cmd_watchdog,
}


def emit(payload):
    """Print one JSON object, surviving a non-UTF-8 Windows console."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
    sys.stdout.flush()


def main(argv=None):
    try:  # a GBK console must not break a Chinese result
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    args.root = getattr(args, "root", None) or DEFAULT_ROOTS
    for name in ("no_ping", "raw", "all", "once", "stop", "status", "start", "force", "minimized",
                 "dry_run", "no_children", "no_activate"):
        if not hasattr(args, name):
            setattr(args, name, False)
    for name in ("ping_timeout",):
        if not hasattr(args, name):
            setattr(args, name, 20.0)
    try:
        payload = COMMANDS[args.command](args)
    except SystemExit as error:
        payload = {"ok": False, "error": "bad_request", "detail": str(error)}
    emit(payload)
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
