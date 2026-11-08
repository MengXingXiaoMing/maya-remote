# The Maya command port

Maya exposes a TCP command port from inside its own interpreter:

```python
import maya.cmds as cmds
cmds.commandPort(name=":7001", sourceType="python")
```

Anything that can reach that loopback port can run code in Maya — there is no
authentication. Treat the port as equal to "can run programs as this user".

## The wire

* One command per connection, terminated by `\n`.
* Maya writes back the evaluated result, then `\n`, then a NUL byte. Read until
  NUL or your own timeout.
* `sourceType="python"` evaluates the payload as Python; without it the payload
  is MEL. `python("...")` is how MEL reaches Python.
* `print()` output does **not** reliably come back on the socket — it goes to
  Maya's Output Window. Only the expression's value is returned (e.g. `1+1` →
  `2`, `print('x')` → `None`).

Because of that last point this toolkit never parses the reply for results.  It
writes a payload file, sends one short `exec(...)` line, and the payload writes
its own JSON result file.  The socket only has to deliver one line.

## Traps that cost real time

> **The one that bites hardest: a bare expression payload executes twice.**
> Maya evaluates a payload for its return value *and* then runs it, so a
> side-effecting expression like `exec(compile(...))` applies its effects
> **twice** — one `polyCube` payload created two cubes before this was caught.
> Make the payload a *statement* instead: `_ = exec(...)`, `if True: exec(...)`
> and `runpy.run_path(...)` each ran exactly once in the same test.  Measured on
> Maya 2024 by having the payload append a line to a file and counting lines.
> Idempotent payloads (queries, pings) hide this completely, which is why it
> survived a full day of testing.

**1. Separate globals and locals.**  Maya evaluates the payload with distinct
globals and locals mappings.  Inside such a payload, `exec(compile(src...))`
inherits both, so the payload's module-level names land in *locals* while its
own functions look them up in *globals*:

```
name 'RESULT_PATH' is not defined
```

Pass an explicit namespace and the problem cannot occur:

```python
exec(compile(src, 'maya_req', 'exec'), {'__name__': '__maya_remote_runner__'})
```

**2. The `sourceType` flag has spellings.**  `sourceType="python"` is accepted on
current builds; some builds want `stp="python"`; a bare call opens a MEL port.
`maya_boot.py` tries them in order and records which one worked in
`session.json`, and the client wraps the payload in `python("...")` when the port
ended up MEL.

**3. Maya 2018/2019 are Python 2.7.**  No f-strings, no `os.replace`, and text
files want `io.open(..., encoding="utf-8")`.  `maya_boot.py` and the generated
runner are written for both.

**4. `mayapy` cannot open the port.**  `maya.standalone.initialize()` +
`cmds.commandPort` fails with `RuntimeError: Maya command error`: the port is an
application feature.  Test the client against `maya_stub.py` instead of mayapy.

**5. The port opens before Maya is ready.**  The boot script runs from Maya's
`-command`, early in startup.  The socket accepts immediately, but the main
thread is still loading plugins, so the first answer can take a minute.  That is
why `ensure` waits with a long ping budget and why the watchdog counts strikes
instead of restarting on the first slow answer.

**6. Commands run on Maya's main thread.**  A payload that blocks (a modal
dialog, `time.sleep`, a huge scene operation) stops every later command from
executing.  The port stays open, connections succeed, nothing comes back — the
signature of a hung Maya, and what `--hang-timeout` measures.

**8. `-command` runs before the message system exists.**  Opening the
`commandPort` there is fine, but calling `cmds.scriptJob(...)` at that moment
makes **Maya 2025.2 exit silently about ten seconds later** — no crash log, no
Windows error event, just a dead pid right after the port looked healthy.  The
boot script therefore defers the `scriptJob` registration through
`maya.utils.executeDeferred`, and keeps Maya API calls off the heartbeat thread
(`cmds.file` from a worker is the same class of mistake).

## Scene version detection

`.ma` files state it twice (`//Maya ASCII 2024 scene`, `requires maya "2024"`).
`.mb` files are binary, but the header block carries `HEADVERS.` followed by the
version — `version_from_scene()` checks the requires line, the ascii header, then
`HEADVERS`, and only then falls back to any four-digit year.
