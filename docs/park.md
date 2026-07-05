# `clexo park` — Usage-Limit Recovery

Sleep without losing your task. `clexo park` saves your session and wakes
you — or your machine — when Claude's usage limits have reset.

## The problem

Claude Code enforces a rolling usage limit (typically 5 hours on Pro). When
it hits, the session stops. If you want to continue working overnight, you
have to remember to manually resume in the morning and hope you recall where
you left off.

## The solution

```
clexo park [note]
```

Run this right before sleeping (or right when the limit hits). clexo:

1. Records the current session ID and your optional note
2. Forks a background daemon that waits, then probes
3. When limits reset, fires a macOS notification and opens a new Terminal
   window with `claude --resume <session_id>` already running

---

## Commands

### `clexo park [note]`

```bash
clexo park "calculus chapter 4 — mid-edit"
```

- Reads `$CLAUDE_CODE_SESSION_ID`; falls back to the most recent session in
  the clexo index if the env var is unset
- Probes immediately with `claude --print "."` to detect the current limit
  state and **parse the exact reset time from the error message**
- Writes `~/.clexo/park.json` with session ID, note, and computed
  `wait_seconds`
- Forks `clexo _wake-daemon` as a detached background process (stdout/stderr
  to `/dev/null`, new session group so it survives terminal close)
- Prints confirmation + `clexo unpark` hint

If parsing fails (error message format unrecognised), falls back to **5.5 hours**
from now.

### `clexo unpark`

```bash
clexo unpark
```

Deletes `~/.clexo/park.json`. The background daemon checks the file at each
probe iteration and exits silently when it finds it gone — so cancellation
takes effect within 30 minutes at most.

---

## Park file

`~/.clexo/park.json` — written by `park`, consumed and deleted by the daemon.

```json
{
  "session_id": "abc12345-...",
  "source": "claude",
  "note": "calculus chapter 4 — mid-edit",
  "parked_at": "2026-06-07T00:10:00Z",
  "wait_seconds": 13320
}
```

If you run `clexo park` again while a park is active, the file is overwritten
and the old daemon exits silently on its next check.

---

## Wake daemon (`clexo _wake-daemon`)

Internal subcommand, not shown in help. Runs entirely in the background.

### Algorithm

```
1. Read ~/.clexo/park.json — exit if missing
2. sleep(wait_seconds)
3. Re-read park.json — exit if gone or session_id changed (unparked / re-parked)
4. Probe loop (up to 7 attempts, 30 min apart):
     a. Run: claude --print "." (capture stdout+stderr, timeout 60s)
     b. If output contains "usage limit" or "rate limit" → still blocked
        → sleep 30 min, retry
     c. Otherwise (clean exit OR unrecognised error) → assume reset, break
     After 7 probes (~3.5 extra hours), fire anyway — don't loop forever
5. Delete park.json
6. osascript: fire macOS notification
     title:    "clexo"
     subtitle: "Session <8-char-id>… · <note>"
     body:     "Usage limits reset — resuming your task"
7. osascript: open Terminal and run `clexo resume <session_id>`
8. osascript: activate Terminal (bring to front)
9. Append to ~/.clexo/wake.log
```

### Parsing the reset time (step 1 of `park`)

When Claude Code hits the usage limit it prints a message that includes the
reset time. `park` does an immediate probe and extracts the wait duration:

| Pattern tried | Example match |
|---|---|
| `"try again in X hours Y minutes"` | `try again in 3 hours 42 minutes` |
| `"try again in X minutes"` | `try again in 47 minutes` |
| `"resets at HH:MM"` | `resets at 04:00` |
| ISO timestamp after `"after "` | `try again after 2026-06-07T04:00:00Z` |

Patterns are tried in order; first match wins. A small buffer (60 seconds) is
added to avoid racing the reset. Falls back to 5.5 hours if nothing matches.

The exact pattern set will be refined once the real error message format is
confirmed (see **TODO** below).

---

## Log

`~/.clexo/wake.log` — append-only, one line per event:

```
[2026-06-07T00:10:05] wake daemon started: session=abc12345, wait=13320s
[2026-06-07T03:42:05] limits confirmed reset (probe 1)
[2026-06-07T03:42:07] wake fired: clexo resume abc12345-...
```

Useful if you wake up and the Terminal didn't open (permissions, machine
slept through the window, etc.) — you can run the resume command manually.

---

## Platform notes

- **macOS only** for notifications and Terminal launch (`osascript`)
- If `osascript` fails (not on macOS, or permission denied), the wake is
  still logged; the session can be resumed manually via `clexo resume`
- The daemon survives terminal close (`start_new_session=True`) but not a
  machine shutdown — if the machine powers off, the daemon is lost (the
  park.json file persists, so you can see what was parked)

---

## Code changes

| Location | Change |
|---|---|
| `cli.py` top constants | `PARK_FILE = CLEXO_DIR / "park.json"` |
| `cli.py` before `_dispatch()` | `_probe_limits_reset()`, `_parse_limit_reset_time()`, `_cmd_park()`, `_cmd_wake_daemon()`, `_cmd_unpark()` |
| `cli.py` `_dispatch()` | `elif "--park"`, `elif "--unpark"`, `elif "--wake-daemon"` branches |
| `cli.py` `main()` `SUBCMD` | `"park": "--park"`, `"unpark": "--unpark"` |
| `cli.py` `_USAGE` | `park` and `unpark` entries |

---

## TODO

- [ ] Confirm exact Claude Code usage-limit error message format and tighten
      `_parse_limit_reset_time()` regex patterns accordingly
- [ ] Test probe on a real limit hit
- [ ] Consider Linux notification support (`notify-send`) as a future addition
