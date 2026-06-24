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

## Same-directory safeguard

The *automatic* restore only fires when the new session starts in the **same directory**
as the saved one. Save in one project, then start your next session in an unrelated
project, and the snapshot is *deferred* (you get a brief "Auto-restore deferred" note)
instead of bleeding the first project's context into the second. The pending snapshot
stays put — go back to the original directory and it restores.

This guard applies only to the *automatic* restore. When you explicitly run
`clexo load <fragment-or-tag>`, that's a deliberate choice, so it always loads — even in
a different directory. The banner then notes the directory it came from
(`↳ from ~/Code/projA (loaded here)`) so the cross-directory continuation is visible.

To turn the guard off for automatic restores too, set `"autoload_cwd_guard": false` in
`~/.clexo/config.json`.

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
`~/.claude/settings.json`. `clexo install` wires it up; you can also (re)install just
the hooks later with `clexo install-hooks`.

Without the hook, `clexo save` still writes the snapshot — you just have to run
`clexo load <sid-or-tag>` manually after the new session starts to inject it.

See [hooks.md](hooks.md) for details on what the hook does and how to verify it's wired.
