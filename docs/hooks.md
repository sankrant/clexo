# Hooks — `SessionStart` and `SessionEnd`

clexo uses two Claude Code hooks. Both are optional; both are strongly recommended.

## SessionStart — the auto-restore hook

**What it does:** when a new Claude Code session starts (matcher: `startup|clear`), the
hook checks for a pending snapshot. If one exists, it reads the snapshot, packs it under
Claude Code's `additionalContext` budget (~10 KB), and injects it into the new session.

**Effect:** after `!clexo save` and `/clear`, your next session is already restored.
No manual `clexo load` needed.

**Command it runs:** `python3 /path/to/server.py --session-start`

## SessionEnd — keep the index fresh

**What it does:** when a session ends, runs `python3 server.py --sync` in the
background. This appends any new messages from the just-ended session to the FTS index
so they're searchable on the next `clexo search`.

**Effect:** searches find recent material. Without this hook, you'd have to run
`clexo sync` manually before searching for something from the same day.

**Command it runs:** `bash -c 'python3 /path/to/server.py --sync >> /tmp/clexo-sync.log 2>&1 &'`

## Installation

Recommended: let the installer prompt you.

```bash
./install.sh
# answer "y" at the SessionStart + SessionEnd prompt
```

Install later if you skipped earlier:

```bash
clexo install-hooks
```

Manual: copy from `settings.json.example` into `~/.claude/settings.json`, replacing the
`/absolute/path/to/clexo` placeholder.

The installer:
- Backs up your existing `~/.claude/settings.json` before writing
- Is idempotent — skips if the hooks are already wired to the same `server.py` path
- Does NOT touch any other hook entries you've added

## Verify hooks are working

After install, restart Claude Code and run:

```bash
!clexo save                    # snapshot the throwaway session
/clear                         # clear context
                               # type a message in the new session
                               # — you should see a "Restored from..." note
                               # or summary in the assistant's reply
```

If nothing was restored, check:

```bash
cat ~/.clexo/chain-loaded                  # should hold a session ID
cat ~/.claude/settings.json | grep clexo   # the hook entries should be present
```

Enable debug logging by setting `"debug": true` in `~/.clexo/config.json` — hook output
goes to `~/.clexo/hook.log`.
