---
name: maya-remote
description: Use when a task needs Autodesk Maya on this Windows machine — opening it, deciding which installed version to open, driving it from outside the app (running MEL/Python, inspecting or changing a scene, running tests), or keeping it alive so a crash or a hang recovers without a human. Covers launch, the command-port protocol, the state files other agents read, and the watchdog.
---

# Maya remote control

Maya 2018 / 2019 / 2023 / 2024 / 2025 are installed under
`C:\Program Files\Autodesk\Maya<year>`.  The scripts here start one, open Maya's
`commandPort`, run code inside it, and restart it when it dies or hangs.

Everything prints one JSON object, so callers parse instead of scraping.
State lives in `%LOCALAPPDATA%\dsh-maya-remote` (override with `--state-dir`).

Scripts (run with any Python 3 — `py -3`, `python`, or a full path):

| file | role |
| --- | --- |
| `scripts/maya_ctl.py` | the caller's whole interface: detect, ensure, status, exec, ping, restart, kill, watchdog |
| `scripts/maya_boot.py` | runs **inside** Maya at startup; opens the port and writes the state files |
| `scripts/maya_watchdog.py` | the restart loop for a crash or a hang |
| `scripts/maya_snapshot.py` | raise Maya's window and photograph it before anyone kills it |
| `scripts/maya_click.py` | list a dialog's controls, screenshot it with a coordinate grid, click it |
| `scripts/maya_stub.py` | fake command port: exercise the client, the watchdog and the drills without Maya |

## Normal use

```bash
python maya_ctl.py detect                     # installed versions, running pids
python maya_ctl.py ensure                     # start if needed, newest version, wait for the port
python maya_ctl.py exec --code "import maya.cmds as cmds
result = {'meshes': len(cmds.ls(type='mesh'))}"
python maya_ctl.py exec --file test_scene.py  # a whole script, result in `result`
python maya_ctl.py status                     # pid, port, ping, scene
python maya_ctl.py restart                    # kill and relaunch, reopening the recorded scene
python maya_ctl.py kill
```

`ensure --scene <file>` opens that scene and, unless `--version` says otherwise,
picks the version the file was written with (`.ma` and `.mb` both carry it;
`detect --scene <file>` reports the choice without launching anything).
`--minimized` starts Maya minimized instead of taking the screen.

Exit code is 0 when `ok` is true.  Inside `exec`, assign to `result` to return a
value; `print()` output comes back as `stdout`.  An exception comes back as
`ok:false`, `error:"python_exception"` and a `traceback` — the command did reach
Maya, so `maya_unreachable` and `maya_timeout` are the only transport failures.

## Keeping it alive

```bash
python maya_ctl.py watchdog --start          # background loop, writes status.json
python maya_ctl.py watchdog --status
python maya_ctl.py watchdog --stop
python maya_watchdog.py --once               # one pass: check, restart if needed, exit
```

The watchdog treats a **gone process** as immediate grounds to relaunch and a
**missing answer** as a hang that needs `--failures-before-restart` (default 2)
strikes, because a long command also stops answering.  Every pass writes
`status.json`; any agent can read that one file to learn whether Maya is up, how
many restarts happened, and the last reason.

## Constraints

- **Nothing starts by itself.** `ensure` and `watchdog --start` are explicit
  calls; the loop only ever reacts to an instance that already exists.
- **One license seat.** Starting a second Maya while one runs can fail or steal
  the seat.  `ensure` is a no-op when the recorded instance answers; use
  `--force` only after `kill`.
- **Killing loses unsaved work.** The watchdog reopens the *scene file* that was
  recorded in `heartbeat.json`, but edits never written to disk are gone.  Save
  (`cmds.file(save=True)`) before a deliberate restart.
- **`exec` runs on Maya's main thread.** A command that blocks freezes the UI
  and stops the port answering — that is what the hang rule is for.
- **The port is loopback-only** and speaks plain unauthenticated Python: treat
  anything that can reach `127.0.0.1` as able to run code in this Maya.
- Maya 2018/2019 run Python 2.7; 2023+ run Python 3.  Code sent with `exec`
  must fit the target version — `maya_boot.py` and the generated runner do.

## When it hangs or dies: look first, then kill

```bash
python maya_ctl.py diagnose              # raise Maya, capture its windows, report
python maya_ctl.py diagnose --full-screen   # also photograph the whole desktop
python maya_ctl.py diagnose --no-raise      # capture without stealing focus
```

`diagnose` writes PNGs under `<state>/diagnostics/` and returns their paths, the
title and rectangle of every visible window of the process, and which window
held the foreground before and after.  Every window is captured, not just the
biggest, because the usual cause of a "hung" Maya is a **modal dialog** — and
the picture says which one, so the recovery can be "dismiss it" instead of
"kill it".  A crash has no window: the report then says so, with an empty
`shots` list.

### Dismiss the dialog instead of killing Maya

Plugin loading pops a security warning, a missing-file prompt or a licence
notice; the port stops answering while that modal is open, which looks exactly
like a hang.  Click it away and carry on:

```bash
python maya_ctl.py targets --pid 47976                     # windows + clickable controls
python maya_ctl.py click --pid 47976 --title "Security Warning" --text Allow
python maya_ctl.py grid  --pid 47976 --title "Security Warning"   # PNG with a 50px grid
python maya_ctl.py click --pid 47976 --title "Security Warning" --at 113,216
```

Two dialog families, two mechanisms:

- **classic Win32 dialogs** expose real child controls, so `--text Allow`
  finds the button and sends `BM_CLICK` — no pointer movement, and the send has
  a timeout so a genuinely hung target cannot trap the tool.
- **Qt dialogs** (Maya 2023+ draws its own) have no child controls: `targets`
  returns an empty `clickable`, so read the `grid` PNG and pass the pixel
  position with `--at`.  Coordinates are **relative to the window's top-left**,
  which is exactly what the grid image shows.

Safety: `--at` is refused when another window is under that point
(`point_not_on_target`), unless `--force`; `--dry-run` reports without clicking;
`--no-activate` skips raising the window.  Both files stay small and stdlib-only.

The watchdog runs the diagnose step **automatically before every relaunch**, and
records the result in `status.json` as `lastDiagnostic` (`shots`, `windowCount`,
`raised`, `notes`, `summaryPath`).  Read the picture before accepting a restart,
and treat `lastReason` as the diagnosis hint:

| `lastReason` | what the evidence usually shows |
| --- | --- |
| `process-gone` | nothing to photograph — it really did die |
| `hung (no answer N/M)` | often a dialog or a long operation; the screenshot decides |

Before killing anything on `hung`, run `targets` and, if a dialog is there,
`click` it: a dismissed dialog leaves the session and the reproduction intact.

`--diagnose-full-screen` and `--diagnose-no-raise` pass through to that step.

### Retention of that evidence

Pictures are pruned automatically before every capture, so an unattended
watchdog cannot fill the disk: by default only the last **24 hours** survive.

```bash
python maya_ctl.py diagnostics               # what is there, and what would go
python maya_ctl.py diagnostics --ack         # the user has seen them: restart the clock
python maya_ctl.py diagnostics --keep-hours 48
python maya_ctl.py diagnostics --no-prune    # report without deleting
```

A confirmation younger than the window **suspends pruning** — the countdown
restarts from it — so confirming never destroys evidence, and nothing is
deleted while somebody is still looking at it.  Once the confirmation itself is
older than the window, the plain rolling rule resumes.

**Never ask the user to confirm.**  The tool never prompts, and neither should
you: capture, carry on, and mention a picture's path only when it matters.  Call
`--ack` when the user says they have seen the pictures — or not at all; the
window handles it either way.  `scripts/maya_snapshot_selftest.py` proves these
rules on any machine (`python maya_snapshot_selftest.py`).

## Handing it to someone else

Self-contained: no absolute paths, no DSH dependency — copy the folder anywhere
and run it from there (verified from a copied folder, a different working
directory and a fresh state directory).  The target machine needs:

- **Windows.**  The launcher, `taskkill`/`tasklist` and the process checks are
  Win32 calls.
- **Maya installed**, by default under `C:\Program Files\Autodesk\Maya<year>`;
  another root works with `--root`.
- **A Python 3 to run the client** — an ordinary install, or `mayapy.exe` from
  Maya 2023+ (verified).  Maya 2018/2019 ship Python 2.7: `detect` runs there,
  the rest is unsupported.

Verified end-to-end on **Maya 2025 only**.  2018/2019 are Python 2.7 and were
never exercised; the boot script does fall back across `commandPort` flag
spellings and to a MEL wrapper, but treat older versions as untested.

As a DSH skill the folder belongs in `~/.dsh/skills/` (**with the filesystem
skill provider enabled** — `skill-filesystem` is off in a fresh profile, so the
folder is invisible until it is on); as a Trae skill it lives in
`~/.trae-cn/skills/maya-remote` and is registered in `skill-config.json`.
Either way the folder still stands alone:
`SKILL.md`, `references/command-port.md` and `references/ops.md` are plain
instructions for a human or any other agent.

## References

- `references/command-port.md` — the wire protocol and the traps that cost real
  time: reply framing, the separate globals/locals namespace, `sourceType`
  spellings, Python 2.7 payloads, Windows encoding.
- `references/ops.md` — runbooks: bring Maya up for work, run a test, dismiss a
  modal dialog, drill the crash and hang paths, make the watchdog survive a
  reboot, and where to look when something does not answer.
