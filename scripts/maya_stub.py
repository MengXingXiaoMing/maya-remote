#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A fake Maya command port — the test double for ``maya_ctl``.

Maya itself is heavy, needs a license and a display, so the client side is
verifiable against this: it speaks the same contract (accept a TCP connection,
evaluate one line, reply NUL-terminated), executes the payload for real, and
writes the same ``session.json`` / ``heartbeat.json`` a Maya boot would write.

    python maya_stub.py --port 7001 --state-dir "%LOCALAPPDATA%\\dsh-maya-remote"
    python maya_stub.py --hang            # accept but never execute: hang drill

Use it to exercise status/exec/ping/watchdog without starting Maya.
"""

from __future__ import print_function

import argparse
import io
import json
import os
import socket
import sys
import threading
import time
import traceback


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


class Stub(object):
    def __init__(self, args):
        self.args = args
        self.state_dir = args.state_dir
        self.connections = 0
        self.started = time.time()

    def write_session(self):
        write_json(
            os.path.join(self.state_dir, "session.json"),
            {
                "pid": os.getpid(),
                "port": self.args.port,
                "portName": ":%d" % self.args.port,
                "sourceType": self.args.source_type,
                "mayaVersion": "stub",
                "python": sys.version.split()[0],
                "scene": self.args.scene,
                "stateDir": self.state_dir,
                "exe": sys.executable,
                "startedAt": time.time(),
                "stub": True,
            },
        )

    def heartbeat(self):
        while True:
            write_json(
                os.path.join(self.state_dir, "heartbeat.json"),
                {"pid": os.getpid(), "port": self.args.port, "scene": self.args.scene, "at": time.time()},
            )
            time.sleep(2.0)

    def handle(self, connection):
        self.connections += 1
        try:
            data = b""
            while b"\n" not in data and len(data) < 1 << 20:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data += chunk
            line = data.decode("utf-8").strip()
            if not line:
                return
            if self.args.hang:
                # Accept, read, and never answer: exactly the hung-Maya drill.
                time.sleep(3600)
                return
            if self.args.source_type == "mel" and line.startswith('python("') and line.endswith('")'):
                line = line[len('python("') : -2].replace('\\"', '"')
            try:
                exec(compile(line, "<maya_stub>", "exec"), {"__name__": "__maya_stub__"})
                reply = b"stub-ok\x00"
            except BaseException:
                reply = ("stub-error: %s" % traceback.format_exc().splitlines()[-1]).encode("utf-8") + b"\x00"
            try:
                connection.sendall(reply)
            except OSError:
                pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def serve(self):
        os.makedirs(os.path.join(self.state_dir, "requests"), exist_ok=True)
        self.write_session()
        thread = threading.Thread(target=self.heartbeat)
        thread.daemon = True
        thread.start()

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", self.args.port))
        server.listen(8)
        print("[maya_stub] listening on 127.0.0.1:%d (pid %d)" % (self.args.port, os.getpid()))
        sys.stdout.flush()
        try:
            while True:
                connection, _ = server.accept()
                worker = threading.Thread(target=self.handle, args=(connection,))
                worker.daemon = True
                worker.start()
        except KeyboardInterrupt:
            pass
        finally:
            server.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fake Maya command port for tests.")
    parser.add_argument("--port", type=int, default=7001)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--source-type", choices=["python", "mel"], default="python")
    parser.add_argument("--scene", default="")
    parser.add_argument("--hang", action="store_true", help="never answer (hang drill)")
    args = parser.parse_args(argv)
    Stub(args).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
