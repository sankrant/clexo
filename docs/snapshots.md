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
file; clexo reads from there when you need older detail (via `pick`).

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
