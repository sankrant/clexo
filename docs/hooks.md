# Hooks — `SessionStart` and `SessionEnd`

clexo uses two Claude Code hooks. Both are optional; both are strongly recommended.

## SessionStart — the auto-restore hook

**What it does:** when a new Claude Code session starts (matcher: `startup|clear`), the
hook checks for a pending snapshot. If one exists, it reads the snapshot, packs it under
Claude Code's `additionalContext` budget (~10 KB), and injects it into the new session.

**Effect:** after `!clexo save` and `/clear`, your next session is already restored.
No manual `clexo load` needed.

**Command it runs:** `clexo session-start`

## SessionEnd — keep the index fresh

**What it does:** when a session ends, runs `clexo sync` in the
background. This appends any new messages from the just-ended session to the FTS index
so they're searchable on the next `clexo search`.

**Effect:** searches find recent material. Without this hook, you'd have to run
`clexo sync` manually before searching for something from the same day.

**Command it runs:** `bash -c 'clexo sync >> /tmp/clexo-sync.log 2>&1 &'`

## Installation

Recommended: `clexo install` wires both hooks (and the MCP server) in one step.

```bash
clexo install
```

Hooks only — e.g. if you skipped them, or to re-point an older install:

```bash
clexo install-hooks
```

Manual: copy the hooks block from `settings.json.example` into
`~/.claude/settings.json`. The commands assume `clexo` is on your PATH (pipx/uv put it there).

The installer:
- Backs up your existing `~/.claude/settings.json` before writing
- Is idempotent — skips if the hooks are already wired to the `clexo` command (and
  re-points an older `server.py`-based hook in place)
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
