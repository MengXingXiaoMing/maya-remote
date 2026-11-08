#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Keep Maya alive: notice a crash or a hang, then relaunch and reopen the scene.

Started by ``maya_ctl.py watchdog --start``.  It owns no Maya API; it only reads
the state directory, probes the command port, and calls ``maya_ctl.py ensure``
when the instance is gone.

Every pass writes ``status.json``, so any other agent can read one file to learn
whether Maya is up, how many times it has been restarted, and why.

    python maya_watchdog.py --once          # one check, act, exit (CI / manual)
    python maya_watchdog.py                 # loop until stopped
"""

from __future__ import print_function

import argparse
import io
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(HERE, "maya_ctl.py")
SNAPSHOT = os.path.join(HERE, "maya_snapshot.py")


def read_json(path):
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            return json.loads(handle.read())
    except Exception:
        return None


def write_json(path, payload):
    temporary = path + ".tmp"
    with io.open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        os.replace(temporary, path)
    except AttributeError:
        if os.path.exists(path):
            os.remove(path)
        os.rename(temporary, path)


def pid_alive(pid):
    if not pid:
        return False
    try:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259
    except Exception:
        return False


class Watchdog(object):
    def __init__(self, args):
        self.args = args
        self.state_dir = os.path.abspath(args.state_dir) if args.state_dir else os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "dsh-maya-remote"
        )
        self.log_path = os.path.join(self.state_dir, "watchdog.log")
        self.status_path = os.path.join(self.state_dir, "status.json")
        self.consecutive_failures = 0

    def log(self, message):
        line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message)
        try:
            with io.open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            pass
        sys.stderr.write(line)
        sys.stderr.flush()

    def ctl(self, *argv):
        command = [sys.executable, CTL, "--state-dir", self.state_dir] + list(argv)
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        text = completed.stdout.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except Exception:
            return {"ok": False, "error": "ctl_unparsable", "stdout": text[:2000], "stderr": completed.stderr.decode("utf-8", "replace")[:2000]}

    def status(self):
        session = read_json(os.path.join(self.state_dir, "session.json"))
        heartbeat = read_json(os.path.join(self.state_dir, "heartbeat.json"))
        port = (session or {}).get("port") or self.args.port or 7001
        pid = (session or {}).get("pid")
        scene = (heartbeat or {}).get("scene") or (session or {}).get("scene") or ""

        if not session or not pid_alive(pid):
            return {"alive": False, "reason": "process-gone", "pid": pid, "port": port, "scene": scene}
        return {"alive": True, "reason": "ok", "pid": pid, "port": port, "scene": scene}

    def ping(self, port):
        report = self.ctl("ping", "--timeout", str(self.args.hang_timeout))
        return bool(report.get("ok"))

    def wait_ready(self, previous_pid, timeout):
        """A relaunch returns before the new instance has written its session."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            session = read_json(os.path.join(self.state_dir, "session.json"))
            if session and session.get("pid") and session.get("pid") != previous_pid:
                if self.ctl("ping", "--timeout", "15").get("ok"):
                    return True
            time.sleep(1.0)
        return False

    def diagnose(self, pid):
        """Photograph Maya *before* the kill.

        A hang is usually one modal dialog away from healthy, so the picture —
        and the window titles around it — is what says whether restarting is
        even necessary.  Runs before every relaunch, crash or hang.
        """
        if not pid:
            return {"ok": False, "detail": "no pid to photograph"}
        argv = [sys.executable, SNAPSHOT, "--state-dir", self.state_dir, "--pid", str(pid)]
        if self.args.diagnose_full_screen:
            argv.append("--full-screen")
        if self.args.diagnose_no_raise:
            argv.append("--no-raise")
        try:
            completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as error:
            self.log("diagnose failed to run: %r" % (error,))
            return {"ok": False, "detail": str(error)}

        text = completed.stdout.decode("utf-8", "replace")
        try:
            report = json.loads(text)
        except Exception:
            self.log("diagnose produced unparsable output")
            return {"ok": False, "detail": "snapshot unparsable", "stdout": text[:400]}

        shots = [shot.get("path") for shot in report.get("shots") or []]
        if shots:
            self.log("diagnostic shots: %s" % ", ".join(shots))
        else:
            self.log("diagnostic: nothing captured (%s)" % ((report.get("notes") or [report.get("detail")])[0],))
        return {
            "ok": bool(report.get("ok")),
            "shots": shots,
            "windowCount": len(report.get("windows") or []),
            "raised": report.get("raised"),
            "notes": report.get("notes"),
            "summaryPath": report.get("summaryPath"),
        }

    def relaunch(self, state):
        self.log("relaunch reason=%s pid=%s scene=%s" % (state.get("reason"), state.get("pid"), state.get("scene") or "-"))
        if self.args.ensure_cmd:
            command = self.args.ensure_cmd
            parts = command.split(" ")
        else:
            parts = []
            if state.get("pid") and pid_alive(state["pid"]):
                # The process exists but does not answer: it must go before a
                # second instance can take the port and the license seat.
                self.ctl("kill")
                time.sleep(2.0)
            parts = [sys.executable, CTL, "--state-dir", self.state_dir, "ensure", "--force"]
            if self.args.version:
                parts += ["--version", str(self.args.version)]
            if self.args.port:
                parts += ["--port", str(self.args.port)]
            if state.get("scene"):
                parts += ["--scene", state["scene"]]
            if self.args.minimized:
                parts += ["--minimized"]

        self.log("ensure: %s" % " ".join(parts))
        try:
            completed = subprocess.run(parts, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as error:
            return {"ok": False, "error": str(error)}

        text = completed.stdout.decode("utf-8", "replace")
        try:
            report = json.loads(text)
        except Exception:
            report = {"ok": False, "error": "ensure_unparsable", "stdout": text[:2000]}
        if not report.get("ok"):
            self.log("ensure failed: %s" % json.dumps(report, ensure_ascii=False)[:800])
        return report

    def write_status(self, payload):
        write_json(self.status_path, payload)

    def run_once(self):
        # Strikes live in status.json, not only in memory: a one-shot pass, a
        # restarted watchdog, or a cron-style caller must all keep counting, or
        # a hang never reaches the restart threshold.
        previous = read_json(self.status_path) or {}
        restarts = int(previous.get("restarts") or 0)
        strikes = int(previous.get("consecutiveFailures") or 0)

        state = self.status()
        healthy = False
        if state["alive"]:
            if self.ping(state["port"]):
                strikes = 0
                self.consecutive_failures = 0
                healthy = True
            else:
                # A missing answer is ambiguous (a long-running command, a busy
                # main thread), so it takes several strikes.
                strikes += 1
                self.consecutive_failures = strikes
                state["reason"] = "hung (no answer %d/%d)" % (strikes, self.args.failures_before_restart)
        else:
            # A dead process is unambiguous: no reason to wait for strikes.
            strikes = self.args.failures_before_restart
            self.consecutive_failures = strikes

        if not healthy and strikes >= self.args.failures_before_restart:
            if restarts >= self.args.max_restarts:
                self.write_status(
                    {
                        "alive": False,
                        "reason": "restart budget spent",
                        "restarts": restarts,
                        "updatedAt": time.time(),
                    }
                )
                self.log("giving up after %d restarts" % restarts)
                return False
            # Evidence first: look at the patient before the operation.
            evidence = self.diagnose(state.get("pid"))
            result = self.relaunch(state)
            restarts += 1
            self.consecutive_failures = 0
            ready = bool(result.get("ok")) and self.wait_ready(state.get("pid"), self.args.ready_timeout)
            after = self.status()
            payload = {
                "alive": ready,
                "restarts": restarts,
                "consecutiveFailures": 0,
                "lastRestartAt": time.time(),
                "lastReason": state.get("reason"),
                "lastError": None if ready else result.get("error") or result.get("detail") or "not ready in time",
                "lastDiagnostic": evidence,
                "pid": after.get("pid"),
                "port": after.get("port"),
                "scene": after.get("scene"),
                "updatedAt": time.time(),
            }
            self.write_status(payload)
            self.log("restart #%d ready=%s pid=%s" % (restarts, ready, payload["pid"]))
            return True

        self.write_status(
            {
                "alive": healthy,
                "reason": state.get("reason"),
                "restarts": restarts,
                "consecutiveFailures": strikes,
                "pid": state.get("pid"),
                "port": state.get("port"),
                "scene": state.get("scene"),
                "updatedAt": time.time(),
            }
        )
        return True

    def loop(self):
        self.log("watchdog start pid=%s poll=%ss hang=%ss" % (os.getpid(), self.args.poll, self.args.hang_timeout))
        while True:
            try:
                self.run_once()
            except Exception as error:
                self.log("watchdog pass failed: %r" % (error,))
            time.sleep(self.args.poll)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Restart Maya when it dies or hangs.")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--version", default=None, help="Maya version for the relaunch (default: newest)")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--poll", type=float, default=5.0, help="seconds between checks")
    parser.add_argument("--hang-timeout", type=float, default=45.0, help="seconds a command may take before Maya counts as hung")
    parser.add_argument("--ready-timeout", type=float, default=120.0, help="seconds to wait for the relaunched instance to answer")
    parser.add_argument("--failures-before-restart", type=int, default=2)
    parser.add_argument("--max-restarts", type=int, default=20)
    parser.add_argument("--ensure-cmd", default=None, help="override the relaunch command line")
    parser.add_argument("--diagnose-full-screen", action="store_true", help="capture the whole desktop, not just Maya's windows")
    parser.add_argument("--diagnose-no-raise", action="store_true", help="do not bring Maya's window to the front for the shot")
    parser.add_argument("--minimized", action="store_true")
    parser.add_argument("--once", action="store_true", help="one check, then exit")
    args = parser.parse_args(argv)

    watchdog = Watchdog(args)
    if args.once:
        return 0 if watchdog.run_once() else 1
    try:
        watchdog.loop()
    except KeyboardInterrupt:
        watchdog.log("watchdog interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
