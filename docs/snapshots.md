# Snapshots — what `save` captures, where it lives

## What gets captured

When you run `!clexo save` (or `clexo save`), clexo writes a compact snapshot of the
current session that contains:

- A **summary** of the conversation so far (auto-generated)
- **Key file references** the agent has touched
- The **most recent N tokens** of exchanges (user + assistant turns), trimmed to fit a
  budget — default 4000–8000 tokens (configurable, see below)

The snapshot is intentionally small: it is the **continuity payload**, not the full
transcript. The full transcript is already on disk in Claude Code's or Codex's session
file; clexo reads from there when you need older detail (via `pick`). Once Claude
reaps that file (after ~30 days), clexo transparently falls back to its own gzipped
transcript archive — so `pick`, `load` and snapshot rebuilds keep working. See
[architecture.md](architecture.md#durable-transcript-archive).

## Where the files live

clexo creates one data directory:

```
~/.clexo/
├── index.db                              # SQLite FTS5 index of all messages
├── chain-<session_id>.md                 # snapshots (one file per saved session)
├── chain-loaded                          # pointer to the snapshot pending restore
├── config.json                           # user config (token budgets, debug flag)
└── hook.log                              # written only if "debug": true
```

Source session files (read-only — clexo never writes here):

| AI | Path |
|----|------|
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| Codex | `~/.codex/sessions/**/*.jsonl` |

A typical snapshot is 5–20 KB — small enough to be lossless to load instantly.

## What `save` prints

```
$ !clexo save
Wrote snapshot: 14,961 chars ≈ 3.7K tokens
Compacted from ~205K msg tokens (98% smaller)
Tagged 'clexo-improve-gain'
Run /clear — snapshot auto-restores on next message.
```

The "Compacted from …" line is dropped on tiny sessions where the snapshot skeleton
(headers, file refs) makes the snapshot no smaller than the source. The "Tagged …"
line only appears the first time a session is saved — subsequent saves on the same
session reuse its existing tag rather than creating new ones. See
[tags.md](tags.md#auto-tags) for the naming rules.

## Picking a session to restore

When you don't remember a tag or UUID, run `clexo resume` with no argument.
Symmetric with `claude --resume` (which also shows a picker when invoked
without an id), but with clexo's index of tags, timestamps, and project
context — plus an option to load via snapshot instead of full resume:

```
$ clexo resume
Recent sessions — pick one to resume:

   1. improve-clexo-gain         today 16:11    clexo                   · Improve clexo gain function
   2. coach                      yesterday      Code                    · Coaching session setup
   3. 3323483d…                  3d ago         Webapp                 · fix auth bug in checkout
   …

Mode: [r] resume full session  [s] load snapshot  [q] quit
> 1 s
```

Selection format is `N` (defaults to `r` = full session) or `N r` / `N s` for an
explicit mode. `r` execs `claude --resume <uuid>` — reopens the original session
with full history. If that session has already been reaped by Claude, `r` degrades
to `s` automatically (it loads clexo's archived snapshot into a fresh session rather
than failing). `s` writes the snapshot to `REFRESH_PENDING` and execs a fresh
`claude` so the SessionStart hook injects the snapshot — same path as `clexo
load`. `q` or empty input quits without doing anything.

Codex rows are marked `[codex]`; selecting one in `r` mode execs `codex resume
<id>` (Codex's own full-resume command) rather than `claude --resume`. Grok rows
exec `grok --resume <id>`. The right binary for the session's source is always
chosen automatically.

## The fast path: `!clexo save`

Inside a Claude Code session, `!` is the bash escape — it runs a shell command directly,
**bypassing the model entirely**. `!clexo save` is therefore the fastest possible save:
no MCP round-trip, no model tokens consumed, no AI cost. It writes the snapshot from
local data alone.

Three equivalent ways to save the current session:

| How | Speed | Cost |
|-----|-------|------|
| `!clexo save` (inside Claude Code) | ~80 ms | $0 — no model tokens |
| `clexo save` (separate terminal) | ~80 ms | $0 |
| Ask the agent to use the `save` MCP tool | ~1–3 s | model tokens |

Use the MCP tool if you're already in a conversation and want the agent to remember the
save itself. Otherwise the `!` form is always preferred.

## Token budget

`~/.clexo/config.json`:

```json
{
  "refresh_tokens_min": 4000,
  "refresh_tokens_max": 8000
}
```

- `refresh_tokens_min` — minimum size of the exchange window included in the snapshot
- `refresh_tokens_max` — cap; `save` will trim older exchanges to fit

Tokens are approximated at 4 chars / token. Raise the cap if you have very long sessions
and want more history carried across `/clear`; lower it if your hook context budget is
tight (Claude Code currently injects up to ~10 KB via `additionalContext`, so a snapshot
much larger than that gets trimmed at hook time anyway).
