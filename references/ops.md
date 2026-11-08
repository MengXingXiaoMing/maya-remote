# Runbooks

Paths below assume `SKILL=<this skill folder>` and `PY` is any Python 3 on the
machine (`py -3`, `python`, or an absolute interpreter path).  Every command
prints JSON and exits non-zero when `ok` is false.  Support is Windows-only:
the launcher, `taskkill`/`tasklist` and the process checks are Windows calls.

## Bring Maya up for work

```bash
$PY $SKILL/scripts/maya_ctl.py detect                      # what is installed / running
$PY $SKILL/scripts/maya_ctl.py detect --scene <file>.mb    # which version that file wants
$PY $SKILL/scripts/maya_ctl.py ensure --scene <file>.mb --minimized
$PY $SKILL/scripts/maya_ctl.py exec --code "import maya.cmds as cmds
result = {'nodes': len(cmds.ls())}"
```

`ensure` is a no-op while the recorded instance answers, so it is safe to call
before any task.  `--timeout 300` is worth it for the first cold start.

## Run a test inside Maya

```bash
$PY $SKILL/scripts/maya_ctl.py exec --file my_test.py
```

`my_test.py` runs with `maya.cmds` available; assign to `result` for structured
output, `print()` for a log.  A raised exception returns `traceback` and exits 1.
Long tests: `--timeout 600`, and expect the watchdog to leave it alone only if
the answer arrives inside `--hang-timeout`.

## Recover a hung or dead Maya

The order matters: **photograph, read, then kill.**  A "hung" Maya is often one
modal dialog away from healthy, and killing it throws that answer away.

```bash
# 1. raise the window and capture the evidence
$PY $SKILL/scripts/maya_ctl.py diagnose
#    -> {"shots": [{"path": "<state>/diagnostics/<stamp>-window.png", ...}],
#        "windows": [...], "raised": true, "foregroundAfter": {...}}
# 2. LOOK at the PNG(s).  A modal dialog, a "file not found" alert, a licence
#    prompt or a progress bar each call for a different action.
# 2.5 a dialog is not a hang: click it away and keep the session
$PY $SKILL/scripts/maya_ctl.py targets --pid <pid>
$PY $SKILL/scripts/maya_ctl.py click  --pid <pid> --title "<title>" --text Allow
# 3. only if it really is stuck: end it and start again
$PY $SKILL/scripts/maya_ctl.py kill
$PY $SKILL/scripts/maya_ctl.py ensure --scene <file>.mb
```

Add `--full-screen` when the blocker might be a window outside Maya (a licence
helper, a Windows dialog).  Add `--no-raise` when stealing focus would be rude:
the capture still works on a background window, because it renders the window
itself rather than copying the screen region where it used to be.

### Dismiss a modal dialog without killing Maya

Plugin loading pops **Untrusted Plugin Loading - Security Warning**, a
missing-reference prompt, or a licence notice.  While that modal is up the
command port accepts connections and runs nothing, so `status` reports
`portOpen:true` with a failing ping — indistinguishable from a hang, but killing
Maya there throws away the session *and* the reproduction.

```bash
# 1. what windows exist, and what could be clicked
$PY $SKILL/scripts/maya_ctl.py targets --pid <pid>
#    -> {"windows":[{"title":"...","rect":[...],"children":[...]}],
#        "clickable":[{"title":"...","text":"Allow","at":[113,216]}]}
# 2a. classic Win32 dialog: real child controls exist
$PY $SKILL/scripts/maya_ctl.py click --pid <pid> --title "Security Warning" --text Allow
# 2b. Qt dialog (Maya 2023+): clickable is empty -> read the grid, then click
$PY $SKILL/scripts/maya_ctl.py grid  --pid <pid> --title "Security Warning"
#    -> {"path":"...\\maya_grid\\<stamp>-563x440.png","origin":[1629,829],"step":50}
$PY $SKILL/scripts/maya_ctl.py click --pid <pid> --title "Security Warning" --at 60,410
# 3. prove it is answering again
$PY $SKILL/scripts/maya_ctl.py ping
```

How to read the grid PNG: thin red lines every `--step` px (default 50), a
bright double line every 5th.  The coordinate you pass to `--at` is measured
from the **window's top-left**, which is the image's top-left — no screen-offset
arithmetic.  For the security warning above, "Allow" sits near `(60, 410)`.

Safety rules the tool enforces for you:

* `--at` is refused with `point_not_on_target` when another window is under that
  point; `--force` overrides, `--dry-run` only reports.
* the window is raised first (`--no-activate` skips that); raising runs on a
  daemon thread with a timeout, so a genuinely stuck main thread cannot trap the
  tool.
* `--text` uses `SendMessageTimeout(SMTO_ABORTIFHUNG)` and falls back to a posted
  message — a synchronous `SendMessage` would wait forever on a real hang.
* the grid is a **screen** copy, not `PrintWindow`, for the same reason: a stuck
  window never repaints, and the picture is needed *before* calling it stuck.

After dismissing, `ping` should answer immediately; then continue the original
task instead of restarting Maya.

The watchdog already does step 1 for you: it calls the same capture before every
relaunch and stores the outcome in `status.json`:

```json
"lastDiagnostic": {
  "ok": true,
  "shots": ["C:\\...\\diagnostics\\20261012-194130-window.png"],
  "windowCount": 2, "raised": true, "summaryPath": "C:\\...\\<stamp>.json"
}
```

So after an unattended restart, `status.json` plus those PNGs answer "what was
on screen when it died" without anyone having watched it happen.  Read
`lastReason` alongside: `process-gone` means there was nothing to photograph;
`hung (no answer N/M)` is where the picture usually pays off.

### Retention

Pictures are pruned before every capture — 24h by default, which is what keeps
an unattended watchdog from filling the disk.

```bash
$PY $SKILL/scripts/maya_ctl.py diagnostics               # what is there, what would go
$PY $SKILL/scripts/maya_ctl.py diagnostics --ack         # the user has seen them: restart the clock
$PY $SKILL/scripts/maya_ctl.py diagnostics --keep-hours 48
$PY $SKILL/scripts/maya_ctl.py diagnostics --no-prune    # report only
$PY $SKILL/scripts/maya_snapshot_selftest.py             # prove the rules here
```

A confirmation younger than the window suspends pruning, so confirming never
destroys evidence and nothing is deleted while someone is still looking.  An
expired confirmation gets out of the way and the rolling rule resumes.  Nothing
in the toolkit ever asks for a confirmation — do not ask on its behalf either;
mention a picture's path when it matters, and call `--ack` only if the user says
they have seen it.

## Drill: crash recovery

```bash
# 1. kill Maya behind the tool's back
Get-Process maya | Stop-Process -Force
# 2. one watchdog pass: it notices, relaunches, waits for readiness
$PY $SKILL/scripts/maya_watchdog.py --once --state-dir "$env:LOCALAPPDATA\dsh-maya-remote"
# 3. prove it is back
$PY $SKILL/scripts/maya_ctl.py exec --code "result = {'ok': True}"
```

`status.json` afterwards shows `alive: true`, `restarts` incremented and
`lastReason: "process-gone"`.

## Drill: hang recovery

A real hang is a payload that never returns:

```bash
$PY $SKILL/scripts/maya_ctl.py exec --timeout 3 --code "import time
time.sleep(240)"
```

The next two watchdog passes see the port open but silent; the second reaches
`--failures-before-restart 2` and restarts Maya (`lastReason: "hung (no answer
2/2)"`).  Strikes are persisted in `status.json`, so one-shot passes accumulate
correctly.

To rehearse the whole thing without Maya:

```bash
$PY $SKILL/scripts/maya_stub.py --port 7001 --state-dir <state>          # terminal 1
$PY $SKILL/scripts/maya_ctl.py --state-dir <state> exec --code "result=1" # terminal 2
$PY $SKILL/scripts/maya_watchdog.py --once --state-dir <state> \
    --ensure-cmd "$PY $SKILL/scripts/maya_stub_ensure.py --state-dir <state>"
```

## Make the watchdog survive logout or reboot

The watchdog is an ordinary process: it dies with the session that started it.
For an unattended machine, register it as a user-level scheduled task (no admin
needed):

```bat
schtasks /Create /TN "maya-watchdog" /SC ONLOGON /RL LIMITED ^
  /TR "\"<full path to pythonw.exe>\" \"<skill folder>\scripts\maya_watchdog.py\" --state-dir \"%LOCALAPPDATA%\dsh-maya-remote\" --poll 10"
```

Run it with `pythonw.exe` instead of `python.exe` if a console window would be
unwelcome — the same launcher-plus-Task-Scheduler pattern this machine already
uses for its game server.

## When nothing answers

| file in the state dir | what it tells you |
| --- | --- |
| `session.json` | pid, port, `sourceType`, Maya version — written the moment the port opens |
| `heartbeat.json` | freshness (`at`) and the current scene path, rewritten every 2s by a thread that makes no Maya calls |
| `status.json` | the watchdog's verdict: `alive`, `restarts`, `consecutiveFailures`, `lastReason` |
| `maya_boot.log` | the in-Maya boot: which `commandPort` spelling worked, or why it failed |
| `boot_error.json` | present only when the boot script could not open the port |
| `watchdog.log` | every relaunch decision and its `ensure` result |
| `requests/` | each payload (`*.code.py`, `*.runner.py`) — rerun one by hand with `references/command-port.md` |

Fast triage:

```bash
$PY $SKILL/scripts/maya_ctl.py status          # pidAlive / portOpen / ping
```

* `pidAlive:false` → it crashed or was killed; `ensure` (or the watchdog) brings it back.
* `pidAlive:true, portOpen:false` → Maya is up but the port is gone (the boot
  script failed, or something closed it).  Restart, do not wait.
* `portOpen:true, ping failing` → the main thread is blocked: a modal dialog is
  the usual cause, `time.sleep` the artificial one.  Run `targets`; a dialog is
  dismissed with `click`, not with a restart.

## Anti-patterns

* **A second Maya while one runs** — one license seat; kill first.
* **Killing when a dialog is up** — `portOpen:true` with a failing ping is
  usually a modal waiting for a click.  `targets` then `click` keeps the session;
  a restart throws away unsaved work for nothing.
* **Killing without saving** — the watchdog reopens the recorded *file*, never
  unsaved edits.  Save through `exec` before a deliberate restart.
* **Reading a `maya_timeout` as "my code failed"** — it means no answer arrived;
  check `traceback` for real script errors instead.
* **`mayapy` for the port** — it cannot serve one (see `command-port.md`).
* **Assuming the first `ensure` is broken because it is slow** — a cold Maya can
  take a minute before the main thread is free to answer.
