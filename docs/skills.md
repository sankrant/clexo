# Optional Claude Code skills

[Skills](https://docs.anthropic.com/en/docs/claude-code/skills) in Claude Code are
small, reusable prompt patterns that the user (or the agent) can invoke. clexo doesn't
bundle any by default — but the `!clexo …` and `clexo …` commands compose nicely with
a few skills you can add yourself.

> **Heads-up.** Earlier builds of clexo bundled a small set of these as part of the
> installer; we removed them so the install stays minimal and skills remain entirely
> user-controlled. The patterns below are starter templates — drop them into
> `~/.claude/skills/` and Claude Code picks them up.

## Pattern 1 — `/refresh` (save + clear in one move)

If `!clexo save` followed by `/clear` is something you do often, wrap it as a skill.

`~/.claude/skills/refresh.md`:

```markdown
---
name: refresh
description: Snapshot the current session with clexo, then clear context.
---

Run the following two commands in order:

1. `!clexo save` — write the snapshot
2. `/clear` — wipe context (auto-restored on next session via SessionStart hook)
```

Invoke with `/refresh` mid-session.

## Pattern 2 — `/pin <tag>` (save + tag + clear)

For sessions you want to come back to by name:

`~/.claude/skills/pin.md`:

```markdown
---
name: pin
description: Snapshot the current session, tag it with the provided name, then /clear.
args: <tag-name>
---

Steps:
1. `!clexo save`
2. `!clexo tag <tag-name>`
3. `/clear`

The session is now reachable later via `clexo resume <tag-name>`.
```

## Pattern 3 — `/recall <query>` (search + load)

A two-step recall that finds the right session and loads it:

`~/.claude/skills/recall.md`:

```markdown
---
name: recall
description: Search past sessions by query, then load the top hit.
args: <search-query>
---

1. Use the `clexo search` MCP tool with the query.
2. If exactly one session matches, use `clexo load` on its session_id.
3. If multiple match, list them with one-line summaries and ask which to load.
```

## Why these aren't built in

Three reasons:
- Skills are personal — the trigger names, args, and behaviour vary by user preference.
- Bundling them would silently modify `~/.claude/skills/`, which is your space.
- The clexo CLI / MCP server is enough; skills are sugar on top.

If you build a skill on top of clexo that you think others would want, open an issue or
PR — happy to link out to a community gallery as the project grows.
