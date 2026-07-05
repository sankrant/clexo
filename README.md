<h1 align="center">clexo</h1>

<p align="center">
  <strong>Session memory and cross-AI context for Claude Code and Codex</strong><br/>
  <em>Claude Code forgets. clexo remembers.</em>
</p>

<p align="center">
  <a href="#-why">Why</a> •
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-the-three-claude-code-operations-clexo-replaces">vs /compact /clear /resume</a> •
  <a href="#-cli">CLI</a> •
  <a href="#-mcp-tools">MCP</a> •
  <a href="#-how-it-works">How it works</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"/>
  <img src="https://img.shields.io/badge/python-3.10+-3776AB.svg" alt="Python"/>
  <img src="https://img.shields.io/badge/MCP-ready-7C3AED.svg" alt="MCP"/>
  <img src="https://img.shields.io/badge/Claude_Code-supported-D77757.svg" alt="Claude Code"/>
  <img src="https://img.shields.io/badge/Codex-supported-1e293b.svg" alt="Codex"/>
  <img src="https://img.shields.io/badge/zero-daemon-22c55e.svg" alt="Zero daemon"/>
</p>

---

> **Inside a Claude Code session, type `!clexo save`. Then `/clear`.**
> The next session auto-restores the snapshot — summary + memory preserved, raw context cleared.
> No `/compact` wait. No `claude --resume` reloading the full history. No `/clear` loss.

<p align="center">
  <img src="docs/demo.gif" alt="clexo save → /clear → auto-restored next session" width="720"/>
</p>

---

## ✨ Why

If you live in Claude Code or Codex, three context operations all hurt. clexo replaces all three.

|   | The pain | clexo replacement |
|---|----------|-------------------|
| 🐢 | **`/compact`** — 1-3 minutes on long sessions, blocks you in-session | `!clexo save` — ~80 ms snapshot, then `/clear` and continue |
| 💸 | **`claude --resume <id>`** — re-loads the entire history into context | `clexo load <tag>` — restores the compact snapshot only |
| 🪦 | **`/clear`** — irreversible, loses everything | `/clear` after `!clexo save` — auto-restored on next session |

Plus one structural gap nothing else closes: **Codex doesn't see Claude's history; Claude doesn't see Codex's.** clexo indexes both into one archive — load a Codex session into Claude, or vice versa.

Zero daemon. No API key. Self-indexing on demand. Your AI sessions become a searchable meta-memory across every prior conversation.

---

## 🚀 Quick Start

```bash
pipx install git+https://github.com/sankrant/clexo
clexo install
```

`pipx` installs clexo into an isolated environment and puts the `clexo` command on your PATH — no system-Python pollution, and no "python3 too old", since pipx picks a suitable interpreter for you. `clexo install` then wires it into Claude Code: it registers the MCP server (`clexo serve`) and installs the SessionStart + SessionEnd hooks that enable auto-restore. Both are idempotent and safe to re-run.

> No `pipx`? Add it with `brew install pipx` (macOS) or `python3 -m pip install --user pipx`. Prefer [`uv`](https://docs.astral.sh/uv/)? `uv tool install git+https://github.com/sankrant/clexo`. Working from a local checkout? `git clone … && cd clexo && ./install.sh` runs the same two steps.

### Try it

```bash
# Inside a Claude Code session, drop a save and clear cleanly:
!clexo save            # snapshot the current session (~80 ms)
/clear                 # standard Claude Code; the next session auto-restores

# From any terminal:
clexo search "csrf token"        # FTS across every session, ever
clexo tag auth-fix               # name the current session
clexo load auth-fix              # launch a fresh claude, snapshot restored via hook
clexo resume auth-fix            # or reopen the original session (claude --resume; full rehydrate)
clexo stats                      # how many tokens you've saved so far
```

---

## 🔁 The three Claude Code operations clexo replaces

### `/compact` → `!clexo save`

`/compact` re-summarises the entire conversation in place. On a long session it can take minutes — you sit and wait. `!clexo save` writes a compact snapshot to disk in milliseconds. You can `/clear` immediately and the next session auto-restores it.

The `!` prefix matters: it runs `clexo save` as a bash command directly, bypassing the model entirely. Zero tokens consumed, no MCP round-trip, no AI cost — the fastest possible save. (You can also ask the agent to use the `save` MCP tool; that works but costs model tokens.)

### `claude --resume <uuid>` → `clexo load <tag>`

`claude --resume` rehydrates the *full* saved conversation back into context — every message, every tool call, every file read, up to the model's context window (200K on Sonnet, 1M on Opus). On a long session that's a slow rehydrate and your full context budget consumed before the first new turn. `clexo load` restores the saved snapshot (summary + recent exchanges + key file refs) — typically a few thousand tokens. Same continuity, a fraction of the context.

### `/clear` → `/clear` (after `!clexo save`)

`/clear` is normally irreversible. After `!clexo save`, it isn't: the SessionStart hook reads the pending snapshot when the next session starts and injects it as additional context. You keep summary + memory; you only lose the verbose raw history.

---

## 🧰 What it does

- **Search** every Claude Code and Codex conversation you've ever had (FTS5)
- **`save`** the current session into a compact snapshot, **`load`** it later — context survives `/clear` and crosses between Claude and Codex
- **`pick`** raw exchanges (including bash output and file reads) from any past session
- **`tag`** sessions with friendly names — `clexo resume my-auth-fix` jumps straight back into `claude --resume <uuid>`
- **Zero daemon** — self-indexing via byte-offset tracking; one optional `SessionEnd` hook keeps the index fresh

---

## ⚙️ Manual install

`clexo install` does the Claude Code wiring for you. To do it by hand instead:

```bash
# 1. Install the package (isolated)
pipx install .            # from a checkout — or: pip install .

# 2. Register the MCP server with Claude Code
claude mcp add --scope user clexo clexo serve

# 3. (Recommended) install the hooks — enables auto-restore after /clear
clexo install-hooks
#    or merge the hooks block from settings.json.example into
#    ~/.claude/settings.json manually
```

Verify the MCP server with `claude mcp list` — you should see `clexo: clexo serve ✓ Connected`.

### Upgrading

```bash
pipx install --force git+https://github.com/sankrant/clexo   # or: pipx upgrade clexo
clexo install                                                # re-points hooks + MCP if needed
```

Upgrading from an older git-clone install? Same two commands — `clexo install` re-points the old `server.py`-based hooks and MCP registration to the `clexo` command (backing up `settings.json` first) and removes the stale `~/.local/bin/clexo` wrapper symlink.

---

## 💻 CLI

```
clexo stats                          Show usage stats
clexo sync                           Index new messages now
clexo search <query>                 Search chat history
clexo save [sid|tag]                 Snapshot the current (or given) session
clexo saved [--short]                List saved snapshots, newest first, with the
                                     id fragment to reload each

clexo tag <name> [--force] [sid]     Tag the current (or given) session
clexo tags [--short|--keywords]      List tags, newest first (--short: name+date)
clexo untag <name>                   Remove a tag
clexo load <name|sid>                Set pending snapshot and launch a fresh claude
                                     (SessionStart hook injects the snapshot)
clexo resume <name|sid>              Exec 'claude --resume <uuid>' — reopens the
                                     original session, full history (no snapshot)
clexo resume                         (no args) Interactive picker over recent
                                     sessions; choose resume / load mode
clexo show <name|sid>                Print the saved snapshot to stdout (inspect only)

clexo install                        Wire MCP server + hooks into Claude Code
                                     (re-runnable; re-points an older install)
clexo install-hooks                  Wire just the SessionStart + SessionEnd hooks
                                     (idempotent; backs up settings.json first)
clexo serve                          Run the MCP server (Claude Code invokes this)
```

`load` vs `resume`: `load` is the clexo path — fresh session, compact snapshot, cheap context. `resume` is a friendly-name wrapper around `claude --resume <uuid>` — same session, full rehydrate, no clexo summarization.

All commands work from anywhere — `!clexo tag my-fix` inside a Claude session tags that session.

---

## 🔌 MCP tools

When clexo is registered as an MCP server, Claude can invoke these directly. You usually don't call them manually — just say "search my history for X", "load my last session", "tag this as auth-fix".

| Tool | What it does |
|------|--------------|
| `search` | FTS search across all sessions (filters: `project_filter`, `source_filter='claude'\|'codex'`, `pwd=true` to scope to the current directory). `sort='time'` displays results oldest-first. Empty query = list recent. |
| `load` | Load a session's snapshot (summary + recent exchanges) into context. Accepts UUID or tag. |
| `save` | Snapshot the current session for restore on the next start. |
| `pick` | Drill into a session's raw exchanges (incl. tool output). FTS-anchored; supports `before`/`after` scroll. Accepts UUID or tag. |
| `tag` | Assign a friendly name to a session. Collisions return a "exists, pass `replace=True` or pick a new name" prompt. |
| `tags` | List all tags (newest first) with each session's summary and opening/closing lines. `short=True` for just name+date; `keywords=True` to add TF-IDF keywords. |
| `untag` | Remove a tag mapping. |
| `get_stats` | Usage counters. |

---

## 🛠️ How it works

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
