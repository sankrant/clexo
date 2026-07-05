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

# Display the same results oldest-first, so the latest match lands last
clexo search "deploy" -t
clexo search "deploy" --time

# Scope to the current working directory (sessions started here)
clexo search "deploy" --pwd

# …and override a configured pwd-default to search everywhere
clexo search "deploy" --all
```

`--pwd` matches the directory exactly (the cwd recorded when each session ran), so
it's narrower than `--project_filter this`, which fuzzy-matches the project name. To
make pwd-scoping the default for every search, set it in `~/.clexo/config.json`:

```json
{ "default_search_scope": "pwd" }
```

With that set, `clexo search` only looks in the current directory unless you pass
`--all`. The MCP `search` tool takes the same scope via its `pwd` argument
(`true`/`false`, or omit to follow the configured default).

Whenever a search is directory-scoped, the result is never silently lossy — it tells
you how much you're not seeing and how to widen:

```
1 session · "nginx" in ~/Code/clexo
...
+2 more in other directories · use --all to include them
```

If the cwd has no match but other directories do, the "no results" line says so:
`No results for 'nginx' in ~/Code/clexo — but 2 in other directories. Use --all to
search everywhere.` (In the MCP tool, "use --all" means call `search` again with
`pwd=false`.)

Flags can appear anywhere in the command — they're pulled out before the rest is
joined into the FTS query, so `clexo search nginx config --source_filter codex`
searches for `nginx config` in Codex sessions. `--source`/`--project` are accepted
as shorthands, and `--flag=value` works too.

The MCP tool accepts the same parameters: `search(query="...", project_filter="webapp",
source_filter="claude", pwd=true)`.

## Ranking

Results blend text relevance (FTS5 BM25) with recency, weighted evenly — a
session from today competes on equal footing with an old one that repeats the
term more densely, rather than losing to it on relevance alone. Ranking
happens per session, not per matching message, so one chatty session can't
occupy every result slot and crowd other matching sessions out of the
results.

`-t` / `--time` doesn't change which sessions are selected — it just displays
them in ascending chronological order instead, oldest first, so the most
recent match lands last (the bottom of the terminal, right where the
`resume`/`load` legend already points). The MCP tool takes the same switch via
`sort="time"` (default `"relevance"`).

## Output

```
3 sessions · "csrf"

1.  2026-04-22  claude  ~/Code/webapp                              8f3a72b1
    open  csrf token error on /api/checkout
    last  shipped to staging, verified — closing
    hit   [Bash] curl -X POST ... → 403 forbidden («csrf» token missing)

→ resume: clexo resume 8f3a72b1    load: clexo load 8f3a72b1
```

Each result is a card: a header (number, date, source, project path, session id),
the session's `open`ing and `last` user message, and the `hit` — the snippet that
matched, with the term highlighted (bold in a terminal, «guillemets» when piped).
When the match would just restate the opening line, the `hit` is omitted; use
`--full` to always show it.

Each header ends with a short 8-char id. That's a fragment, not the full UUID —
but `clexo resume`/`load`/`show` resolve any unambiguous id prefix, so you pass
the 8 chars straight through. One legend at the bottom shows both ways back,
pre-filled with the **last** result's id so it's a runnable example:

- `clexo resume <id>` reopens the full session, dispatching to `claude --resume`
  (Claude), `grok --resume` (Grok), or `codex resume` (Codex).
- `clexo load <id>` injects a compacted snapshot into a fresh session instead.

Both route through `clexo` so you needn't remember the underlying CLI. `--full`
prints the complete UUID in each header if you ever need the canonical id.

### Layouts

```bash
clexo search "csrf"            # cards (default)
clexo search "csrf" --oneline  # one aligned row per result (scan many fast)
clexo search "csrf" --full     # open + last + every match snippet, full ids
```

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
