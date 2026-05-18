# Picking — raw exchange drill-in within a session

The key distinction:

- **`search`** is **global** — full-text across every session indexed (Claude + Codex).
- **`pick`** is **scoped** — drills into a single session's chained history (the current loaded chain by default, or whichever session you point it at).

```bash
clexo pick "csrf token"                              # within the current chain
clexo pick "csrf token" --session_id auth-fix        # within a specific session (tag)
clexo pick "csrf token" --session_id 8f3a72b1-...    # or by UUID
```

Pick returns the raw exchange that matched, including tool outputs (bash, file reads,
edit results) — the things a summary loses.

## When to use pick vs search vs load

| Tool | Scope | Returns | Best for |
|------|-------|---------|----------|
| `search` | All sessions, ever | Session list with one-line snippets | "Which session was this in?" |
| `load`   | One session | Snapshot (summary + recent + key files) | "I want to continue the work" |
| `pick`   | One session (current chain by default) | Specific raw exchange(s) | "I need the exact bash output / file content from this conversation" |

`pick` does not pull a session into the current context wholesale — it's surgical
retrieval within one conversation, designed to keep your context budget small.

## Scrolling

```bash
clexo pick "csrf token" --session_id auth-fix             # first match
clexo pick "csrf token" --session_id auth-fix --after 1   # next match
clexo pick "csrf token" --session_id auth-fix --before 1  # previous match
clexo pick "" --session_id auth-fix --after 5             # 5 exchanges past the start
```

Or anchor at a specific position (signed offset from match):

```bash
clexo pick "csrf token" --session_id auth-fix --offset +3
```

## MCP form

When invoked via the MCP server, `pick` takes the same arguments:

```
pick(args="csrf token", session_id="auth-fix", after=1)
```

Without `session_id`, `pick` falls back to the most recent loaded session — useful
when you've just run `load` and want to dig further.

## Typical flow

1. `clexo search "csrf"` — pinpoints session `auth-fix`
2. `clexo load auth-fix` — restores summary into a new working session
3. `clexo pick "the exact 403 error" --session_id auth-fix` — fetches the raw response
   body when you need to compare against the current bug

The summary tells you the gist; `pick` gives you the receipts.
