# Cross-AI — Codex ↔ Claude

clexo indexes Claude Code and Codex sessions into one searchable archive. You can find,
load, and pick from either AI's history regardless of which AI you're currently in.

## What "one archive" means

- `~/.clexo/index.db` holds messages from both sources.
- `search` returns matches from both unless you filter (`--source_filter claude` or
  `--source_filter codex`).
- `load <tag>` restores a Codex session's snapshot when you're in Claude, and vice
  versa. The snapshot format is source-agnostic.

## Use cases

**You debugged something in Codex, now you're in Claude and need the same context:**
```bash
clexo search "redis connection timeout" --source_filter codex
clexo load <session-id>
```

**You want a single archive of "everything I asked an AI about Webapp this month":**
```bash
clexo search "" --project_filter webapp
```

**You're mid-task in Codex and switching to Claude for a different model strength:**
```bash
# in Codex
!clexo save
!clexo tag deploying-feature-x

# switch terminals, open Claude Code
clexo load deploying-feature-x
```

## What doesn't carry across

The snapshot carries the *gist*, not the *transcript*. Tool outputs from the original
session aren't auto-replayed; the new AI doesn't see every bash output the old one did.
For specific raw output, use `pick` — it works the same across sources.

Authentication state, API keys, model-specific settings — none of these are part of
the snapshot. The new AI is the same AI it was before; only the conversation context
crosses over.

## Behind the scenes

| Source | Source path | Source format |
|--------|-------------|---------------|
| Claude Code | `~/.claude/projects/**/*.jsonl` | one JSON object per line |
| Codex | `~/.codex/sessions/**/*.jsonl` | one JSON object per line |

Both are parsed into a common message shape and stored together in `messages` (FTS5).
The `source` column on each row tracks which AI it came from; the `tag → session_id`
mapping is source-agnostic.

There's nothing magic — Codex and Claude both write transcripts to disk in JSONL, so
clexo can read both. The unification is at the index layer, not the protocol layer.
