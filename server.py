#!/usr/bin/env python3
"""
Claude Code + Codex chat search MCP server.

Indexes:
  ~/.claude/projects/**/*.jsonl   (Claude Code sessions)
  ~/.codex/sessions/**/*.jsonl    (Codex sessions)

Self-updates via byte-offset tracking on every search call.
SessionEnd hook also calls: python server.py --sync

Run as MCP server : python server.py
Run sync only     : python server.py --sync
"""

import datetime
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

CLEXO_DIR          = Path.home() / ".clexo"

DB_PATH            = CLEXO_DIR / "index.db"
CLAUDE_PROJECTS    = Path.home() / ".claude" / "projects"
CODEX_SESSIONS     = Path.home() / ".codex" / "sessions"
CODEX_SESSION_IDX  = Path.home() / ".codex" / "session_index.jsonl"
REFRESH_PENDING    = CLEXO_DIR / "refresh-pending"

_UUID_RE      = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_CHAIN_RE     = re.compile(r'^## Session ([a-f0-9-]{36})', re.MULTILINE)
_CHAIN_LOADED = CLEXO_DIR / "chain-loaded"

# Tag validation — friendly names that can't collide with raw UUIDs.
_TAG_RE      = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_TAG_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

_loaded_session_id: str = ""


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5(
            session_id UNINDEXED,
            project    UNINDEXED,
            role       UNINDEXED,
            content,
            ts         UNINDEXED,
            tokenize='porter unicode61'
        );
        CREATE TABLE IF NOT EXISTS file_state (
            filepath    TEXT PRIMARY KEY,
            last_offset INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sessions (
            session_id     TEXT PRIMARY KEY,
            project        TEXT,
            first_user_msg TEXT,
            last_ts        TEXT,
            cwd            TEXT,
            source         TEXT DEFAULT 'claude',
            thread_name    TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS stats (
            key   TEXT PRIMARY KEY,
            value INTEGER DEFAULT 0
        );
        -- Friendly-name → session_id map. Many tags per session allowed; tag is unique PK.
        CREATE TABLE IF NOT EXISTS tags (
            tag        TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            created_ts TEXT
        );
        CREATE INDEX IF NOT EXISTS tags_session_idx ON tags(session_id);
    """)
    # Migrate existing sessions table if columns are missing
    existing = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    for col, dflt in [("source", "'claude'"), ("thread_name", "''"), ("summary", "NULL"), ("last_prompt", "''")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT DEFAULT {dflt}")
    conn.commit()
    return conn


# ── text extraction ───────────────────────────────────────────────────────────

def _extract_claude_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "text":
                parts.append(block.get("text", ""))
            elif t == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                vals = " ".join(str(v)[:200] for v in inp.values() if isinstance(v, str)) if isinstance(inp, dict) else str(inp)[:200]
                parts.append(f"[{name}] {vals}")
        return " ".join(parts)
    return ""


# ── incremental line reader ───────────────────────────────────────────────────

def _read_new_lines(jsonl_file: Path, last_offset: int):
    """Yields (line_bytes, offset_after_this_line) for each complete new line."""
    with open(jsonl_file, "rb") as f:
        f.seek(last_offset)
        raw = f.read()
    parts = raw.split(b"\n")
    complete = parts if raw.endswith(b"\n") else parts[:-1]
    offset = last_offset
    for line in complete:
        offset += len(line) + 1
        line = line.strip()
        if line:
            yield line, offset


# ── Claude Code sync ──────────────────────────────────────────────────────────

def _sync_claude(conn) -> int:
    total = 0
    for jsonl_file in sorted(CLAUDE_PROJECTS.glob("*/*.jsonl")):
        try:
            stat = jsonl_file.stat()
            fp = str(jsonl_file)
            row = conn.execute("SELECT last_offset FROM file_state WHERE filepath=?", [fp]).fetchone()
            last_offset = row[0] if row else 0
            if stat.st_size <= last_offset:
                continue

            session_id = jsonl_file.stem
            project    = jsonl_file.parent.name
            new_offset = last_offset

            for raw_line, new_offset in _read_new_lines(jsonl_file, last_offset):
                try:
                    obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type")
                ts = obj.get("timestamp", "")

                if msg_type in ("user", "assistant"):
                    msg  = obj.get("message", {})
                    role = msg.get("role", msg_type)
                    text = _extract_claude_text(msg.get("content", ""))
                    if text.strip():
                        conn.execute(
                            "INSERT INTO messages(session_id,project,role,content,ts) VALUES(?,?,?,?,?)",
                            [session_id, project, role, text, ts]
                        )
                        total += 1
                        conn.execute("""
                            INSERT INTO sessions(session_id,project,first_user_msg,last_ts,cwd,source,thread_name,last_prompt)
                            VALUES(?,?,?,?,?,'claude','','')
                            ON CONFLICT(session_id) DO UPDATE SET
                                last_ts        = MAX(last_ts, excluded.last_ts),
                                first_user_msg = CASE
                                    WHEN excluded.first_user_msg != '' AND (first_user_msg IS NULL OR first_user_msg = '' OR first_user_msg LIKE '<%')
                                    THEN excluded.first_user_msg
                                    ELSE first_user_msg
                                END,
                                cwd            = COALESCE(cwd, excluded.cwd)
                        """, [
                            session_id, project,
                            text[:200] if (role == "user" and not _is_noise(text)) else "",
                            ts,
                            obj.get("cwd", "") if msg_type == "user" else "",
                        ])

                elif msg_type == "ai-title":
                    title = obj.get("aiTitle", "")
                    if title:
                        conn.execute("""UPDATE sessions SET thread_name=?
                            WHERE session_id=? AND (thread_name IS NULL OR thread_name='')""",
                            [title, session_id])

                elif msg_type == "custom-title":
                    title = obj.get("customTitle", "")
                    if title:
                        conn.execute("UPDATE sessions SET thread_name=? WHERE session_id=?",
                            [title, session_id])

                elif msg_type == "last-prompt":
                    lp = obj.get("lastPrompt", "")
                    if lp and not _is_noise(lp):
                        conn.execute("UPDATE sessions SET last_prompt=? WHERE session_id=?",
                            [lp[:200], session_id])

            conn.execute("INSERT OR REPLACE INTO file_state VALUES(?,?)", [fp, new_offset])
        except Exception as e:
            if _debug_enabled():
                print(f"[clexo sync claude] {jsonl_file}: {e}", file=sys.stderr)
            continue

    conn.commit()
    return total


# ── Codex sync ────────────────────────────────────────────────────────────────

def _load_codex_thread_names() -> dict:
    names = {}
    if not CODEX_SESSION_IDX.exists():
        return names
    with open(CODEX_SESSION_IDX) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("id"):
                    names[obj["id"]] = obj.get("thread_name", "")
            except Exception:
                pass
    return names


def _sync_codex(conn) -> int:
    if not CODEX_SESSIONS.exists():
        return 0

    thread_names = _load_codex_thread_names()
    total = 0

    for jsonl_file in sorted(CODEX_SESSIONS.glob("**/*.jsonl")):
        try:
            stat = jsonl_file.stat()
            fp = f"codex:{jsonl_file}"
            row = conn.execute("SELECT last_offset FROM file_state WHERE filepath=?", [fp]).fetchone()
            last_offset = row[0] if row else 0
            if stat.st_size <= last_offset:
                continue

            # Extract UUID from filename: rollout-YYYY-MM-DDThh-mm-ss-{uuid}.jsonl
            m = _UUID_RE.search(jsonl_file.stem)
            if not m:
                continue
            session_id  = m.group(0)
            thread_name = thread_names.get(session_id, "")
            cwd         = ""
            project     = ""
            new_offset  = last_offset

            # Grab cwd from session_meta on first index of this file
            if last_offset == 0:
                with open(jsonl_file, "rb") as f:
                    head = f.read(3000)
                for ln in head.split(b"\n")[:15]:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        obj = json.loads(ln.decode("utf-8", errors="replace"))
                        if obj.get("type") == "session_meta":
                            pl  = obj.get("payload", {})
                            cwd = pl.get("cwd", "")
                            break
                    except Exception:
                        pass

            project = Path(cwd).name if cwd else "codex"

            for raw_line, new_offset in _read_new_lines(jsonl_file, last_offset):
                try:
                    obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type")
                ts       = obj.get("timestamp", "")
                payload  = obj.get("payload", {})

                # User: event_msg / user_message — skip IDE context injections
                if msg_type == "event_msg" and payload.get("type") == "user_message":
                    text = payload.get("message", "")
                    if not text or text.startswith("# Context from my IDE"):
                        continue
                    conn.execute(
                        "INSERT INTO messages(session_id,project,role,content,ts) VALUES(?,?,?,?,?)",
                        [session_id, project, "user", text, ts]
                    )
                    total += 1
                    conn.execute("""
                        INSERT INTO sessions(session_id,project,first_user_msg,last_ts,cwd,source,thread_name)
                        VALUES(?,?,?,?,?,'codex',?)
                        ON CONFLICT(session_id) DO UPDATE SET
                            last_ts        = MAX(last_ts, excluded.last_ts),
                            first_user_msg = COALESCE(first_user_msg,
                                CASE WHEN excluded.first_user_msg != '' THEN excluded.first_user_msg END),
                            cwd            = COALESCE(cwd, excluded.cwd),
                            thread_name    = COALESCE(NULLIF(thread_name,''), excluded.thread_name)
                    """, [session_id, project, text[:200], ts, cwd, thread_name])

                # Assistant: response_item role=assistant, content[].type=output_text
                elif msg_type == "response_item" and payload.get("role") == "assistant":
                    for block in payload.get("content", []):
                        if block.get("type") == "output_text":
                            text = block.get("text", "")
                            if text.strip():
                                conn.execute(
                                    "INSERT INTO messages(session_id,project,role,content,ts) VALUES(?,?,?,?,?)",
                                    [session_id, project, "assistant", text, ts]
                                )
                                total += 1
                                conn.execute("""
                                    INSERT INTO sessions(session_id,project,first_user_msg,last_ts,cwd,source,thread_name)
                                    VALUES(?,?,''  ,?,?,'codex',?)
                                    ON CONFLICT(session_id) DO UPDATE SET
                                        last_ts     = MAX(last_ts, excluded.last_ts),
                                        thread_name = COALESCE(NULLIF(thread_name,''), excluded.thread_name)
                                """, [session_id, project, ts, cwd, thread_name])
                            break

            conn.execute("INSERT OR REPLACE INTO file_state VALUES(?,?)", [fp, new_offset])
        except Exception as e:
            if _debug_enabled():
                print(f"[clexo sync codex] {jsonl_file}: {e}", file=sys.stderr)
            continue

    conn.commit()
    return total


# ── backfill titles for already-indexed Claude sessions ──────────────────────

def _backfill_claude_titles(conn) -> int:
    """One-time pass: extract ai-title / custom-title / last-prompt for sessions
    that were indexed before we started capturing these fields."""
    untitled = conn.execute("""
        SELECT session_id FROM sessions
        WHERE source='claude' AND (thread_name IS NULL OR thread_name='')
    """).fetchall()

    updated = 0
    for (session_id,) in untitled:
        files = list(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
        if not files:
            continue
        thread_name = last_prompt = ""
        try:
            with open(files[0]) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = obj.get("type")
                    if t == "ai-title" and not thread_name:
                        thread_name = obj.get("aiTitle", "")
                    elif t == "custom-title" and not thread_name:
                        thread_name = obj.get("customTitle", "")
                    elif t == "last-prompt":
                        lp = obj.get("lastPrompt", "")
                        if lp and not _is_noise(lp):
                            last_prompt = lp[:200]
        except Exception:
            continue

        if thread_name or last_prompt:
            conn.execute(
                "UPDATE sessions SET thread_name=?, last_prompt=? WHERE session_id=?",
                [thread_name, last_prompt, session_id]
            )
            updated += 1

    conn.commit()
    return updated


# ── stats helper ──────────────────────────────────────────────────────────────

def _stat(key: str, delta: int = 1, conn=None) -> None:
    try:
        c = conn or get_db()
        c.execute("""
            INSERT INTO stats(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
        """, [key, delta])
        c.commit()
    except Exception:
        pass


# ── public sync entry point ───────────────────────────────────────────────────

_last_sync_ts: float = 0.0
_SYNC_THROTTLE_SECONDS = 300  # 5-minute no-op window for opportunistic callers


def _debug_enabled() -> bool:
    """True if ~/.clexo/config.json has "debug": true. Used to gate verbose logging."""
    try:
        return bool(json.loads((CLEXO_DIR / "config.json").read_text()).get("debug"))
    except Exception:
        return False


def sync_all(conn: sqlite3.Connection = None, throttle: bool = False) -> int:
    """Sync new messages from Claude/Codex JSONLs into the FTS index.

    throttle=True: no-op if a sync ran within the last _SYNC_THROTTLE_SECONDS.
    Use from search-path callers; SessionEnd hook and explicit save force fresh.
    """
    global _last_sync_ts
    if throttle and (time.time() - _last_sync_ts) < _SYNC_THROTTLE_SECONDS:
        return 0
    owned = conn is None
    if owned:
        conn = get_db()
    total = _sync_claude(conn) + _sync_codex(conn)
    _backfill_claude_titles(conn)
    if total > 0:
        _stat("messages_indexed", total, conn)
    if owned:
        conn.close()
    _last_sync_ts = time.time()
    return total


# ── helpers for search output ─────────────────────────────────────────────────

def _resume_cmd(source: str, session_id: str) -> str:
    if source == "codex":
        return ""   # codex resume only works as a terminal command; not actionable in-session
    return f"claude --resume {session_id}"


# Single source of truth for prefixes that mark system-injected (non-user) text.
# Kept broad enough to catch variants ("# Context from ..." with various tails).
_NOISE_PREFIXES = (
    "# Context from",
    "# Files mentioned by the user",
    "A previous agent",
    "Base directory for this skill:",
)

def _is_system_noise(text: str) -> bool:
    """True if text is system-injected — XML/HTML tags or known prefixes."""
    t = text.strip()
    if t.startswith("<"):
        return True
    return any(t.startswith(p) for p in _NOISE_PREFIXES)

def _is_noise(text: str) -> bool:
    """True if text is noise — system-injected or too short to be meaningful."""
    if _is_system_noise(text):
        return True
    return len(text.strip()) < 8

def clean(text: str) -> str | None:
    t = text.strip() if text else ""
    return None if (not t or _is_noise(t)) else t[:120]


def _session_summary(db: sqlite3.Connection, session_id: str) -> list[str]:
    """Opening (first_user_msg) + last user message, from sessions table where possible,
    falling back to messages table only when needed."""
    sess = db.execute(
        "SELECT first_user_msg, last_prompt FROM sessions WHERE session_id=?",
        [session_id]
    ).fetchone()

    first_msg  = clean(sess[0]) if sess and sess[0] else None
    last_prompt = clean(sess[1]) if sess and sess[1] else None

    # If sessions table has both, use them directly (no messages query)
    if first_msg and last_prompt and first_msg != last_prompt:
        return [first_msg, last_prompt]
    if first_msg and (not last_prompt or first_msg == last_prompt):
        # Try messages table for a better last message
        last_rows = db.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY ts DESC LIMIT 10",
            [session_id]
        ).fetchall()
        for (text,) in last_rows:
            t = clean(text)
            if t and t != first_msg:
                return [first_msg, t]
        return [first_msg] if first_msg else []

    # Full fallback: query messages table for first 2 + last
    first_rows = db.execute(
        "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY ts LIMIT 10",
        [session_id]
    ).fetchall()
    last_rows = db.execute(
        "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY ts DESC LIMIT 10",
        [session_id]
    ).fetchall()

    opening, seen = [], set()
    for (text,) in first_rows:
        t = clean(text)
        if t and t not in seen:
            seen.add(t)
            opening.append(t)
        if len(opening) == 2:
            break

    closing = None
    for (text,) in last_rows:
        t = clean(text)
        if t and t not in seen:
            closing = t
            break

    return opening + ([closing] if closing else [])


# ── Tags ──────────────────────────────────────────────────────────────────────

# Tag helpers — store friendly names that map to session UUIDs and resolve
# them transparently anywhere a session_id is accepted (load, pick, resume).

def _normalize_tag(name: str) -> str:
    return (name or "").strip().lower()


def _validate_tag(name: str) -> str | None:
    """Return None if valid, else an error string describing why it isn't."""
    if not name or not name.strip():
        return "Tag name cannot be empty."
    n = _normalize_tag(name)
    if _TAG_UUID_RE.match(n):
        return "Tag names cannot look like a UUID (would clash with raw session ids)."
    if not _TAG_RE.match(n):
        return ("Tag must be 1–64 chars of [a-z0-9_-], starting with a letter or digit "
                f"(got: {name!r}).")
    return None


def _resolve_current_or_given_session(session_id: str = "") -> tuple[str, str]:
    """Resolve to (session_id, source). Returns ("", "") if nothing matches.

    With session_id: verifies it exists either in the sessions index or as a
    JSONL on disk. Without: uses CLAUDE_CODE_SESSION_ID env, else most-recent
    mtime across Claude + Codex sessions.
    """
    db = get_db()
    if session_id:
        row = db.execute("SELECT source FROM sessions WHERE session_id=?",
                         [session_id]).fetchone()
        if row:
            return session_id, (row[0] or "claude")
        if list(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl")):
            return session_id, "claude"
        if CODEX_SESSIONS.exists() and list(CODEX_SESSIONS.glob(f"**/*{session_id}*.jsonl")):
            return session_id, "codex"
        return "", ""

    # Resolution order:
    #   1. CLAUDE_CODE_SESSION_ID env if its JSONL exists. For !-prefix shells
    #      run from the user's actual claude, env is the most reliable signal
    #      (even if that session was resumed and is no longer in ~/.claude/sessions).
    #   2. Newest ~/.claude/sessions/*.json whose cwd matches PWD. Used when env
    #      is unset; helps when the parent process isn't reachable via env.
    #   3. Mtime-of-all-JSONLs fallback.
    # NOTE: when the caller is an MCP server connected to a *different* claude
    # than the user is currently typing in, env will resolve to the MCP-attached
    # session — not the user's. There's no reliable way to detect this from the
    # MCP side; the success message includes the resolved cwd so the user can spot it.
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if env_sid:
        if list(CLAUDE_PROJECTS.glob(f"*/{env_sid}.jsonl")):
            return env_sid, "claude"
        if CODEX_SESSIONS.exists() and list(CODEX_SESSIONS.glob(f"**/*{env_sid}*.jsonl")):
            return env_sid, "codex"

    sessions_dir = Path.home() / ".claude" / "sessions"
    pwd          = os.environ.get("PWD") or os.getcwd()
    if sessions_dir.exists():
        candidates: list = []
        for sfile in sessions_dir.glob("*.json"):
            try:
                d = json.loads(sfile.read_text())
            except Exception:
                continue
            if d.get("cwd") == pwd and d.get("sessionId"):
                candidates.append((sfile.stat().st_mtime, d["sessionId"]))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1], "claude"

    if env_sid:
        return env_sid, "claude"

    all_files = list(CLAUDE_PROJECTS.glob("*/*.jsonl"))
    if CODEX_SESSIONS.exists():
        all_files += list(CODEX_SESSIONS.glob("**/*.jsonl"))
    if not all_files:
        return "", ""
    jsonl = max(all_files, key=lambda p: p.stat().st_mtime)
    if ".claude" in str(jsonl):
        return jsonl.stem, "claude"
    m = _UUID_RE.search(jsonl.stem)
    return (m.group(0) if m else jsonl.stem), "codex"


def _resolve_tag(name: str) -> str | None:
    """Tag → session_id, or None if not found."""
    n = _normalize_tag(name)
    if not n:
        return None
    row = get_db().execute("SELECT session_id FROM tags WHERE tag=?", [n]).fetchone()
    return row[0] if row else None


def _resolve_session_or_tag(name_or_uuid: str) -> str:
    """Look up `name_or_uuid` as a tag. If found, return its session_id; otherwise
    return the input unchanged (caller treats it as a UUID/raw id)."""
    if not name_or_uuid:
        return name_or_uuid
    s = name_or_uuid.strip()
    if _TAG_UUID_RE.match(s.lower()):
        return s
    sid = _resolve_tag(s)
    return sid if sid else s


def _create_tag(name: str, session_id: str = "", replace: bool = False) -> str:
    err = _validate_tag(name)
    if err:
        return f"Error: {err}"
    tag = _normalize_tag(name)

    db = get_db()
    if session_id:
        s = session_id.strip()
        if not _TAG_UUID_RE.match(s.lower()):
            return f"Error: session_id {session_id!r} doesn't look like a UUID."
        sid = s
        row = db.execute("SELECT source FROM sessions WHERE session_id=?",
                         [sid]).fetchone()
        source = (row[0] if row else None) or "claude"
    else:
        sid, source = _resolve_current_or_given_session()
        if not sid:
            return ("No session found to tag. Pass a session_id, or run this from "
                    "inside a Claude/Codex session.")

    existing = db.execute("SELECT session_id FROM tags WHERE tag=?", [tag]).fetchone()
    if existing and not replace:
        ex_sid = existing[0]
        if ex_sid == sid:
            return f"Tag '{tag}' already points at this session ({sid[:8]}…)."
        return (f"Tag '{tag}' already exists → {ex_sid} ({ex_sid[:8]}…). "
                f"Re-run with replace=True (or --force) to overwrite, "
                f"or choose a different name.")

    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    db.execute("INSERT OR REPLACE INTO tags(tag, session_id, created_ts) VALUES(?,?,?)",
               [tag, sid, now])
    db.commit()
    verb = "Replaced" if existing else "Tagged"
    # include project/cwd for the resolved session so caller can verify it's
    # the one they meant (especially when nested-claude env inheritance is in play).
    sess = db.execute("SELECT project, cwd FROM sessions WHERE session_id=?",
                      [sid]).fetchone()
    where = ""
    if sess:
        proj = (sess[0] or "").lstrip("-").replace("-", "/")
        loc  = sess[1] or proj
        if loc:
            where = f" ({loc})"
    return f"{verb} '{tag}' → {sid} [{source}]{where}"


def _remove_tag(name: str) -> str:
    tag = _normalize_tag(name)
    if not tag:
        return "Error: tag name cannot be empty."
    db = get_db()
    row = db.execute("SELECT session_id FROM tags WHERE tag=?", [tag]).fetchone()
    if not row:
        return f"No tag named '{tag}'."
    db.execute("DELETE FROM tags WHERE tag=?", [tag])
    db.commit()
    return f"Removed tag '{tag}' (was → {row[0]})"


# Keyword extraction for richer tags listings ─────────────────────────────
_KEYWORD_STOP = frozenset("""
the a an and or but if then so to of in on at for with from by as is are was were be been being
have has had do does did will would can could should may might must
i you he she it we they me my your our their this that these those
what when where why how which who whom whose
not no yes ok okay just like also more most some any all each every other
get got set put make made take took see saw look know knew think thought want wanted use used
one two three first last next new old same different
about into out up down over under back here there
yeah ok hi hey thanks please sorry actually really very much many lot
also need add file files line lines code function functions def let try call called calls
""".split())

_WORD_RE      = re.compile(r"[a-z][a-z_]{2,30}")
_SUMMARY_RE   = re.compile(r'^#{2,3}\s+Summary\s*\n(.*?)(?=^#{2,3}\s+|\Z)',
                            re.MULTILINE | re.DOTALL)


def _session_keywords(db: sqlite3.Connection, session_id: str,
                      top: int = 8, idf_cache: dict | None = None) -> list[str]:
    """Top TF-IDF keywords for a session.

    User messages weighted 3× (they're the topic signal); assistant text 1×.
    Filters: stopword list, min raw count 2 (drops typos/one-offs), letters
    and underscores only, 3–31 chars. IDF computed against the full FTS index;
    pass a shared `idf_cache` dict to amortize across multiple sessions.
    """
    rows = db.execute(
        "SELECT role, content FROM messages WHERE session_id=?", [session_id]
    ).fetchall()
    if not rows:
        return []

    tf_w  = Counter()  # weighted (user 3×) — used for ranking
    tf_r  = Counter()  # raw — used for min-count filter
    for role, content in rows:
        if not content or _is_noise(content):
            continue
        weight = 3 if role == "user" else 1
        for w in _WORD_RE.findall(content.lower()):
            if w in _KEYWORD_STOP:
                continue
            tf_w[w] += weight
            tf_r[w] += 1

    candidates = [w for w, c in tf_r.most_common(60) if c >= 2]
    if not candidates:
        return []

    N = db.execute("SELECT COUNT(DISTINCT session_id) FROM messages").fetchone()[0] or 1

    scored = []
    for w in candidates:
        if idf_cache is not None and w in idf_cache:
            idf = idf_cache[w]
        else:
            try:
                df = db.execute(
                    "SELECT COUNT(DISTINCT session_id) FROM messages WHERE messages MATCH ?",
                    [f'"{w}"']
                ).fetchone()[0]
            except Exception:
                df = N
            idf = math.log((N + 1) / (df + 1)) + 1
            if idf_cache is not None:
                idf_cache[w] = idf
        scored.append((w, tf_w[w] * idf))

    scored.sort(key=lambda x: -x[1])
    return [w for w, _ in scored[:top]]


def _chain_summary(sid: str) -> str:
    """Pull the ### Summary section from a saved chain-<sid>.md / refresh-<sid>.md.
    Returns the last (most recent) summary if the file is a chain; empty if none."""
    for fname in (f"chain-{sid}.md", f"refresh-{sid}.md"):
        f = CLEXO_DIR / fname
        if not f.exists():
            continue
        try:
            content = f.read_text()
        except Exception:
            continue
        matches = _SUMMARY_RE.findall(content)
        if matches:
            return matches[-1].strip()
    return ""


def _format_tags() -> str:
    db = get_db()
    rows = db.execute("""
        SELECT t.tag, t.session_id, t.created_ts,
               s.source, s.project, s.thread_name, s.summary, s.last_ts
        FROM tags t
        LEFT JOIN sessions s ON s.session_id = t.session_id
        ORDER BY t.tag
    """).fetchall()
    if not rows:
        return "No tags yet. Create one with tag(name='<friendly-name>')."

    idf_cache: dict = {}
    out = [f"{len(rows)} tag(s):\n"]
    for tag, sid, _created, source, project, thread_name, summary, last_ts in rows:
        src  = source or "claude"
        proj = (project or "").lstrip("-").replace("-", "/")
        last = (last_ts or "")[:10] or "?"
        out.append(f"@{tag}  →  {sid}  [{src}] {proj}  (last: {last})")
        if thread_name:
            out.append(f"    Title: {thread_name}")

        # Summary: sessions.summary → chain file → omit
        summary_text = summary or _chain_summary(sid)
        if summary_text:
            for line in summary_text.splitlines():
                stripped = line.strip().lstrip("-").strip()
                if stripped and len(stripped) > 3:
                    out.append(f"    Summary: {stripped[:200]}")
                    break

        if source is not None:
            for j, line in enumerate(_session_summary(db, sid)):
                prefix = "Opening:" if j == 0 else "Last:"
                out.append(f"    {prefix} {line}")
            kws = _session_keywords(db, sid, top=8, idf_cache=idf_cache)
            if kws:
                out.append(f"    Keywords: {', '.join(kws)}")
        out.append("")
    return "\n".join(out).rstrip()


# ── conversation formatter for summarisation ─────────────────────────────────

def _format_for_summary(db: sqlite3.Connection, session_id: str, char_limit: int = 8000) -> str:
    """Return conversation text (user + assistant turns) for AI summarisation."""
    sess = db.execute(
        "SELECT project, source, thread_name, cwd FROM sessions WHERE session_id=?",
        [session_id]
    ).fetchone()
    if not sess:
        return ""

    project, source, thread_name, cwd = sess
    header_parts = [f"Source: {source}", f"Project: {project.lstrip('-').replace('-','/')}"]
    if thread_name:
        header_parts.append(f"Title: {thread_name}")
    if cwd:
        header_parts.append(f"Dir: {cwd}")
    header = "  ".join(header_parts)

    rows = db.execute(
        "SELECT role, content, ts FROM messages WHERE session_id=? ORDER BY ts",
        [session_id]
    ).fetchall()

    lines = [header, ""]
    total = len(header)
    for role, content, ts in rows:
        if _is_noise(content):
            continue
        prefix = "User" if role == "user" else "Asst"
        # assistant messages truncated — just enough to show what was done
        text = content.strip()[:300] if role == "assistant" else content.strip()[:500]
        line = f"[{prefix}] {text}"
        total += len(line)
        if total > char_limit:
            lines.append("[... conversation truncated ...]")
            break
        lines.append(line)

    return "\n".join(lines)


# ── session excerpt (raw JSONL window) ───────────────────────────────────────

def _find_session_jsonl(session_id: str, source: str = "claude") -> Path | None:
    if source == "codex":
        hits = list(CODEX_SESSIONS.glob(f"**/*{session_id}*.jsonl"))
    else:
        hits = list(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _is_user_text(msg: dict) -> bool:
    """True for real user input — false for tool_result messages and noise."""
    if msg["role"] != "user":
        return False
    content = msg["content"]
    if isinstance(content, str):
        return bool(content.strip()) and not _is_noise(content)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                return bool(text.strip()) and not _is_noise(text)
    return False


def _read_raw_messages(jsonl_file: Path) -> list[dict]:
    """All user/assistant messages from JSONL including tool_result blocks."""
    if CODEX_SESSIONS in jsonl_file.parents:
        return _read_raw_messages_codex(jsonl_file)
    msgs = []
    with open(jsonl_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") not in ("user", "assistant"):
                    continue
                msg = obj.get("message", {})
                msgs.append({
                    "role":    msg.get("role", obj["type"]),
                    "content": msg.get("content", ""),
                    "ts":      obj.get("timestamp", ""),
                })
            except Exception:
                continue
    return msgs


def _read_raw_messages_codex(jsonl_file: Path) -> list[dict]:
    """Full history from a Codex JSONL — all turns across compaction boundaries."""
    msgs = []
    with open(jsonl_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                t  = obj.get("type", "")
                p  = obj.get("payload", {})
                ts = obj.get("timestamp", "")
                if not isinstance(p, dict):
                    continue
                if t == "event_msg" and p.get("type") == "user_message":
                    text = p.get("message", "")
                    if text and not _is_noise(text):
                        msgs.append({"role": "user", "content": text, "ts": ts})
                elif t == "response_item" and p.get("role") == "assistant":
                    for block in p.get("content", []):
                        if block.get("type") == "output_text":
                            text = block.get("text", "")
                            if text.strip():
                                msgs.append({"role": "assistant", "content": text, "ts": ts})
                            break
            except Exception:
                continue
    return msgs


def _extract_content(content, include_results: bool = True) -> str:
    if isinstance(content, str):
        return content.strip()
    parts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            text = block.get("text", "").strip()
            if text:
                parts.append(text)
        elif t == "tool_use":
            name = block.get("name", "")
            inp  = block.get("input", {})
            cmd  = inp.get("command") or inp.get("file_path") or next(iter(inp.values()), "")
            parts.append(f"[{name}: {str(cmd)[:300]}]")
        elif t == "tool_result" and include_results:
            raw = block.get("content", "")
            if isinstance(raw, list):
                raw = "\n".join(b.get("text", "") for b in raw
                                if isinstance(b, dict) and b.get("type") == "text")
            parts.append(f"[OUTPUT]\n{str(raw)[:3000]}")
    return "\n".join(parts)


def _fmt_message(msg: dict) -> str:
    role = msg["role"].upper()
    ts   = msg["ts"][:19] if msg["ts"] else ""
    body = _extract_content(msg["content"])
    return f"[{role}] {ts}\n{body}"


def _anchor_index(msgs: list, anchor_ts: str) -> int | None:
    """Index of the first message with ts >= anchor_ts, or None if no message
    matches (e.g. anchor is past all messages, or msgs is empty)."""
    for i, m in enumerate(msgs):
        if m["ts"] >= anchor_ts:
            return i
    return None


def _extract_window(msgs: list, anchor_idx: int, before: int, after: int) -> list:
    """before user-turns before anchor_idx, then anchor onwards for after msgs."""
    start = anchor_idx
    found = 0
    for i in range(anchor_idx - 1, -1, -1):
        if _is_user_text(msgs[i]):
            found += 1
            start = i
            if found >= before:
                break
    end = min(anchor_idx + after, len(msgs) - 1)
    return msgs[start : end + 1]


def _next_user_text_idx(msgs: list, from_idx: int, direction: str, count: int = 1) -> int:
    """Index after moving `count` user-text messages in direction from from_idx."""
    idx = from_idx
    for _ in range(max(1, count)):
        if direction == "before":
            found = next((i for i in range(idx - 1, -1, -1) if _is_user_text(msgs[i])), None)
        else:
            found = next((i for i in range(idx + 1, len(msgs)) if _is_user_text(msgs[i])), None)
        if found is None:
            break
        idx = found
    return idx


_pick_state: dict | None = None


def _save_pick_state(state: dict) -> None:
    global _pick_state
    _pick_state = state


def _load_pick_state() -> dict | None:
    return _pick_state


# ── Chain file helpers ────────────────────────────────────────────────────────

def _parse_chain_sections(content: str) -> list[tuple[str, str]]:
    """Parse chain file into (session_id, section_text) pairs in order."""
    matches = list(_CHAIN_RE.finditer(content))
    sections = []
    for i, m in enumerate(matches):
        start = m.start()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections.append((m.group(1), content[start:end].strip()))
    return sections


def _load_chain_content(sid: str) -> str | None:
    """Load last section from chain-<sid>.md; prefix with chain header if multi-session."""
    f = CLEXO_DIR / f"chain-{sid}.md"
    if not f.exists():
        return None
    sections = _parse_chain_sections(f.read_text())
    if not sections:
        return None
    all_ids  = [s[0] for s in sections]
    last_txt = sections[-1][1]
    if len(all_ids) > 1:
        prev = ", ".join(i[:8] + "…" for i in all_ids[:-1])
        return f"Chain: {len(all_ids)} sessions — {prev}, {all_ids[-1][:8]}… (current)\n\n{last_txt}"
    return last_txt


def _find_chain_prev(session_id: str) -> str | None:
    """Return the session_id preceding session_id in the loaded chain, or None."""
    if not _loaded_session_id:
        return None
    f = CLEXO_DIR / f"chain-{_loaded_session_id}.md"
    if not f.exists():
        return None
    uuids = _CHAIN_RE.findall(f.read_text())
    try:
        idx = uuids.index(session_id)
        return uuids[idx - 1] if idx > 0 else None
    except ValueError:
        return None


# ── Project filter resolution ─────────────────────────────────────────────────

_CWD_ALIASES = {"this", "cwd", "."}
_CLAUDE_SESSIONS = Path.home() / ".claude" / "sessions"

def _resolve_project_filter(raw: str) -> str:
    """Resolve "this"/"cwd"/"." to the current session's project name."""
    if raw.lower() not in _CWD_ALIASES:
        return raw
    try:
        best = max(_CLAUDE_SESSIONS.glob("*.json"), key=lambda p: p.stat().st_mtime)
        data = json.loads(best.read_text())
        cwd = data.get("cwd", "")
        if cwd:
            return Path(cwd).name
    except Exception:
        pass
    return raw


# ── FTS query helpers ────────────────────────────────────────────────────────

def _relax_fts(query: str) -> str | None:
    """Return a relaxed FTS query, or None if already unquoted / single-word.

    Only relaxes quoted phrases → unquoted AND. Stops there — OR is too noisy
    when common words are present; let the AI reformulate if AND also misses.
    """
    q = query.strip()
    if q.startswith('"') and q.endswith('"') and q.count('"') == 2:
        return q[1:-1]
    return None


# ── Core search logic (shared by MCP tool and CLI) ────────────────────────────

def _search(query: str, limit: int = 10, project_filter: str = "",
            source_filter: str = "") -> str:
    """Search indexed sessions. Returns formatted string result."""
    db = get_db()
    sync_all(db, throttle=True)
    _stat("search_calls", conn=db)

    # If query has no explicit FTS5 operators, quote it so special chars (. @ /) are safe
    _fts_ops = {"AND", "OR", "NOT"}
    if not any(op in query.split() for op in _fts_ops) and '"' not in query:
        fts_query = f'"{query}"'
    else:
        fts_query = query

    conditions = ["messages MATCH ?"]
    params: list = [fts_query]
    if project_filter:
        conditions.append("m.project LIKE ?")
        params.append(f"%{project_filter}%")
    if source_filter:
        conditions.append("s.source = ?")
        params.append(source_filter)
    where = " AND ".join(conditions)

    try:
        rows = db.execute(f"""
            SELECT m.session_id, m.project, m.role,
                   snippet(messages, 3, '>>>', '<<<', '...', 20) AS snip, m.ts
            FROM messages m
            JOIN sessions s ON s.session_id = m.session_id
            WHERE {where}
            ORDER BY rank LIMIT ?
        """, params + [limit]).fetchall()
    except Exception as e:
        return f"Search error: {e}"

    if not rows:
        return f"No results for '{query}'"

    seen: dict = {}
    for session_id, project, role, snip, ts in rows:
        if session_id not in seen:
            sess = db.execute(
                "SELECT first_user_msg, cwd, source, thread_name FROM sessions WHERE session_id=?",
                [session_id]
            ).fetchone()
            source      = (sess[2] or "claude") if sess else "claude"
            thread_name = (sess[3] or "")       if sess else ""
            seen[session_id] = {
                "project":     project.lstrip("-").replace("-", "/"),
                "first_msg":   (sess[0] or "")[:120] if sess else "",
                "source":      source,
                "thread_name": thread_name,
                "snippets":    [],
                "ts":          ts,
            }
        if session_id in seen:
            seen[session_id]["snippets"].append(f"[{role}] {snip}")

    if not seen:
        return f"No results for '{query}' (after source filter)"

    out = [f"Found {len(seen)} session(s) matching '{query}':\n"]
    for i, (sid, info) in enumerate(seen.items(), 1):
        date  = info["ts"][:10] if info["ts"] else "?"
        badge = f"[{info['source']}]"
        out.append(f"--- {i}. {date} {badge} | {info['project']}")
        if info["source"] == "codex" and info["thread_name"]:
            out.append(f"    Title: {info['thread_name']}")
        summary = _session_summary(db, sid)
        for j, line in enumerate(summary):
            prefix = "Opening:" if j == 0 else ("Last:" if j == len(summary) - 1 else "→")
            out.append(f"    {prefix} {line}")
        for snip in info["snippets"][:2]:
            out.append(f"    Match: {snip}")
        out.append(f"    Session: {sid}")
        resume = _resume_cmd(info['source'], sid)
        if resume:
            out.append(f"    Resume: {resume}")
        out.append("")
    return "\n".join(out)


def refresh_save(session_id: str = "", db: sqlite3.Connection | None = None) -> str:
    """Save current (or specified) session as a snapshot. Module-level so
    the CLI can call this without standing up the MCP server.

    Session resolution order: explicit session_id arg → CLAUDE_CODE_SESSION_ID
    env var (inherited by both the MCP server subprocess and any bang-prefix
    Bash invocation, so works for both call paths) → most-recent-mtime fallback.
    The env var is the only reliable signal when multiple Claude CLIs run in
    parallel or when an Agent sub-session has fresher writes than ours."""
    if db is None:
        db = get_db()
    if not session_id:
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    # accept a tag name in place of a UUID.
    session_id = _resolve_session_or_tag(session_id) if session_id else session_id
    CFG_PATH = CLEXO_DIR / "config.json"

    sync_all(db)

    source = "claude"
    if session_id:
        hits = list(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
        if not hits and CODEX_SESSIONS.exists():
            hits = list(CODEX_SESSIONS.glob(f"**/*{session_id}*.jsonl"))
            source = "codex" if hits else "claude"
        jsonl = hits[0] if hits else None
    else:
        all_files = list(CLAUDE_PROJECTS.glob("*/*.jsonl"))
        if CODEX_SESSIONS.exists():
            all_files += list(CODEX_SESSIONS.glob("**/*.jsonl"))
        jsonl = max(all_files, key=lambda p: p.stat().st_mtime) if all_files else None
        if jsonl:
            if ".claude" in str(jsonl):
                session_id = jsonl.stem
                source = "claude"
            else:
                m = _UUID_RE.search(jsonl.stem)
                session_id = m.group(0) if m else jsonl.stem
                source = "codex"

    if not jsonl or not jsonl.exists():
        return "No session JSONL found."

    sess = db.execute(
        "SELECT summary, first_user_msg, last_prompt, thread_name FROM sessions WHERE session_id=?",
        [session_id]
    ).fetchone()
    if sess and sess[0]:
        summary = sess[0]
    elif sess:
        parts = []
        if sess[3]: parts.append(f"- Title: {sess[3]}")
        opening = sess[1] if sess[1] and not _is_noise(sess[1]) else None
        if opening: parts.append(f"- Opening: {opening}")
        if sess[2]: parts.append(f"- Last: {sess[2]}")
        summary = "\n".join(parts) or "(no summary)"
    else:
        summary = "(session not in index yet)"

    try:
        cfg = json.loads(CFG_PATH.read_text())
    except Exception:
        cfg = {}
    chars_min = cfg.get("refresh_tokens_min", 4000) * 4
    chars_max = cfg.get("refresh_tokens_max", 8000) * 4

    msgs = []
    full_chars = 0
    with open(jsonl) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                obj = json.loads(line)
                ts  = obj.get('timestamp', '')
                if source == 'codex':
                    t = obj.get('type', '')
                    p = obj.get('payload', {})
                    if not isinstance(p, dict): continue
                    if t == 'event_msg' and p.get('type') == 'user_message':
                        text = p.get('message', '')
                        if text and not _is_system_noise(text):
                            full_chars += len(text)
                            msgs.append(('user', text, ts))
                    elif t == 'response_item' and p.get('role') == 'assistant':
                        for block in p.get('content', []):
                            if block.get('type') == 'output_text':
                                text = block.get('text', '')
                                if text.strip():
                                    full_chars += len(text)
                                    msgs.append(('assistant', text, ts))
                                break
                else:
                    if obj.get('type') not in ('user', 'assistant'): continue
                    msg      = obj.get('message', {})
                    role     = msg.get('role', obj['type'])
                    raw      = msg.get('content', '')
                    text     = _extract_content(raw, include_results=False)
                    text_all = _extract_content(raw, include_results=True)
                    if text and not _is_system_noise(text):
                        full_chars += len(text_all)
                        msgs.append((role, text, ts))
            except Exception:
                continue

    pairs, i = [], len(msgs) - 1
    while i >= 0:
        if msgs[i][0] == 'assistant':
            j = i - 1
            while j >= 0 and msgs[j][0] == 'assistant':
                j -= 1
            if j >= 0 and msgs[j][0] == 'user':
                pairs.append((msgs[j], msgs[i]))
                i = j - 1
            else:
                i -= 1
        else:
            i -= 1

    selected, total = [], 0
    for u, a in pairs:
        chunk = len(u[1]) + len(a[1])
        if total + chunk > chars_max:
            break
        selected.append((u, a))
        total += chunk
        if total >= chars_min:
            break
    selected.reverse()

    tokens_saved = max(0, (full_chars - total) // 4)
    try:
        db.execute("""
            INSERT INTO stats(key, value) VALUES('tokens_saved', ?)
            ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
        """, [tokens_saved])
        db.commit()
    except Exception:
        pass

    exchanges = []
    for u, a in selected:
        exchanges.append(f"[USER] {u[2][:19]}\n{u[1][:20000]}\n")
        exchanges.append(f"[ASSISTANT] {a[2][:19]}\n{a[1][:20000]}\n")

    file_refs = sorted(set(
        re.findall(r'(?:/Users/\w[^\s,\'")\]]{5,}|~/.claude/\S+|~/Code/\S+)',
                   '\n'.join(exchanges))
    ))[:20]

    date            = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    project_display = jsonl.parent.name.lstrip('-').replace('-', '/')
    section_header  = f"## Session {session_id} | {date} | {source} | {project_display}"
    section_body    = (
        f"### Summary\n{summary}\n\n"
        f"### Key files\n" + ('\n'.join(file_refs) or '(none)') + "\n\n"
        f"### Recent exchanges\n" + '\n'.join(exchanges)
    )
    new_section = f"{section_header}\n\n{section_body}"

    global _loaded_session_id
    chain_sid = _loaded_session_id
    if not chain_sid and _CHAIN_LOADED.exists():
        chain_sid = _CHAIN_LOADED.read_text().strip()
        _CHAIN_LOADED.unlink(missing_ok=True)

    prior_content = ""
    if chain_sid and chain_sid != session_id:
        old_chain = CLEXO_DIR / f"chain-{chain_sid}.md"
        if old_chain.exists():
            prior_content = old_chain.read_text().rstrip() + "\n\n"
            old_chain.unlink()
        else:
            old_refresh = CLEXO_DIR / f"refresh-{chain_sid}.md"
            if old_refresh.exists():
                prior_content = f"## Session {chain_sid} | [migrated]\n\n{old_refresh.read_text()}\n\n"
                old_refresh.unlink()

    chain_file = CLEXO_DIR / f"chain-{session_id}.md"
    chain_file.write_text(prior_content + new_section)
    _loaded_session_id = session_id
    REFRESH_PENDING.write_text(session_id)
    _stat("refresh_saves", conn=db)

    total_chars = len(prior_content) + len(new_section)
    chain_label = " (chain: appended)" if prior_content else " (chain: new)"
    return (
        f"Saved {total_chars:,} chars{chain_label} · session {session_id} [{source}]\n"
        f"Run /clear — context auto-restores on your next message."
    )


# ── MCP server ────────────────────────────────────────────────────────────────

def _run_server():
    from mcp.server.fastmcp import FastMCP

    app = FastMCP(
        "clexo",
        instructions=(
            "Persistent, FTS-indexed archive of past Claude Code and Codex "
            "sessions. Use clexo whenever the user references prior work "
            "that is not in current context — e.g. 'load the last session', "
            "'load the latest codex session', 'what did we discuss about X', "
            "'find that session about Y', 'remember when we migrated Z'. "
            "Do NOT grep ~/.codex/sessions or ~/.claude/projects directly — "
            "clexo is the indexed entry point for that archive.\n\n"
            "Tools:\n"
            "  search  — find which session (FTS5; supports project_filter "
            "and source_filter='claude'|'codex'; empty query lists recent)\n"
            "  load    — pull a session's summary + recent exchanges into "
            "current context, and mark it as the next-session restore point\n"
            "  pick    — drill into raw exchanges/tool outputs within a "
            "session (FTS-anchored, or 'before'/'after'/signed offset to "
            "scroll)\n\n"
            "Typical flows:\n"
            "  'load the last codex session' → search(source_filter='codex') "
            "→ load(<id>)\n"
            "  'what was that nginx config' → search('nginx') → "
            "load(<id>) → pick('nginx', session_id=<id>) if the summary "
            "is not enough\n\n"
            "search() finds *which session*; pick() finds *what was said* "
            "in it; load() summarizes."
        ),
    )
    db  = get_db()

    @app.tool()
    def search(query: str = "", limit: int = 10, project_filter: str = "",
               source_filter: str = "") -> str:
        """Full-text search across the indexed archive of all past Claude Code
        and Codex sessions — every user message, assistant reply, and tool
        result. The broad entry point for any backward reference to prior
        conversation.

        Use whenever the user points back to earlier work not in current
        context — e.g. "did we discuss the CSRF fix?", "remember when we
        migrated the database?", "what was that nginx config?", "find that
        session about the OOM bug", "list recent sessions in this project".

        Returns ranked sessions with snippets. Then:
          load(session_id)                          — session summary
          pick(args="<q>", session_id="<uuid>")     — raw drill-in

        search() finds *which session*; pick() finds *what was said* in it.

        With query: FTS5 (AND, OR, NOT, phrase quotes).
        Without query: lists recent sessions newest first.

        Args:
            query: Search terms; omit to list recent sessions
            limit: Max results (default 10)
            project_filter: Partial project path e.g. "myapp"; use "this"/"cwd"/"." for current project
            source_filter: "claude" or "codex" to restrict to one AI
        """
        pf = _resolve_project_filter(project_filter)
        if not query.strip():
            sync_all(db, throttle=True)
            conditions, params = [], []
            if pf:
                conditions.append("project LIKE ?")
                params.append(f"%{pf}%")
            if source_filter:
                conditions.append("source = ?")
                params.append(source_filter)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            rows = db.execute(f"""
                SELECT session_id, project, first_user_msg, last_ts, source, thread_name
                FROM sessions {where}
                ORDER BY last_ts DESC LIMIT ?
            """, params + [limit]).fetchall()
            if not rows:
                return "No sessions indexed yet."
            out = [f"Recent {len(rows)} session(s):\n"]
            for sid, project, first_msg, ts, src, thread_name in rows:
                date = ts[:10] if ts else "?"
                proj = project.lstrip("-").replace("-", "/")
                src  = src or "claude"
                out.append(f"{date} [{src}] | {proj}")
                if src == "codex" and thread_name:
                    out.append(f"  Title: {thread_name}")
                for j, line in enumerate(_session_summary(db, sid)):
                    out.append(f"  {'Opening:' if j == 0 else 'Last:'} {line}")
                out.append(f"  Session: {sid}")
                resume = _resume_cmd(src, sid)
                if resume:
                    out.append(f"  Resume: {resume}")
                out.append("")
            return "\n".join(out)
        return _search(query, limit=limit, project_filter=pf, source_filter=source_filter)

    def get_session_excerpt(
        session_id: str,
        query: str = "",
        before: int = 1,
        after: int = 6,
        direction: str = "",
        count: int = 1,
    ) -> str:
        """Retrieve a window of raw messages from a session, including tool outputs.

        Two modes:
        - query provided  : FTS-find best match → show before+after window around it
        - direction given : continue from last window boundary, showing count more messages

        Args:
            session_id: Session UUID (from search_chats or refresh.md)
            query:      Search terms to locate anchor (omit when scrolling)
            before:     Messages before anchor in query mode (default 1)
            after:      Messages after anchor in query mode (default 6)
            direction:  "before" or "after" to scroll from last shown window boundary
            count:      Number of messages to show when scrolling (default 1)
        """
        sess = db.execute(
            "SELECT source FROM sessions WHERE session_id=?", [session_id]
        ).fetchone()
        source = sess[0] if sess else "claude"

        jsonl = _find_session_jsonl(session_id, source)
        if not jsonl:
            return f"JSONL file not found for session {session_id}."

        msgs = _read_raw_messages(jsonl)
        if not msgs:
            return "No messages found in session file."

        state = _load_pick_state()

        # ── scroll mode — continue from last window boundary ─────────────────
        if direction in ("before", "after"):
            if not state:
                return ("No prior pick position. Call pick(args=\"<query>\", "
                        f"session_id=\"{session_id}\") to anchor first.")

            # Support both old (anchor_ts) and new (start_ts/end_ts) state format
            if "end_ts" in state:
                end_idx   = _anchor_index(msgs, state["end_ts"])
                start_idx = _anchor_index(msgs, state["start_ts"])
            else:
                anchor_idx = _anchor_index(msgs, state["anchor_ts"])
                start_idx  = anchor_idx
                end_idx    = anchor_idx

            if start_idx is None or end_idx is None:
                return ("Saved pick position is stale (anchors no longer in this "
                        "session). Re-anchor with pick(args=\"<query>\", "
                        f"session_id=\"{session_id}\").")

            if count == 0:
                # Re-show last window unchanged
                s = start_idx
                e = end_idx + 1
            elif direction == "after":
                s = end_idx + 1
                e = min(s + count, len(msgs))
            else:
                e = start_idx
                s = max(0, e - count)

            window = msgs[s:e]
            if not window:
                if direction == "before":
                    prev_sid = _find_chain_prev(session_id)
                    if prev_sid:
                        return (f"At start of session {session_id[:8]}…\n"
                                f"Chain: previous session → {prev_sid}\n"
                                f"  pick(args='-1', session_id='{prev_sid}')")
                dir_word = "end" if direction == "after" else "beginning"
                return f"Already at the {dir_word} of the session."

        # ── query mode ───────────────────────────────────────────────────────
        else:
            if not query:
                return "Provide either a query or a direction (before/after)."
            def _run_fts(q):
                return db.execute(
                    "SELECT ts FROM messages WHERE session_id=? AND messages MATCH ? ORDER BY rank LIMIT 1",
                    [session_id, q]
                ).fetchall()

            rows = _run_fts(query)
            if not rows:
                relaxed = _relax_fts(query)
                if relaxed:
                    rows = _run_fts(relaxed)
                    if rows:
                        query = relaxed
            if not rows:
                hint = ""
                prev_sid = _find_chain_prev(session_id)
                if prev_sid:
                    hint = (f"\nChain: previous session → {prev_sid}"
                            f"\n  pick(args='{query}', session_id='{prev_sid}')")
                return (f"No match for '{query}' in this session (tried relaxed variants). "
                        f"Try pick() with different keywords, or search(query='...') to look across all sessions."
                        + hint)
            anchor_idx = _anchor_index(msgs, rows[0][0])
            if anchor_idx is None:
                # FTS matched a ts not in the loaded msgs — index/file out of sync.
                return (f"Internal: FTS-matched ts not present in session JSONL. "
                        f"Try `python server.py --sync` to re-index.")
            window = _extract_window(msgs, anchor_idx, before, after)

        start_i = next((i for i, m in enumerate(msgs) if m["ts"] == window[0]["ts"]), 0)
        end_i   = next((i for i, m in enumerate(msgs) if m["ts"] == window[-1]["ts"]), len(msgs) - 1)

        _save_pick_state({
            "start_ts": window[0]["ts"],
            "end_ts":   window[-1]["ts"],
            "query":    query if not direction else (state or {}).get("query", ""),
        })

        lines = [
            f"Session: {session_id}",
            f"Messages {start_i + 1}–{end_i + 1} of {len(msgs)}",
            f"Scroll:  /pick -N  |  /pick +N",
            "",
        ]
        for m in window:
            lines.append(_fmt_message(m))
            lines.append("")

        try:
            db.execute("""
                INSERT INTO stats(key, value) VALUES('pick_uses', 1)
                ON CONFLICT(key) DO UPDATE SET value = value + 1
            """)
            db.commit()
        except Exception:
            pass

        return "\n".join(lines)

    @app.tool()
    def load(session_id: str) -> str:
        """Load a session's snapshot (summary + recent exchanges + key file
        refs) into current context, and mark it as the restore point for
        the next session start.

        Mostly auto-invoked: the SessionStart hook calls load() to resume
        the last saved session. Manual use: after search() returns a
        session_id the user wants to revisit.

        Pair with pick() for deeper recall — load() gives the gist, pick()
        retrieves raw exchanges and tool outputs from the same session.

        Args:
            session_id: UUID, or a tag name created with tag(). May be the
                        pending restore UUID on session start.
        """
        global _loaded_session_id
        # resolve tag → uuid so chain/refresh files and pending state use the uuid.
        session_id = _resolve_session_or_tag(session_id)
        chain_f   = CLEXO_DIR / f"chain-{session_id}.md"
        refresh_f = CLEXO_DIR / f"refresh-{session_id}.md"
        if not chain_f.exists() and not refresh_f.exists():
            refresh_save(session_id)
        result = _refresh_load(session_id)
        if "## Session" in result or "# Refresh Context" in result:
            REFRESH_PENDING.write_text(session_id)
            _loaded_session_id = session_id
            _stat("refresh_loads", conn=db)
        return result

    @app.tool()
    def save() -> str:
        """Save the current session as a snapshot for restore on the next start.

        Compresses the active session (summary + recent exchanges + key file
        refs) into a chain file at ~/.clexo/chain-<sid>.md and marks it as the
        pending restore. The next time a new session starts, the SessionStart
        hook auto-loads this snapshot.

        Use when the user asks to save ("save this session", "save"), is about
        to clear context, or context is getting long enough to warrant a
        checkpoint.

        Pair with load(session_id) to immediately rehydrate a different past
        session into the current context instead.
        """
        return refresh_save()

    # ── Tag tools ────────────────────────────────────────────────────────
    @app.tool()
    def tag(name: str, session_id: str = "", replace: bool = False) -> str:
        """Assign a friendly tag to a session for easy recall later.

        Without session_id: tags the current session. With session_id (a UUID):
        tags that specific session. A session can have many tags; a tag points
        at exactly one session.

        On collision (name already in use): returns a message asking you to
        either re-run with replace=True to overwrite, or pick a different name.
        Always relay that prompt back to the user — do not silently overwrite.

        Use when the user wants to bookmark this session (or one referenced
        in search() results) by a memorable name — e.g. "tag this as
        auth-rewrite", "save this as today-morning".

        Pair with tags() to list and untag() to remove. load(<tag>) and
        pick(args=..., session_id=<tag>) both accept tags in place of UUIDs.

        Args:
            name:       Tag name, [a-z0-9_-], 1–64 chars, must start with [a-z0-9]
            session_id: Optional session UUID; defaults to current session
            replace:    Set True to overwrite an existing tag with the same name
        """
        return _create_tag(name, session_id, replace=replace)

    @app.tool()
    def untag(name: str) -> str:
        """Remove a tag mapping. Does not affect the underlying session."""
        return _remove_tag(name)

    @app.tool()
    def tags() -> str:
        """List all tags with their target sessions, opening/closing lines, and
        any saved summary. Use when the user asks "what tags do I have?",
        "list my tagged sessions", etc."""
        return _format_tags()

    @app.tool()
    def pick(args: str = "", session_id: str = "") -> str:
        """Drill into one session's chained history for raw exchanges and tool
        outputs not in current context. FTS-anchored within the session and
        its predecessors in the clexo chain.

        Use when the user references specific prior content the AI should
        "remember" — e.g. "what was that cloudflare command?", "what were
        the name options?", "show me the nginx block we used". The user
        expects recall; pick() retrieves it.

        Returns raw messages — bash outputs, file reads, tool results —
        unlike load() which summarizes.

        Modes:
          pick(args="nginx config", session_id="<uuid>")  FTS anchor
          pick(args="after") / pick(args="before")        scroll
          pick(args="+2") / pick(args="-1")               jump N

        Call with the loaded session_id, or any session_id from search().
        If no match here, widen to search().

        Args:
            args:       Query text, "before"/"after", or signed offset
            session_id: Session UUID (required)
        """
        a = args.strip()

        # Fall back to the most recently loaded session when caller omits one.
        session_id = session_id or _loaded_session_id
        # accept tag names alongside UUIDs.
        session_id = _resolve_session_or_tag(session_id)

        # Parse direction + count
        direction = ""
        count     = 1

        m = re.match(r'^([+-])(\d+)$', a)
        if m:
            direction = "before" if m.group(1) == "-" else "after"
            count     = int(m.group(2))
        else:
            m2 = re.match(r'^(before|after)(?:\s+(\d+))?$', a.lower())
            if m2:
                direction = m2.group(1)
                count     = int(m2.group(2)) if m2.group(2) else 1

        if direction:
            if not session_id:
                return ("session_id required to scroll. Call pick() with a query "
                        "first, or pass session_id from search() / load() results.")
            state = _load_pick_state()
            if not state:
                return ("No prior pick position. Call pick(args=\"<query>\", "
                        f"session_id=\"{session_id}\") to anchor first.")
            return get_session_excerpt(
                session_id=session_id,
                direction=direction,
                count=count,
            )

        # no args — re-show last window
        if not a:
            if not session_id:
                return ("session_id required. Call pick(args=\"<query>\", "
                        "session_id=\"<uuid>\") to anchor first.")
            state = _load_pick_state()
            if not state:
                return ("No prior pick position. Call pick(args=\"<query>\", "
                        f"session_id=\"{session_id}\") to anchor first.")
            return get_session_excerpt(
                session_id=session_id,
                direction="after",
                count=0,
            )

        if not session_id:
            return "session_id required for query mode — pass one from search() or load()."
        return get_session_excerpt(session_id=session_id, query=a)

    @app.tool()
    def get_stats() -> str:
        """Return clexo usage stats."""
        try:
            rows = {r[0]: r[1] for r in db.execute("SELECT key, value FROM stats").fetchall()}
            return "\n".join([
                f"Saves:              {rows.get('refresh_saves',    0):>6,}",
                f"Loads:              {rows.get('refresh_loads',    0):>6,}",
                f"Tokens saved:       {rows.get('tokens_saved',     0):>6,}",
                f"Pick uses:          {rows.get('pick_uses',        0):>6,}",
                f"Search calls:       {rows.get('search_calls',     0):>6,}",
                f"Messages indexed:   {rows.get('messages_indexed', 0):>6,}",
            ])
        except Exception as e:
            return f"Stats unavailable: {e}"

    app.run()


def _refresh_load(session_id: str = "") -> str:
    """Core logic for loading a saved refresh context (shared by MCP tool and hook)."""
    # accept tag names alongside UUIDs.
    if session_id:
        session_id = _resolve_session_or_tag(session_id)
    PREAMBLE = (
        "=== Previous session context restored. This IS your memory — treat it as "
        "continuous conversation. The user picks up from where they left off. Answer "
        "confidently from this context. If they reference something not in this summary, "
        "use the pick tool to search the full chat history. ===\n\n"
    )

    def _load_file(sid: str, clear_pending: bool) -> str:
        # Chain file takes precedence over old refresh-*.md
        chain_content = _load_chain_content(sid)
        if chain_content:
            if clear_pending:
                REFRESH_PENDING.unlink(missing_ok=True)
            return PREAMBLE + chain_content
        f = CLEXO_DIR / f"refresh-{sid}.md"
        if not f.exists():
            if clear_pending:
                REFRESH_PENDING.unlink(missing_ok=True)
            return f"Refresh file for session {sid[:8]}… not found."
        content = f.read_text()
        if clear_pending:
            REFRESH_PENDING.unlink(missing_ok=True)
        return PREAMBLE + content

    if session_id:
        return _load_file(session_id, clear_pending=False)

    if REFRESH_PENDING.exists():
        sid = REFRESH_PENDING.read_text().strip()
        return _load_file(sid, clear_pending=True)

    # No pending — list available chain + legacy refresh files
    files = sorted(
        list(CLEXO_DIR.glob("chain-*.md")) + list(CLEXO_DIR.glob("refresh-*.md")),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    if not files:
        return "No saved sessions found — run save() first."

    lines = ["Available sessions (most recent first):\n"]
    for i, f in enumerate(files[:20], 1):
        stem  = f.stem
        sid   = stem[stem.index("-") + 1:]
        label = "chain" if stem.startswith("chain-") else "session"
        mtime = datetime.datetime.fromtimestamp(
            f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        try:
            first_line = f.read_text().splitlines()[0]
        except Exception:
            first_line = ""
        lines.append(f"{i}. [{mtime}] [{label}] {sid[:8]}…  {first_line}")
    lines.append("\nCall load(session_id='<full-uuid>') to load one.")
    return "\n".join(lines)


def _pack_compact(content: str, cap: int, footer: str = "") -> dict:
    """Pack a restored session's content under `cap` bytes for hook injection.

    Strategy: preserve the skeleton (everything outside `### Recent exchanges`)
    verbatim, then greedily fit recent turns newest-first into the remaining
    budget. Older turns are replaced with an elided marker pointing at pick().

    Returns dict with: compact (str), total_turns, kept, elided, budget, used.
    """
    PLACEHOLDER = "__CLEXO_EXCHANGES__"
    lines = content.splitlines()

    # Parse turns from `### Recent exchanges` through end. The section is
    # terminal in a chain entry, so don't bail on `##`/`###` lines inside
    # message bodies (e.g. assistant replies with markdown subheaders).
    turns: list[str] = []
    current: list[str] = []
    in_ex = False
    for line in lines:
        if line.strip() == "### Recent exchanges":
            in_ex = True
            continue
        if in_ex:
            if line.startswith("[USER]") or line.startswith("[ASSISTANT]"):
                if current:
                    turns.append("\n".join(current).rstrip())
                current = [line]
            elif current:
                current.append(line)
    if current:
        turns.append("\n".join(current).rstrip())

    # Build skeleton with a placeholder where exchanges go.
    skeleton_lines: list[str] = []
    skip = False
    for line in lines:
        if line.strip() == "### Recent exchanges":
            skip = True
            skeleton_lines.append(line)
            skeleton_lines.append(PLACEHOLDER)
            continue
        if skip:
            continue
        skeleton_lines.append(line)
    skeleton = "\n".join(skeleton_lines)

    overhead = len(skeleton) - len(PLACEHOLDER) + len(footer)
    budget = max(0, cap - overhead)

    kept: list[str] = []
    used = 0
    for turn in reversed(turns):
        chunk = ("\n\n" + turn) if kept else turn
        if used + len(chunk) > budget:
            break
        kept.insert(0, turn)
        used += len(chunk)

    elided = len(turns) - len(kept)
    if kept and elided > 0:
        body = (
            f"({elided} older exchange{'s' if elided != 1 else ''} elided — "
            f"call pick() to retrieve)\n\n" + "\n\n".join(kept)
        )
    elif kept:
        body = "\n\n".join(kept)
    else:
        body = "(all exchanges elided — call pick() to retrieve)"

    compact = skeleton.replace(PLACEHOLDER, body) + footer

    return {
        "compact":     compact,
        "total_turns": len(turns),
        "kept":        len(kept),
        "elided":      elided,
        "budget":      budget,
        "used":        used,
    }


def _session_start_hook() -> None:
    """Output SessionStart hook JSON by delegating to _refresh_load."""
    # Peek at pending before _refresh_load clears it — needed for chain handoff
    loaded_sid = ""
    if REFRESH_PENDING.exists():
        loaded_sid = REFRESH_PENDING.read_text().strip()

    content = _refresh_load()

    if not re.search(r'^## Session', content, re.MULTILINE) and "# Refresh Context" not in content:
        print(json.dumps({}))
        return

    # Write handoff file so save() in this process knows which chain to extend
    if loaded_sid:
        _CHAIN_LOADED.write_text(loaded_sid)

    lines = content.splitlines()

    # Extract date: chain format "## Session <uuid> | date | ..." or old "# Refresh Context — date —..."
    date = ""
    for line in lines:
        if line.startswith("## Session"):
            parts = line.split("|")
            if len(parts) >= 2:
                date = parts[1].strip()
            break
        if line.startswith("# Refresh Context"):
            parts = line.split("—")
            if len(parts) >= 2:
                date = parts[1].strip()
            break

    # Extract first meaningful summary line (handles both ## and ### Summary headers)
    summary = ""
    in_summary = False
    for line in lines:
        if line.strip() in ("## Summary", "### Summary"):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("##") or line.startswith("###"):
                break
            text = line.lstrip("- ").strip()
            if text and "<" not in text and len(text) > 3:
                summary = text[:55] + ("…" if len(text) > 55 else "")
                break

    # Pack under the 10KB hook context cap (additionalContext is dropped
    # entirely if it exceeds ~10K chars). Elided turns are retrievable via
    # mcp__clexo__pick.
    footer = ""
    if loaded_sid:
        footer = (
            f"\n\n=== For older exchanges or specifics not shown here, call "
            f'mcp__clexo__pick(args="<query>", session_id="{loaded_sid}"). ==='
        )

    CAP = 9500
    packed = _pack_compact(content, CAP, footer)
    compact = packed["compact"]
    kept_n  = packed["kept"]
    total_n = packed["total_turns"]
    elided  = packed["elided"]
    budget  = packed["budget"]
    used    = packed["used"]

    # Build box banner with packing stats (rendered via systemMessage)
    def _human_bytes(n: int) -> str:
        return f"{n/1024:.1f}K" if n >= 1024 else f"{n}B"

    stats = f"ctx {_human_bytes(len(compact))}/{_human_bytes(CAP)}"
    if total_n:
        stats += f" · {kept_n}/{total_n} turns"
        if elided > 0:
            stats += f" ({elided} elided)"
    else:
        stats += " · no turns"

    line1 = f"  ↺  Clexo · Session restored · {date}" if date else "  ↺  Clexo · Session restored"
    line2 = f"     {summary}" if summary else None
    line3 = f"     {stats}"
    candidates = [line1, line3] + ([line2] if line2 else [])
    inner_w = max(len(s) for s in candidates) + 2
    rows = [
        "╔" + "═" * inner_w + "╗",
        "║" + line1 + " " * (inner_w - len(line1)) + "║",
    ]
    if line2:
        rows.append("║" + line2 + " " * (inner_w - len(line2)) + "║")
    rows.append("║" + line3 + " " * (inner_w - len(line3)) + "║")
    rows.append("╚" + "═" * inner_w + "╝")
    banner = "\n".join(rows)

    # hook.log is debug-only — enable by setting "debug": true in ~/.clexo/config.json
    if _debug_enabled():
        log = CLEXO_DIR / "hook.log"
        with open(log, "a") as _lf:
            _lf.write(
                f"\n--- {datetime.datetime.now().isoformat()} ---\n"
                f"MODE: json hookSpecificOutput\n"
                f"banner_len={len(banner)} compact_len={len(compact)} full_len={len(content)} "
                f"turns_kept={kept_n} turns_total={total_n} elided={elided} "
                f"budget={budget} used={used}\n"
            )

    print(json.dumps({
        "systemMessage": banner,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": compact,
        },
    }))


def _print_stats() -> None:
    try:
        rows = {r[0]: r[1] for r in get_db().execute("SELECT key, value FROM stats").fetchall()}
        print("\n".join([
            f"Saves:              {rows.get('refresh_saves',    0):>6,}",
            f"Loads:              {rows.get('refresh_loads',    0):>6,}",
            f"Tokens saved:       {rows.get('tokens_saved',     0):>6,}",
            f"Pick uses:          {rows.get('pick_uses',        0):>6,}",
            f"Search calls:       {rows.get('search_calls',     0):>6,}",
            f"Messages indexed:   {rows.get('messages_indexed', 0):>6,}",
        ]))
    except Exception as e:
        print(f"Stats unavailable: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if "--sync" in sys.argv:
        n = sync_all()
        print(f"Indexed {n} new messages.", flush=True)
    elif "--session-start" in sys.argv:
        _session_start_hook()
    elif "--stats" in sys.argv or "stats" in sys.argv or "gain" in sys.argv:
        _print_stats()
    elif "--search" in sys.argv:
        idx = sys.argv.index("--search")
        query = " ".join(sys.argv[idx + 1:]) if idx + 1 < len(sys.argv) else ""
        if not query:
            print("Usage: server.py --search <query>", file=sys.stderr)
            sys.exit(1)
        print(_search(query))
    elif "--save" in sys.argv:
        idx = sys.argv.index("--save")
        sid = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        print(refresh_save(sid))
    # ── Tag CLI ────────────────────────────────────────────────────────────
    elif "--tag" in sys.argv:
        rest = sys.argv[sys.argv.index("--tag") + 1:]
        if not rest:
            print("Usage: server.py --tag <name> [--force] [session_id]", file=sys.stderr)
            sys.exit(1)
        force = "--force" in rest
        positional = [a for a in rest if a != "--force"]
        name = positional[0] if positional else ""
        sid  = positional[1] if len(positional) > 1 else ""
        out = _create_tag(name, sid, replace=force)
        print(out)
        # Exit non-zero on collision so scripts/aliases can detect it.
        if out.startswith("Error:") or "already exists" in out:
            sys.exit(2)
    elif "--untag" in sys.argv:
        idx = sys.argv.index("--untag")
        if idx + 1 >= len(sys.argv):
            print("Usage: server.py --untag <name>", file=sys.stderr)
            sys.exit(1)
        out = _remove_tag(sys.argv[idx + 1])
        print(out)
        if out.startswith("No tag") or out.startswith("Error:"):
            sys.exit(2)
    elif "--tags" in sys.argv:
        print(_format_tags())
    elif "--load" in sys.argv:
        idx = sys.argv.index("--load")
        arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        sid = _resolve_session_or_tag(arg) if arg else ""
        if sid:
            chain_f   = CLEXO_DIR / f"chain-{sid}.md"
            refresh_f = CLEXO_DIR / f"refresh-{sid}.md"
            if not chain_f.exists() and not refresh_f.exists():
                refresh_save(sid)
        print(_refresh_load(sid))
    elif "--resume" in sys.argv:
        idx = sys.argv.index("--resume")
        if idx + 1 >= len(sys.argv):
            print("Usage: server.py --resume <tag-or-uuid>", file=sys.stderr)
            sys.exit(1)
        name = sys.argv[idx + 1]
        sid = _resolve_session_or_tag(name)
        if not _TAG_UUID_RE.match(sid.lower()):
            print(f"Unknown tag '{name}'. Run `clexo tags` to list.", file=sys.stderr)
            sys.exit(1)
        # TTY → exec claude --resume; otherwise print the command paste-ready.
        if sys.stdout.isatty():
            os.execvp("claude", ["claude", "--resume", sid])
        else:
            print(f"claude --resume {sid}")
    else:
        _run_server()
