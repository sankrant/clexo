# Architecture — internals

clexo is a single-file Python program (`server.py`) that runs in two modes:

- **MCP server** — when invoked by Claude Code via its MCP transport
- **CLI** — when invoked from a shell (via the `clexo` wrapper script)

Both modes share the same code paths and the same SQLite database
(`~/.clexo/index.db`).

## Data flow

```
                                       ┌────────────────────────┐
~/.claude/projects/**/*.jsonl  ────┐    │                        │
                                   ├──→ │   server.py --sync     │ ──→ ~/.clexo/index.db
~/.codex/sessions/**/*.jsonl   ────┘    │   (byte-offset tail)   │     (FTS5 + tags)
                                       └────────────────────────┘

                              save           ┌─────────────────────┐
   Claude Code  ─── !clexo save ──────────→  │   server.py --save  │ ──→ ~/.clexo/chain-<sid>.md
                                              └─────────────────────┘     + writes chain-loaded

                          /clear, new session
                                  │
                                  ▼
                       ┌─────────────────────────────┐
                       │ SessionStart hook fires:    │
                       │ python3 server.py           │
                       │   --session-start           │
                       │ → reads chain-loaded        │
                       │ → emits additionalContext   │
                       └─────────────────────────────┘
```

## Indexing — byte-offset tracking

Every JSONL session file's last-read byte position is stored in `file_state`:

```sql
CREATE TABLE file_state (
  path        TEXT PRIMARY KEY,
  last_size   INTEGER,    -- byte offset last consumed
  mtime       REAL
);
```

`sync` reads each file from `last_size` to current EOF — appending only the new lines.
This means re-indexing a 10 MB session file after 50 KB of new messages takes a few ms,
not seconds.

## Durable transcript archive

Claude Code deletes session JSONLs after `cleanupPeriodDays` (default 30). Everything
that reads a session at retrieval time — `pick`, snapshot generation, `load` — opens
the live file, so without intervention old sessions silently stop being recallable.

clexo keeps its own compact copy. On every sync, alongside indexing, it writes a
gzipped transcript per session to `~/.clexo/archive/<source>/<uuid>.jsonl.gz`
(`_write_archive`). The transcript keeps every user/assistant turn and every tool
**command**, but drops tool **output**, thinking, images, snapshots and per-line
metadata — that bulk is either in git or re-run on current data, so it's dead weight
for recall. The result is ~10× smaller than the raw JSONL and stays in the source's
native shape, so the existing readers parse it unchanged. Sessions are archived when
they end (the SessionEnd hook runs an unthrottled sync) and backfilled lazily: a sync
that skips an already-indexed file still writes its archive if one is missing, which
also self-heals a deleted archive.

This is for clexo's own recall (`pick`, `load`, snapshot rebuild) — **not** for
`claude --resume`, which needs the verbatim file Claude owns.

### The reader gate

All of this hinges on one rule: **writers take direct paths, readers go through a
gate.**

- **Writers** (`_sync_claude`, `_sync_codex`, `_write_archive`) glob the live source
  tree directly — they're building the index/archive *from* live files.
- **Readers** resolve sessions through `_find_session_jsonl(session_id, source)`, which
  returns the live file if it exists, else materializes the archive into
  `~/.clexo/cache/` and returns that. The archive fallback lives in exactly this one
  function, so `pick`, `refresh_save`, `load` and `show` all inherit it for free.
- **Resume** is the one path that needs the *real* live file (`_live_session_jsonl`,
  no fallback). `clexo resume` on a reaped session degrades to `clexo load` —
  rebuilding the snapshot from the archive into a fresh session — instead of failing.

## Schema

```sql
sessions(session_id, project, first_user_msg, last_ts, cwd, source,
         thread_name, summary, last_prompt, prefix_delta_tokens)
messages(...)  -- FTS5 virtual table, porter tokenizer
tags(name, session_id, created_at)
stats(key, value)
file_state(path, last_size, mtime)
```

`prefix_delta_input_tokens` and `prefix_delta_cache_tokens` are non-zero only on
sessions that were started from a clexo snapshot. The SessionStart hook reads the
*source* session's JSONL, finds the last `type: "assistant"` line, and pulls its
`message.usage` block — `input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`. That's the real API prefix size the source session was
about to replay. The snapshot replaces it, so per-turn savings split into:

- **input delta** = `source.input_tokens` (the non-cached input the source session would
  have paid each turn)
- **cache delta** = `source.cache_read + source.cache_creation`

The one-time cost of reading the snapshot itself is already accounted for in
`tokens_compacted` (computed at save time), so we don't deduct it again here.

Both deltas are stamped onto the new session and `sync_claude` credits them per
indexed assistant turn into `input_tokens_saved` / `cache_tokens_saved` in `stats`.

`tokens_compacted` uses the same source: at save time we compute
`max(0, (source.input + source.cache_read + source.cache_creation) − snapshot_tokens)`
— the one-shot prefix reduction the snapshot represents. For Codex sessions (no
`usage` field in their event log) we fall back to extracted-text length / 4.

### What `clexo gain` shows

```
Tokens compacted:     N  (one-shot at save time)
Input tokens saved:   N  (M loads × ~T turns × ~D tokens/turn)
Cache tokens saved:   N  (M loads × ~T turns × ~D tokens/turn)
```

- **Tokens compacted** — sum of per-save `(source_prefix − snapshot)` deltas.
- **Input tokens saved** — `Σ prefix_delta_input × assistant_turns_after_load` across
  all loaded sessions. These are the *fresh* (non-cached) input tokens the snapshot
  eliminated — the part you'd have paid full input rate for.
- **Cache tokens saved** — same math, but for cache reads + cache creation. These were
  going to be served from Anthropic's prompt cache (much cheaper than fresh input), so
  the dollar savings are smaller than the raw count suggests — but the context-window
  pressure was real.

The breakdown decomposes each total: `M loads` = sessions with non-zero delta of that
kind, `~T turns` = implied average (`total_saved / Σ delta`), `~D tokens/turn` =
average per-load delta. All values come from Claude Code's own `usage` accounting —
the same numbers it sends with every API call — not from disk-file size heuristics.

FTS5 internals (`messages_data`, `messages_idx`, `messages_content`, etc.) are
auto-generated by SQLite.

## Snapshot format

A snapshot is a markdown file (`~/.clexo/chain-<sid>.md`) with three sections:

```markdown
## Summary
<auto-generated summary of the conversation>

## Key files
<file paths the agent has touched>

## Recent exchanges
<user / assistant turns, trimmed to fit the token budget>
```

The hook reads this file, packs it under Claude Code's `additionalContext` budget
(~10 KB), and injects it on the next `SessionStart`.

## MCP server

`server.py` implements the [MCP](https://modelcontextprotocol.io/) protocol over stdio.
Tools exposed:

| Tool | What it does |
|------|--------------|
| `search` | FTS over all sessions |
| `load` | Restore a snapshot by UUID or tag |
| `save` | Snapshot the current session |
| `pick` | Drill into a session's raw exchanges |
| `tag` / `tags` / `untag` | Tag management |
| `get_stats` | Usage counters |

## Why no daemon

clexo runs as needed (CLI command, hook invocation, MCP request) and exits. There's no
background process, no PID file, no socket, no service to install or maintain. The cost
is that searches occasionally have to sync new bytes — but that's amortized down to a
few ms after the first sync.
