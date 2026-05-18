# Auto-restore — `!clexo save` → `/clear` → continue

The headline workflow. End-to-end:

```
!clexo save     # inside a running Claude Code session
                #   writes ~/.clexo/chain-<sid>.md
                #   marks that snapshot as "pending restore"
                #   ~80 ms; no model tokens consumed

/clear          # standard Claude Code; raw context wiped

                # Claude Code starts a fresh session and fires the
                # SessionStart hook. The hook reads the pending snapshot,
                # injects it as `additionalContext`.

                # Your next message arrives with the summary, recent
                # exchanges, and key file refs already in context —
                # but the raw context cap is fresh.
```

The result: your session continues seamlessly with summary + memory preserved, but
without the full multi-megabyte transcript weighing down the context window.

## What you give up vs. `claude --resume`

`claude --resume <uuid>` re-loads the **entire** session — every message, every tool
call, every file read, every bash output. Total fidelity.

`!clexo save → /clear` gives you the snapshot only — summary, recent exchanges, key
file refs. Lossier; orders of magnitude faster and cheaper.

The right tool depends on what you need:
- **Need older bash output or file content** from earlier in the session? Use `clexo pick "<query>"` to fetch the specific exchange. (See [picking.md](picking.md).)
- **Need to be back in the exact same Claude Code session?** Use `claude --resume <uuid>` (or `clexo resume <tag>`). Slow rehydrate, full fidelity.
- **Want to continue the work but not relive every token?** `!clexo save → /clear`. The 95% case.

## Prerequisites

This flow requires the `SessionStart` hook to be installed in
`~/.claude/settings.json`. The `install.sh` script prompts to install it; you can also
install it later with `clexo install-hooks`.

Without the hook, `clexo save` still writes the snapshot — you just have to run
`clexo load <sid-or-tag>` manually after the new session starts to inject it.

See [hooks.md](hooks.md) for details on what the hook does and how to verify it's wired.
