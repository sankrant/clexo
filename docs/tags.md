# Tags — friendly names for sessions

Sessions on disk are identified by UUID. Tags give you a human-readable handle.

```bash
clexo tag auth-fix                     # tag the current session
clexo tag auth-fix <session-uuid>      # tag a specific session
clexo tag auth-fix --force             # replace an existing tag

clexo tags                             # list all tags with summary + keywords
clexo untag auth-fix                   # remove a tag

clexo load auth-fix                    # load the snapshot (snapshot, not full session)
clexo resume auth-fix                  # exec `claude --resume <uuid>` — full session
clexo pick "csrf token" \              # drill into a tagged session
  --session_id auth-fix
```

Tag names: `[a-z0-9_-]`, lowercase. Anything that looks like a UUID is rejected (to keep
tag-or-uuid resolution unambiguous).

## How `load <name>` resolves

When you run `clexo load <name>`, clexo tries:

1. **As a tag** — looks up `<name>` in the tags table; if found, uses the mapped UUID.
2. **As a UUID prefix** — if `<name>` looks UUID-shaped, accepts it directly.
3. **Otherwise** — errors with a hint to run `clexo tags`.

If the snapshot doesn't yet exist for the resolved session, `load` writes one
on the fly (running an implicit `save`), then proceeds.

## Tags table

Tags live in `~/.clexo/index.db` in the `tags` table:

```sql
CREATE TABLE tags (
  name        TEXT PRIMARY KEY,    -- the friendly name
  session_id  TEXT NOT NULL,       -- target UUID
  created_at  TEXT NOT NULL
);
```

One session can have many tags. Each tag points to exactly one session.

## Collisions

If you try `clexo tag <name>` and the tag already exists:

```
Tag 'auth-fix' already exists (→ 8f3a... in -Users-alex-Code).
Pass --force to replace, or pick a different name.
```

`--force` (or, when invoked via MCP, `replace=True`) overwrites the existing mapping.
The old session is untagged but not deleted — the snapshot and index are untouched.

## `clexo tags` output

```
3 tag(s):

@auth-fix  →  8f3a72b1-...  [claude] Users/alex/Code/webapp  (last: 2026-05-15)
    Title: Fix CSRF in auth middleware
    Opening: csrf token error on /api/checkout
    Last: shipped to staging, verified — closing
    Keywords: csrf, middleware, token, webapp, checkout, session, cookie

@deploy-debug  →  ...
```

Keywords are TF-IDF over the session's messages (user text weighted 3×). They're a
quick "what was this about" signal when you've forgotten which tag was which.

## When to tag

Some patterns that work well:

- **At end of a meaningful session** — `!clexo tag csrf-debug` before `/clear`.
- **When you know you'll return** — long-running feature work, multi-day debugging.
- **Right after a breakthrough** — the conversation where you figured something out.

You don't need to tag everything. Tags are for sessions you specifically want to find
again by name; FTS search ([searching.md](searching.md)) covers the long tail.
