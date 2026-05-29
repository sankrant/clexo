# Searching — FTS5 across every session

clexo indexes every user message, every assistant reply, and every tool result from
both Claude Code and Codex into a single SQLite FTS5 table.

```bash
clexo search "csrf token"
clexo search "deploy AND not staging"
clexo search                                  # empty query → list recent sessions
```

## What gets indexed

| Source | Records | Path |
|--------|---------|------|
| Claude Code | `user`/`assistant` messages; `ai-title`, `custom-title`, `last-prompt` | `~/.claude/projects/**/*.jsonl` |
| Codex | `event_msg`, `response_item` | `~/.codex/sessions/**/*.jsonl` |

Tool results (bash output, file reads, etc.) are stored under the assistant message
that triggered them. `search` ranks across **all** sessions; `pick` drills **within one** session (see [picking.md](picking.md)).

## Query syntax (FTS5)

Standard SQLite FTS5 with the porter tokenizer:

```bash
clexo search "auth"                       # word match
clexo search "auth middleware"            # AND across terms
clexo search '"csrf token"'               # quoted phrase
clexo search "auth OR session"            # OR
clexo search "auth NOT staging"           # exclusion
clexo search 'auth*'                      # prefix
```

## Filters

```bash
# Restrict to a specific project
clexo search "deploy" --project_filter webapp

# "this" / "cwd" / "." → current working directory
clexo search "deploy" --project_filter this

# Restrict to one AI
clexo search "deploy" --source_filter claude
clexo search "deploy" --source_filter codex

# Cap the number of results
clexo search "deploy" --limit 3
```

Flags can appear anywhere in the command — they're pulled out before the rest is
joined into the FTS query, so `clexo search nginx config --source_filter codex`
searches for `nginx config` in Codex sessions. `--source`/`--project` are accepted
as shorthands, and `--flag=value` works too.

The MCP tool accepts the same parameters: `search(query="...", project_filter="webapp",
source_filter="claude")`.

## Output

```
3 session(s) matching 'csrf':

--- 1. 2026-04-22 [claude] | Users/alex/Code/webapp
    Opening: csrf token error on /api/checkout
    Last: shipped to staging, verified — closing
    Match: [assistant] [Bash] curl -X POST ... → 403 forbidden (>>>csrf<<< token missing)
    Session: 8f3a72b1-cd54-...
    Resume: clexo resume 8f3a72b1-cd54-...   (full session · claude --resume)
    Load:   clexo load 8f3a72b1-cd54-...   (compacted snapshot)
```

Each result shows opening/closing lines, one match snippet, and two ways back into the
session — both routed through `clexo` so you needn't remember the underlying CLI:

- `clexo resume <id>` reopens the full session. The parenthetical shows the source-specific
  command it dispatches to: `claude --resume` for Claude, `grok --resume` for Grok,
  `codex resume` for Codex.
- `clexo load <id>` injects a compacted snapshot into a fresh session instead.

If the summary isn't enough, `pick` drills into the same session for raw context.

## Empty query — list recent sessions

```bash
clexo search                  # all sources
clexo search --source_filter codex   # recent Codex sessions only
```

Useful for "what was I working on yesterday?" without remembering keywords.

## Indexing freshness

The FTS index updates on every search via byte-offset tracking — only new bytes from
each JSONL are read. The optional `SessionEnd` hook ([hooks.md](hooks.md)) keeps the
index pre-warmed.

Force a full resync:

```bash
clexo sync
```

`clexo stats` shows how many sessions and messages are indexed.
