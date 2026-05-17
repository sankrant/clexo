# clexo

**Session memory and cross-AI context for Claude Code and Codex.** Search every past session, save and restore context across `/clear`, retrieve specific tool outputs from any prior conversation, and bookmark sessions with friendly names.

Built by [Sankrant](https://github.com/sankrant).

---

## What it does

- **Search** every Claude Code and Codex conversation you've ever had (FTS5)
- **`save`** the current session into a compact snapshot and **`load`** it later — context survives `/clear` and crosses between Claude and Codex
- **`pick`** raw exchanges (including bash output and file reads) from any past session
- **`tag`** sessions with friendly names — `clexo resume my-auth-fix` jumps straight back into `claude --resume <uuid>`
- **Zero daemon** — self-indexing via byte-offset tracking; one optional `SessionEnd` hook keeps things fresh

---

## Quick start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

(Only `mcp` is required at runtime. Python 3.10+.)

### 2. Register the MCP server with Claude Code

```bash
claude mcp add --scope user clexo python3 /absolute/path/to/clexo/server.py
```

Verify with `claude mcp list` — you should see `clexo: ... ✓ Connected`.

### 3. Put `clexo` on your PATH (optional, for the CLI)

```bash
ln -s /absolute/path/to/clexo/clexo ~/.local/bin/clexo
```

### 4. Add the SessionStart + SessionEnd hooks (optional, recommended)

See `settings.json.example` — merge the hooks block into your `~/.claude/settings.json` (Claude Code's settings file, not this project's config). The `SessionStart` hook auto-restores the last saved snapshot; `SessionEnd` keeps the FTS index current.

---

## CLI

```
clexo stats                          Show usage stats
clexo sync                           Index new messages now
clexo search <query>                 Search chat history
clexo save [sid|tag]                 Snapshot the current (or given) session

clexo tag <name> [--force] [sid]     Tag the current (or given) session
clexo tags                           List tags with summary + keywords
clexo untag <name>                   Remove a tag
clexo load <name|sid>                Load a saved snapshot by tag or UUID
clexo resume <name|sid>              Exec 'claude --resume <uuid>' (resolves tag first)
```

All commands work from anywhere — `!clexo tag my-fix` inside a Claude session tags that session.

---

## MCP tools

When clexo is registered as an MCP server, Claude can invoke these directly. You usually don't call them manually — just say "search my history for X", "load my last session", "tag this as auth-fix".

| Tool | What it does |
|------|--------------|
| `search` | FTS search across all sessions (filters: `project_filter`, `source_filter='claude'\|'codex'`). Empty query = list recent. |
| `load` | Load a session's snapshot (summary + recent exchanges) into context. Accepts UUID or tag. |
| `save` | Snapshot the current session for restore on the next start. |
| `pick` | Drill into a session's raw exchanges (incl. tool output). FTS-anchored; supports `before`/`after` scroll. Accepts UUID or tag. |
| `tag` | Assign a friendly name to a session. Collisions return a "exists, pass `replace=True` or pick a new name" prompt. |
| `tags` | List all tags with each session's summary, opening/closing lines, and TF-IDF keywords. |
| `untag` | Remove a tag mapping. |
| `get_stats` | Usage counters. |

---

## How it works

- **Indexing** — SQLite FTS5 (porter tokenizer). Byte-offset tracking per JSONL file means syncs are O(new bytes), not O(file size). New messages are picked up on the next search; the optional `SessionEnd` hook runs `--sync` in the background.
- **Source files** —
  - Claude Code: `~/.claude/projects/**/*.jsonl` (`user`/`assistant` messages; `ai-title`, `custom-title`, `last-prompt` records)
  - Codex: `~/.codex/sessions/**/*.jsonl` (`event_msg`, `response_item`)
- **Snapshots** — `save` writes `~/.clexo/chain-<sid>.md` containing the summary, key file refs, and the most recent N tokens of exchanges. The `SessionStart` hook reads the pending snapshot, packs it under Claude Code's 10K hook context cap, and injects it as `additionalContext`.
- **Tags** — small `tags` table mapping `tag → session_id`. One session can have many tags; tag names are `[a-z0-9_-]`, normalized to lowercase, and can't look like a UUID. Wherever a UUID is accepted (`load`, `pick`, `save`, `resume`), a tag works too.
- **Keywords in `tags` listing** — TF-IDF over each session's messages: user text weighted 3×, raw count threshold 2 (filters typos/one-offs), IDF computed against the full corpus and cached across the listing.

---

## Configuration

`~/.clexo/config.json` (created on first use):

```json
{
  "refresh_tokens_min": 4000,
  "refresh_tokens_max": 8000
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `refresh_tokens_min` | 4000 | Minimum token budget for `save`'s exchange window |
| `refresh_tokens_max` | 8000 | Maximum token budget (cap) |
| `debug` | `false` | If `true`, write hook + sync diagnostics to `~/.clexo/hook.log` |

Tokens are approximated at 4 chars/token.

---

## Tests

```bash
pip install pytest
pytest tests/
```

---

## License

MIT — see [LICENSE](LICENSE).
