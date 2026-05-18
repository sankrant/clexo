# clexo — docs

Deep-dive documentation for clexo features. The repo [README](../README.md) is the entry
point; these pages cover individual features in detail.

| Page | What it covers |
|------|----------------|
| [auto-restore.md](auto-restore.md) | The `!clexo save → /clear → auto-restored` flow |
| [snapshots.md](snapshots.md) | What `save` captures, where files live, token budgets |
| [hooks.md](hooks.md) | `SessionStart` + `SessionEnd` — what they run and when |
| [tags.md](tags.md) | Friendly names for sessions; collisions, search, resume |
| [searching.md](searching.md) | FTS5 syntax, project / source filters, query examples |
| [picking.md](picking.md) | Drill into raw exchanges (tool outputs, file reads) |
| [cross-ai.md](cross-ai.md) | Loading a Codex session into Claude (and vice versa) |
| [architecture.md](architecture.md) | Internals — byte-offset indexing, schema, snapshot format |
| [skills.md](skills.md) | Optional Claude Code skills you can add on top of clexo |

If you spot something undocumented or stale, open an issue — the README aims for
"correct enough to scan"; these docs aim for "correct in detail."
