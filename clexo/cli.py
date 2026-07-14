#!/usr/bin/env python3
"""
Claude Code + Codex chat search MCP server.

Indexes:
  ~/.claude/projects/**/*.jsonl   (Claude Code sessions)
  ~/.codex/sessions/**/*.jsonl    (Codex sessions)
  ~/.grok/sessions/**/*.jsonl     (Grok Build sessions) — added recently

Self-updates via byte-offset tracking on every search call.
SessionEnd hook also calls: clexo sync

Run as MCP server : clexo serve
Run sync only     : clexo sync
"""

import datetime
import gzip
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

CLEXO_DIR          = Path.home() / ".clexo"

DB_PATH            = CLEXO_DIR / "index.db"
ARCHIVE_DIR        = CLEXO_DIR / "archive"      # gzipped transcript per session
ARCHIVE_CACHE      = CLEXO_DIR / "cache"        # decompressed-on-demand for readers
CLAUDE_PROJECTS    = Path.home() / ".claude" / "projects"
CODEX_SESSIONS     = Path.home() / ".codex" / "sessions"
CODEX_SESSION_IDX  = Path.home() / ".codex" / "session_index.jsonl"
GROK_SESSIONS      = Path.home() / ".grok" / "sessions"
REFRESH_PENDING    = CLEXO_DIR / "refresh-pending"
REFRESH_EXPLICIT   = CLEXO_DIR / "refresh-explicit"  # marks a user-requested load (bypasses the cwd guard once)

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
    if "prefix_delta_tokens" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN prefix_delta_tokens INTEGER DEFAULT 0")
    if "prefix_delta_input_tokens" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN prefix_delta_input_tokens INTEGER DEFAULT 0")
    if "prefix_delta_cache_tokens" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN prefix_delta_cache_tokens INTEGER DEFAULT 0")
    # One-shot: split historical tokens_saved (compaction-only) into tokens_compacted.
    done = conn.execute("SELECT 1 FROM stats WHERE key='migration_split_tokens'").fetchone()
    if not done:
        conn.execute("""
            INSERT INTO stats(key, value)
            SELECT 'tokens_compacted', value FROM stats WHERE key='tokens_saved'
            ON CONFLICT(key) DO UPDATE SET value = stats.value + excluded.value
        """)
        conn.execute("DELETE FROM stats WHERE key='tokens_saved'")
        conn.execute("INSERT OR IGNORE INTO stats(key, value) VALUES('migration_split_tokens', 1)")
    # One-shot: zero out bogus byte/4 deltas; switch to usage-based input/cache split.
    done2 = conn.execute("SELECT 1 FROM stats WHERE key='migration_split_input_cache'").fetchone()
    if not done2:
        conn.execute("UPDATE sessions SET prefix_delta_tokens = 0 WHERE prefix_delta_tokens > 0")
        conn.execute("DELETE FROM stats WHERE key='tokens_saved'")
        conn.execute("INSERT OR IGNORE INTO stats(key, value) VALUES('migration_split_input_cache', 1)")
    # One-shot: zero tokens_compacted — accumulated with the same broken byte/4 formula.
    done3 = conn.execute("SELECT 1 FROM stats WHERE key='migration_reset_compacted'").fetchone()
    if not done3:
        conn.execute("DELETE FROM stats WHERE key='tokens_compacted'")
        conn.execute("INSERT OR IGNORE INTO stats(key, value) VALUES('migration_reset_compacted', 1)")
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
                if isinstance(inp, dict):
                    # Index command-bearing fields in full (so search recalls real
                    # commands and paths), but cap other fields so a Write/MultiEdit
                    # body — the whole file contents — doesn't flood the index.
                    cmd_keys = ("command", "file_path", "path", "pattern", "query", "url")
                    primary = " ".join(str(inp[k]) for k in cmd_keys
                                       if isinstance(inp.get(k), str))
                    rest = " ".join(str(v)[:200] for k, v in inp.items()
                                    if isinstance(v, str) and k not in cmd_keys)
                    vals = (primary + " " + rest).strip()[:2000]
                else:
                    vals = str(inp)[:2000]
                parts.append(f"[{name}] {vals}")
        return " ".join(parts)
    return ""


# ── source-session prefix accounting ──────────────────────────────────────────

def _claude_source_prefix(jsonl_path) -> tuple[int, int]:
    """Return (input_tokens, cache_tokens) from the last assistant message's
    `usage` block in a Claude Code JSONL. This is the real API prefix the
    source session would have replayed on its next turn — the value a clexo
    snapshot eliminates by replacing the conversation with a small blob.

    cache_tokens = cache_read_input_tokens + cache_creation_input_tokens.
    Returns (0, 0) if the file is missing, unreadable, or has no assistant
    usage blocks (e.g. a Codex JSONL or an empty session)."""
    if not jsonl_path or not jsonl_path.exists():
        return 0, 0
    last_input = 0
    last_cache = 0
    try:
        with open(jsonl_path, "rb") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                u = (obj.get("message") or {}).get("usage") or {}
                ipt   = int(u.get("input_tokens")               or 0)
                cread = int(u.get("cache_read_input_tokens")    or 0)
                cwrt  = int(u.get("cache_creation_input_tokens") or 0)
                if (ipt + cread + cwrt) > 0:
                    last_input = ipt
                    last_cache = cread + cwrt
    except Exception:
        return 0, 0
    return last_input, last_cache


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
            session_id = jsonl_file.stem
            if stat.st_size <= last_offset:
                # Already fully indexed — but make sure it's archived (one-time
                # backfill of existing sessions; self-heals a deleted archive).
                if not _archive_path(session_id, "claude").exists():
                    _write_archive(session_id, "claude", jsonl_file)
                continue

            project    = jsonl_file.parent.name
            new_offset = last_offset

            delta_row = conn.execute(
                "SELECT prefix_delta_input_tokens, prefix_delta_cache_tokens "
                "FROM sessions WHERE session_id = ?",
                [session_id],
            ).fetchone()
            prefix_input = (delta_row[0] if delta_row else 0) or 0
            prefix_cache = (delta_row[1] if delta_row else 0) or 0
            assistant_turns_this_pass = 0

            for raw_line, new_offset in _read_new_lines(jsonl_file, last_offset):
                try:
                    obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type")
                ts = obj.get("timestamp", "")

                # Conversational turns. A prompt the user typed while the agent
                # was still working is stored as a queued-command `attachment`,
                # not a `user` record — index it as the human turn it is, so it's
                # searchable and counted like any other.
                role = text = None
                turn_cwd = ""
                if msg_type in ("user", "assistant"):
                    msg  = obj.get("message", {})
                    role = msg.get("role", msg_type)
                    text = _extract_claude_text(msg.get("content", ""))
                    turn_cwd = obj.get("cwd", "") if msg_type == "user" else ""
                elif msg_type == "attachment":
                    prompt = _human_attachment(obj)
                    if prompt is not None:
                        role, text = "user", _extract_claude_text(prompt)
                        turn_cwd = obj.get("cwd", "")

                if role and text and text.strip():
                    conn.execute(
                        "INSERT INTO messages(session_id,project,role,content,ts) VALUES(?,?,?,?,?)",
                        [session_id, project, role, text, ts]
                    )
                    total += 1
                    if role == "assistant":
                        assistant_turns_this_pass += 1
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
                        turn_cwd,
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

            # Refresh the durable transcript archive so this session survives
            # Claude's ~30-day cleanup of the source file.
            _write_archive(session_id, "claude", jsonl_file)

            if assistant_turns_this_pass > 0:
                if prefix_input > 0:
                    conn.execute("""
                        INSERT INTO stats(key, value) VALUES('input_tokens_saved', ?)
                        ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
                    """, [prefix_input * assistant_turns_this_pass])
                if prefix_cache > 0:
                    conn.execute("""
                        INSERT INTO stats(key, value) VALUES('cache_tokens_saved', ?)
                        ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
                    """, [prefix_cache * assistant_turns_this_pass])
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
    with open(CODEX_SESSION_IDX, encoding="utf-8") as f:
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

            # Extract UUID from filename: rollout-YYYY-MM-DDThh-mm-ss-{uuid}.jsonl
            m = _UUID_RE.search(jsonl_file.stem)
            if not m:
                continue
            session_id  = m.group(0)

            if stat.st_size <= last_offset:
                if not _archive_path(session_id, "codex").exists():
                    _write_archive(session_id, "codex", jsonl_file)
                continue

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
            _write_archive(session_id, "codex", jsonl_file)
        except Exception as e:
            if _debug_enabled():
                print(f"[clexo sync codex] {jsonl_file}: {e}", file=sys.stderr)
            continue

    conn.commit()
    return total


# ── Grok Build sessions (additive, does not touch Claude/Codex logic) ────────

def _sync_grok(conn: sqlite3.Connection) -> int:
    """Index Grok Build sessions from ~/.grok/sessions.

    We only touch files under GROK_SESSIONS. Existing Claude/Codex behavior
    is completely untouched.
    """
    if not GROK_SESSIONS.exists():
        return 0

    total = 0

    # Walk all chat_history.jsonl files (cleaner for conversational content)
    for chat_file in GROK_SESSIONS.glob("*/*/chat_history.jsonl"):
        try:
            # Derive session_id from parent directory name
            session_id = chat_file.parent.name
            # Derive project/cwd from the grandparent directory name (URL-encoded)
            encoded_cwd = chat_file.parent.parent.name
            project = encoded_cwd.replace("%2F", "/").lstrip("/")

            # Track offset for incremental sync (reuse the same file_state table)
            row = conn.execute(
                "SELECT last_offset FROM file_state WHERE filepath = ?",
                [str(chat_file)]
            ).fetchone()
            last_offset = row[0] if row else 0

            new_offset = chat_file.stat().st_size
            if new_offset <= last_offset:
                continue

            with open(chat_file, "rb") as f:
                f.seek(last_offset)
                for raw_line in f:
                    if not raw_line.strip():
                        continue
                    try:
                        obj = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue

                    msg_type = obj.get("type")
                    if msg_type not in ("user", "assistant"):
                        continue

                    content = obj.get("content", [])
                    texts = []
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                texts.append(part.get("text", ""))
                    elif isinstance(content, str):
                        texts.append(content)

                    text = "\n".join(t for t in texts if t).strip()
                    if not text or _is_system_noise(text):
                        continue

                    role = "user" if msg_type == "user" else "assistant"
                    ts = obj.get("timestamp") or ""

                    conn.execute("""
                        INSERT INTO messages(session_id, project, role, content, ts)
                        VALUES (?, ?, ?, ?, ?)
                    """, [session_id, project, role, text, ts])

                    total += 1

                    # Ensure session row exists
                    conn.execute("""
                        INSERT OR IGNORE INTO sessions(session_id, project, first_user_msg, last_ts, cwd, source)
                        VALUES (?, ?, '', ?, ?, 'grok')
                    """, [session_id, project, ts, project])

            conn.execute("INSERT OR REPLACE INTO file_state VALUES(?, ?)", [str(chat_file), new_offset])

        except Exception as e:
            if _debug_enabled():
                print(f"[clexo sync grok] {chat_file}: {e}", file=sys.stderr)
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
            with open(files[0], encoding="utf-8") as f:
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


def _config_get(key, default=None):
    """Read a single key from ~/.clexo/config.json, tolerating a missing or
    malformed file (returns `default`)."""
    try:
        return json.loads((CLEXO_DIR / "config.json").read_text(encoding="utf-8")).get(key, default)
    except Exception:
        return default


def _debug_enabled() -> bool:
    """True if ~/.clexo/config.json has "debug": true. Used to gate verbose logging."""
    return bool(_config_get("debug"))


def _default_search_scope() -> str:
    """Default scope for `clexo search`, from the `default_search_scope` config key:
    'pwd' restricts to the current working directory unless --all is passed; anything
    else (the default) searches every directory."""
    val = str(_config_get("default_search_scope", "all") or "all").strip().lower()
    return "pwd" if val == "pwd" else "all"


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
    total = _sync_claude(conn) + _sync_codex(conn) + _sync_grok(conn)
    _backfill_claude_titles(conn)
    _prune_archives(conn)
    if total > 0:
        _stat("messages_indexed", total, conn)
    if owned:
        conn.close()
    _last_sync_ts = time.time()
    return total


# ── helpers for search output ─────────────────────────────────────────────────

def _resume_binary(source: str) -> str:
    """Return the correct CLI binary for a given session source."""
    if source == "grok":
        return "grok"
    return "claude"


def _resume_argv(source: str, session_id: str) -> list[str]:
    """argv that fully resumes a session, dispatched by source.

    codex uses a positional subcommand (`codex resume <id>`); claude and grok
    take the id behind a `--resume` flag."""
    if source == "codex":
        return ["codex", "resume", session_id]
    binary = _resume_binary(source)
    return [binary, "--resume", session_id]


def _resume_cmd(source: str, session_id: str) -> str:
    """Paste-ready full-resume command for a session, correct for its source."""
    return " ".join(_resume_argv(source, session_id))


# ── Centralized resume / load exec (the only places that launch a CLI) ────────

def _ensure_snapshot(session_id: str) -> str | None:
    """Make sure a snapshot exists for a session, building one if absent.
    Returns an error string if the session can't be found anywhere (live or
    archive), else None. refresh_save reads through the archive gate, so this
    works even after Claude has reaped the live file."""
    chain_f   = CLEXO_DIR / f"chain-{session_id}.md"
    refresh_f = CLEXO_DIR / f"refresh-{session_id}.md"
    if chain_f.exists() or refresh_f.exists():
        return None
    result = refresh_save(session_id)
    return result if result.startswith("No session JSONL") else None


def _exec_load(session_id: str, source: str = "claude", allow_print: bool = True) -> None:
    """Load a session's compacted snapshot into a fresh CLI session (the
    SessionStart hook injects it). Builds the snapshot first if missing — works
    for reaped sessions, since the snapshot is rebuilt from the archive."""
    err = _ensure_snapshot(session_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)
    REFRESH_PENDING.write_text(session_id, encoding="utf-8")
    # User ran `clexo load` deliberately — let the SessionStart hook restore it
    # even when the fresh session starts in a different directory than the saved
    # one. The same-directory safeguard only applies to automatic restores.
    REFRESH_EXPLICIT.write_text(session_id, encoding="utf-8")
    binary = _resume_binary(source or "claude")
    if sys.stdout.isatty():
        os.execvp(binary, [binary])
    elif allow_print:
        print(f"Snapshot ready for {session_id}. Run: {binary}")


def _exec_resume(session_id: str, source: str = "claude", allow_print: bool = True) -> None:
    """The single resume entry point. Degrades gracefully when the live file is
    gone: live source present → true `<cli> --resume`; reaped but archived →
    load clexo's reconstructed snapshot into a fresh session instead."""
    source = source or "claude"
    if _live_session_jsonl(session_id, source):
        argv = _resume_argv(source, session_id)
        if sys.stdout.isatty():
            os.execvp(argv[0], argv)
        elif allow_print:
            print(_resume_cmd(source, session_id))
        return
    # Live file reaped by Claude/Codex — fall back to the archived snapshot.
    if _materialize_archive(session_id, source):
        print(f"Live session {session_id[:8]}… was reaped by {source}; loading "
              f"clexo's archived snapshot into a fresh session instead.",
              file=sys.stderr)
        _exec_load(session_id, source, allow_print=allow_print)
        return
    print(f"Session {session_id[:8]}… not found: no live file and no clexo archive.",
          file=sys.stderr)
    sys.exit(1)


# Single source of truth for prefixes that mark system-injected (non-user) text.
# Kept broad enough to catch variants ("# Context from ..." with various tails).
_NOISE_PREFIXES = (
    "# Context from",
    "# Files mentioned by the user",
    "A previous agent",
    "Base directory for this skill:",
    # Path echo Claude Code injects alongside a pasted image — redundant with the
    # "[Image #N]" already in the user's own message, not something they typed.
    "[Image: source:",
)

# Harness / API error strings that surface as assistant "messages" but carry no
# content — a dead-context turn, not something worth restoring or indexing. Left
# unfiltered, one of these (e.g. a session that died on "Prompt is too long")
# could be the only assistant line a snapshot keeps.
_ERROR_MESSAGES = (
    "Prompt is too long",
    "API Error",
    "Request interrupted",
)

def _is_system_noise(text: str) -> bool:
    """True if text is system-injected or a harness/API error — XML/HTML tags,
    known injected prefixes, or dead-context error strings — not real content."""
    t = text.strip()
    if t.startswith("<"):
        return True
    if any(t.startswith(e) for e in _ERROR_MESSAGES):
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


_TAG_STOP_WORDS = {
    "how", "what", "why", "can", "could", "would", "will", "should",
    "do", "does", "did", "is", "are", "was", "were", "the", "a", "an",
    "please", "hey", "hi", "ok", "okay", "just", "also", "now",
    "let", "lets", "help", "me", "you", "i", "we", "us",
    "to", "of", "for", "in", "on", "at", "and", "or", "but",
    "this", "that", "these", "those", "it", "its",
    "ill", "im", "ive", "be", "been", "have", "has", "had",
}


def _slugify_words(text: str, max_words: int = 3, max_chars: int = 24) -> str:
    """Free text → tag-friendly slug. Strips stop words, picks leading content
    words, joins with '-', enforces length cap. Returns '' if no usable words."""
    if not text:
        return ""
    s = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    words = [w for w in s.split() if w not in _TAG_STOP_WORDS and len(w) >= 2]
    out = ""
    for w in words[:max_words]:
        candidate = (out + "-" + w) if out else w
        if len(candidate) > max_chars:
            break
        out = candidate
    return out


def _project_slug(project: str, cwd: str) -> str:
    """Derive a short project name from cwd (preferred) or the encoded JSONL
    project dir name. Returns '' if nothing usable."""
    name = ""
    if cwd:
        name = Path(cwd).name
    elif project:
        parts = [p for p in project.split("-") if p]
        cands = [p for p in parts if any(c.isalpha() for c in p) and len(p) >= 3]
        name = cands[-1] if cands else (parts[-1] if parts else "")
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


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


def _auto_tag_for_session(session_id: str, thread_name: str, first_user_msg: str,
                          project: str, cwd: str, db) -> str | None:
    """Generate a candidate tag name for the session.

    Returns None if the session is already tagged or no usable name can be built.
    Picks topic from thread_name → first_user_msg → timestamp fallback. Prepends
    a project slug unless it already appears in the topic. On name collisions,
    appends `-2`, `-3`, … up to `-999`.
    """
    existing = db.execute(
        "SELECT 1 FROM tags WHERE session_id=? LIMIT 1", [session_id]
    ).fetchone()
    if existing:
        return None

    proj  = _project_slug(project or "", cwd or "")
    topic = _slugify_words(thread_name or "") or _slugify_words(first_user_msg or "")
    if not topic:
        topic = datetime.datetime.now().strftime("save-%Y%m%d-%H%M")

    if proj and proj in topic.split("-"):
        candidate = topic
    elif proj:
        candidate = f"{proj}-{topic}"
    else:
        candidate = topic
    candidate = candidate.lstrip("-_")[:30].rstrip("-")
    if not candidate or _validate_tag(candidate):
        return None

    base = candidate
    n = 2
    while db.execute("SELECT 1 FROM tags WHERE tag=? LIMIT 1", [candidate]).fetchone():
        suffix = f"-{n}"
        candidate = (base[: 30 - len(suffix)] + suffix).rstrip("-")
        if _validate_tag(candidate):
            return None
        n += 1
        if n > 999:
            return None
    return candidate


def _apply_auto_tag(session_id: str, db) -> str | None:
    """Generate and insert an auto-tag for the session if it has none yet.

    Returns the tag name on success, or None if already tagged or no usable
    name can be built. Shared by `refresh_save` (auto-tag on first save) and
    bare `clexo tag` (auto-tag on demand).
    """
    sess = db.execute(
        "SELECT thread_name, first_user_msg, project, cwd "
        "FROM sessions WHERE session_id=?",
        [session_id],
    ).fetchone()
    candidate = _auto_tag_for_session(
        session_id,
        (sess[0] if sess else "") or "",
        (sess[1] if sess else "") or "",
        (sess[2] if sess else "") or "",
        (sess[3] if sess else "") or "",
        db,
    )
    if not candidate:
        return None
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    db.execute(
        "INSERT OR IGNORE INTO tags(tag, session_id, created_ts) VALUES(?,?,?)",
        [candidate, session_id, now],
    )
    db.commit()
    return candidate


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
                d = json.loads(sfile.read_text(encoding="utf-8"))
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


def _expand_id_prefix(prefix: str) -> str | None:
    """Expand a session-id prefix (e.g. the 8-char id shown in search results) to
    the full UUID when exactly one indexed session matches. Returns None if the
    prefix is too short, matches nothing, or is ambiguous."""
    p = prefix.lower().replace("-", "")
    if len(p) < 8 or any(c not in "0123456789abcdef" for c in p):
        return None
    try:
        rows = get_db().execute(
            "SELECT session_id FROM sessions WHERE REPLACE(session_id,'-','') LIKE ? LIMIT 2",
            [p + "%"],
        ).fetchall()
    except Exception:
        return None
    return rows[0][0] if len(rows) == 1 else None


def _resolve_session_or_tag(name_or_uuid: str) -> str:
    """Resolve `name_or_uuid` to a full session id. Tries, in order: a complete
    UUID (passthrough), a known tag, then an unambiguous id prefix (the short id
    printed in search results). Falls back to the input unchanged."""
    if not name_or_uuid:
        return name_or_uuid
    s = name_or_uuid.strip()
    if _TAG_UUID_RE.match(s.lower()):
        return s
    sid = _resolve_tag(s)
    if sid:
        return sid
    return _expand_id_prefix(s) or s


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
                      top: int = 8, idf_cache: dict | None = None,
                      n_sessions: int | None = None) -> list[str]:
    """Top TF-IDF keywords for a session.

    User messages weighted 3× (they're the topic signal); assistant text 1×.
    Filters: stopword list, min raw count 2 (drops typos/one-offs), letters
    and underscores only, 3–31 chars. IDF computed against the full FTS index;
    pass a shared `idf_cache` dict to amortize across multiple sessions, and
    `n_sessions` (the index-wide session count) to skip recomputing that
    constant per call.
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

    N = n_sessions if n_sessions is not None else \
        db.execute("SELECT COUNT(DISTINCT session_id) FROM messages").fetchone()[0]
    N = N or 1

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
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        matches = _SUMMARY_RE.findall(content)
        if matches:
            return matches[-1].strip()
    return ""


def _format_tags(short: bool = False, keywords: bool = False) -> str:
    db = get_db()
    rows = db.execute("""
        SELECT t.tag, t.session_id, t.created_ts,
               s.source, s.project, s.thread_name, s.summary, s.last_ts
        FROM tags t
        LEFT JOIN sessions s ON s.session_id = t.session_id
        ORDER BY COALESCE(s.last_ts, t.created_ts) DESC
    """).fetchall()
    if not rows:
        return "No tags yet. Create one with tag(name='<friendly-name>')."

    if short:
        # Compact listing: "@tag  YYYY-MM-DD", newest activity first (rows are
        # already date-sorted above). Date is the session's last activity
        # (last_ts), falling back to when the tag was created for sessions not
        # in the index.
        def _date(row):
            return (row[7] or row[2] or "")[:10]
        width = max(len(row[0]) for row in rows)
        out = [f"{len(rows)} tag(s):\n"]
        for row in rows:
            out.append(f"@{row[0]:<{width}}  {_date(row) or '?'}")
        return "\n".join(out)

    idf_cache: dict = {}
    # Keyword TF-IDF is the expensive part of this listing (~860 FTS lookups
    # for 50 tags), so it's opt-in via `keywords`. The index-wide session count
    # it needs is constant across all tags — compute it once, not per session.
    n_sessions = db.execute(
        "SELECT COUNT(DISTINCT session_id) FROM messages").fetchone()[0] \
        if keywords else 0
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
            if keywords:
                kws = _session_keywords(db, sid, top=8, idf_cache=idf_cache,
                                        n_sessions=n_sessions)
                if kws:
                    out.append(f"    Keywords: {', '.join(kws)}")
        out.append("")
    return "\n".join(out).rstrip()


def _format_saved(short: bool = False) -> str:
    """List sessions that have a saved clexo snapshot (chain-/refresh- files in
    ~/.clexo), newest snapshot first. These are reachable via
    `clexo load <fragment>` even when untagged."""
    snaps: dict = {}  # sid -> newest snapshot mtime
    for pat in ("chain-*.md", "refresh-*.md"):
        for f in CLEXO_DIR.glob(pat):
            # filenames are chain-<uuid>.md / refresh-<uuid>.md (split once: the
            # uuid itself contains hyphens). Skip control files like
            # chain-loaded / refresh-pending (no .md, won't match the glob).
            sid = f.stem.split("-", 1)[1] if "-" in f.stem else ""
            if not _TAG_UUID_RE.match(sid.lower()):
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if sid not in snaps or mtime > snaps[sid]:
                snaps[sid] = mtime
    if not snaps:
        return "No saved snapshots yet. Run `clexo save` (or save()) to create one."

    db = get_db()
    ordered = sorted(snaps.items(), key=lambda kv: kv[1], reverse=True)

    tag_map: dict = {}
    for tag, sid in db.execute("SELECT tag, session_id FROM tags").fetchall():
        tag_map.setdefault(sid, []).append(tag)

    def _meta(sid: str, mtime: float):
        row = db.execute(
            "SELECT COALESCE(source,'claude'), project, last_ts "
            "FROM sessions WHERE session_id=?", [sid]
        ).fetchone()
        src  = (row[0] if row else "claude") or "claude"
        proj = ((row[1] if row else "") or "").lstrip("-").replace("-", "/")
        # Prefer the indexed last-activity date; fall back to when the snapshot
        # file was written so older/unindexed sessions still show a date.
        last = ((row[2] if row else "") or "")[:10]
        if not last:
            last = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        return src, proj, last

    if short:
        out = [f"{len(ordered)} saved snapshot(s):\n"]
        for sid, m in ordered:
            _src, proj, last = _meta(sid, m)
            tags = tag_map.get(sid, [])
            tagstr = ("  " + " ".join(f"@{t}" for t in tags)) if tags else ""
            out.append(f"{sid[:8]}  {last}  {proj or '?'}{tagstr}")
        return "\n".join(out)

    out = [f"{len(ordered)} saved snapshot(s):\n"]
    for sid, m in ordered:
        src, proj, last = _meta(sid, m)
        frag = sid[:8]
        tags = tag_map.get(sid, [])
        tagstr = ("  " + " ".join(f"@{t}" for t in tags)) if tags else ""
        out.append(f"{frag}  [{src}] {proj or '?'}  (last: {last}){tagstr}")
        for j, line in enumerate(_session_summary(db, sid)):
            prefix = "Opening:" if j == 0 else "Last:"
            out.append(f"    {prefix} {line}")
        out.append(f"    clexo load {frag}")
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

# ── Durable transcript archive ────────────────────────────────────────────────
# Claude reaps session JSONLs after ~30 days (the cleanupPeriodDays default), so
# pick(), snapshots and load() — which all read the live file — silently lose old
# sessions. clexo keeps its own compact, gzipped transcript per session: every
# user/assistant turn and every tool *command*, but NOT tool output, thinking,
# images, snapshots or per-line metadata (tool output is either in git or re-run
# on current data, so it is dead weight here). The archive stays in the source's
# native JSONL shape, so the existing readers parse it unchanged once it is
# materialized back. Verbatim `claude --resume` needs the original file and is a
# non-goal; this serves clexo's own resume/recall instead.

def _archive_path(session_id: str, source: str) -> Path:
    return ARCHIVE_DIR / source / f"{session_id}.jsonl.gz"


def _human_attachment(obj: dict):
    """Prompt content of a queued-command attachment — a message the user typed
    while the agent was still working. Claude Code stores it as a
    ``type: "attachment"`` record with ``attachment.origin.kind == "human"``,
    outside the normal user/assistant stream, so any reader that gates on
    ``type in ("user", "assistant")`` silently drops a real human turn.

    Returns the prompt (a list of content blocks, or a str), or None if obj is
    not such an attachment."""
    if obj.get("type") != "attachment":
        return None
    att = obj.get("attachment")
    if not isinstance(att, dict):
        return None
    origin = att.get("origin")
    if not (isinstance(origin, dict) and origin.get("kind") == "human"):
        return None
    return att.get("prompt")


def _strip_archive_line(obj: dict, source: str) -> dict | None:
    """Reduce one source JSONL line to transcript essentials, or None to drop it.

    Keeps user/assistant text and tool-use commands; drops tool_result/
    toolUseResult, thinking, images and heavy metadata. Output keeps the
    source's native shape so _read_raw_messages parses it unchanged."""
    if source == "codex":
        p = obj.get("payload")
        if not isinstance(p, dict):
            return None
        t = obj.get("type")
        ts = obj.get("timestamp", "")
        if t == "event_msg" and p.get("type") == "user_message":
            return {"type": t, "timestamp": ts, "payload": p}
        if t == "response_item" and p.get("role") == "assistant":
            blocks = [b for b in p.get("content", [])
                      if isinstance(b, dict) and b.get("type") in ("output_text", "text")]
            if blocks:
                return {"type": t, "timestamp": ts,
                        "payload": {"role": "assistant", "content": blocks}}
        return None
    # claude / grok: type + message{role, content}
    if obj.get("type") not in ("user", "assistant"):
        # A prompt the user queued while the agent was working is a real human
        # turn stored as an attachment — keep it (text/commands only, per the
        # archive's drop-images policy) rather than dropping the turn entirely.
        prompt = _human_attachment(obj)
        if prompt is None:
            return None
        if isinstance(prompt, list):
            kept = [b for b in prompt if isinstance(b, dict)
                    and b.get("type") in ("text", "tool_use")]
            if not kept:
                return None
            content = kept
        elif isinstance(prompt, str) and prompt.strip():
            content = prompt
        else:
            return None
        return {"type": "user", "timestamp": obj.get("timestamp", ""),
                "message": {"role": "user", "content": content}}
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, list):
        kept = [b for b in content if isinstance(b, dict)
                and b.get("type") in ("text", "tool_use")]
        if not kept:
            return None
        content = kept
    elif isinstance(content, str):
        if not content.strip():
            return None
    else:
        return None
    return {"type": obj["type"], "timestamp": obj.get("timestamp", ""),
            "message": {"role": msg.get("role", obj["type"]), "content": content}}


def _write_archive(session_id: str, source: str, src_jsonl: Path) -> None:
    """(Re)write the gzipped transcript archive for one session. Best-effort:
    archiving must never break a sync."""
    try:
        out = _archive_path(session_id, source)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(src_jsonl, encoding="utf-8") as fin, \
                gzip.open(out, "wt", encoding="utf-8") as fout:
            for line in fin:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                kept = _strip_archive_line(obj, source)
                if kept is not None:
                    fout.write(json.dumps(kept, ensure_ascii=False) + "\n")
    except Exception as e:
        if _debug_enabled():
            print(f"[clexo archive] {session_id}: {e}", file=sys.stderr)


def _materialize_archive(session_id: str, source: str) -> Path | None:
    """Decompress a session's archive into a cache file the readers can open.
    Returns the cache path, or None if no archive exists."""
    arc = _archive_path(session_id, source)
    if not arc.exists():
        return None
    dest = ARCHIVE_CACHE / source / f"{session_id}.jsonl"
    try:
        if not dest.exists() or dest.stat().st_mtime < arc.stat().st_mtime:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(arc, "rt", encoding="utf-8") as fin:
                dest.write_text(fin.read(), encoding="utf-8")
        return dest
    except Exception:
        return None


def _archive_retention_days() -> int:
    """How long to keep clexo's transcript archives, from the
    `archive_retention_days` config key. 0 / absent = keep forever (default)."""
    try:
        val = json.loads((CLEXO_DIR / "config.json").read_text(encoding="utf-8")).get("archive_retention_days", 0)
        return int(val or 0)
    except Exception:
        return 0


def _prune_archives(conn: sqlite3.Connection) -> int:
    """Delete archives (and their cache files) for sessions whose last activity
    is older than the configured retention. No-op at the default (forever).
    Tagged sessions are always kept. Only ever touches clexo's own archive —
    never Claude's/Codex's source files."""
    days = _archive_retention_days()
    if days <= 0 or not ARCHIVE_DIR.exists():
        return 0
    cutoff = (datetime.datetime.now().astimezone()
              - datetime.timedelta(days=days)).isoformat()[:10]
    tagged = {r[0] for r in conn.execute("SELECT DISTINCT session_id FROM tags")}
    last_ts = {r[0]: (r[1] or "") for r in
               conn.execute("SELECT session_id, last_ts FROM sessions")}
    removed = 0
    for gz in ARCHIVE_DIR.glob("*/*.jsonl.gz"):
        sid = gz.name[:-len(".jsonl.gz")]
        if sid in tagged:
            continue
        lt = last_ts.get(sid, "")
        # Fall back to the archive's own mtime when the session isn't indexed.
        when = lt[:10] if lt else datetime.datetime.fromtimestamp(
            gz.stat().st_mtime).astimezone().isoformat()[:10]
        if when < cutoff:
            try:
                gz.unlink()
                (ARCHIVE_CACHE / gz.parent.name / gz.name[:-3]).unlink(missing_ok=True)
                removed += 1
            except Exception:
                pass
    return removed


def _live_session_jsonl(session_id: str, source: str = "claude") -> Path | None:
    """The on-disk source file, or None once Claude/Codex has reaped it. No
    archive fallback. For writers (sync, archive) and the resume liveness gate —
    callers that specifically need the *real* live file."""
    if source == "codex":
        hits = list(CODEX_SESSIONS.glob(f"**/*{session_id}*.jsonl"))
    else:
        hits = list(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _find_session_jsonl(session_id: str, source: str = "claude") -> Path | None:
    """THE reader gate. A readable JSONL for this session: the live source file
    if it still exists, else the materialized archive. Every reader path (pick,
    snapshot, load, show) resolves sessions through here, so the archive
    fallback lives in exactly one place. Writers must NOT call this — they use
    _live_session_jsonl / the sync globs directly."""
    return _live_session_jsonl(session_id, source) or _materialize_archive(session_id, source)


def _resolve_reader_jsonl(session_id: str) -> tuple[Path | None, str]:
    """Reader gate with source auto-detection: try claude then codex,
    live-or-archive. Returns (path or None, source)."""
    for src in ("claude", "codex"):
        p = _find_session_jsonl(session_id, src)
        if p:
            return p, src
    return None, "claude"


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


def _read_raw_messages(jsonl_file: Path, source: str = "claude") -> list[dict]:
    """All user/assistant messages from JSONL including tool_result blocks.

    `source` routes the parser; it matters for archive cache files, which live
    under ~/.clexo/cache/ rather than the original source tree."""
    if source == "codex" or CODEX_SESSIONS in jsonl_file.parents:
        return _read_raw_messages_codex(jsonl_file)
    msgs = []
    with open(jsonl_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                otype = obj.get("type")
                if otype not in ("user", "assistant"):
                    # A queued human prompt (typed while the agent worked) is a
                    # real turn stored as an attachment — surface it as user.
                    prompt = _human_attachment(obj)
                    if prompt is None:
                        continue
                    msgs.append({
                        "role":    "user",
                        "content": prompt,
                        "ts":      obj.get("timestamp", ""),
                    })
                    continue
                msg = obj.get("message", {})
                msgs.append({
                    "role":    msg.get("role", otype),
                    "content": msg.get("content", ""),
                    "ts":      obj.get("timestamp", ""),
                })
            except Exception:
                continue
    return msgs


def _read_raw_messages_codex(jsonl_file: Path) -> list[dict]:
    """Full history from a Codex JSONL — all turns across compaction boundaries."""
    msgs = []
    with open(jsonl_file, encoding="utf-8") as f:
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
    sections = _parse_chain_sections(f.read_text(encoding="utf-8"))
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
    uuids = _CHAIN_RE.findall(f.read_text(encoding="utf-8"))
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
        data = json.loads(best.read_text(encoding="utf-8"))
        cwd = data.get("cwd", "")
        if cwd:
            return Path(cwd).name
    except Exception:
        pass
    return raw


def _pwd_dir() -> str:
    """The directory to scope --pwd to: the current session's recorded cwd
    (env → DB) when available, else the shell's $PWD / os.getcwd(). Matches the
    cwd stored verbatim at index time, so an exact `s.cwd = ?` compare works.
    The DB lookup is what makes --pwd correct from the MCP server, whose own
    process cwd may not be the user's project dir."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if sid:
        try:
            row = get_db().execute(
                "SELECT cwd FROM sessions WHERE session_id=?", [sid]).fetchone()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
    return os.environ.get("PWD") or os.getcwd()


def _pwd_scope(pwd) -> bool:
    """Resolve the effective --pwd scope: an explicit True/False (from --pwd /
    --all, or the MCP `pwd` arg) wins; None consults the configured default."""
    if pwd is True:
        return True
    if pwd is False:
        return False
    return _default_search_scope() == "pwd"


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

# ── Output formatting (TTY-aware; shared by search + recent listing) ──────────
#
# One render path serves both the human CLI and the MCP tool. Color is emitted
# only for an interactive terminal (isatty); MCP and piped output stay plain so
# the model / downstream tools see clean text and full session ids.

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_RESET   = "\033[0m"
# FTS snippet() highlight delimiters. Control chars (STX/ETX), not '>>>'/'<<<',
# so they can't collide with literal '<'/'>' in content (XML tags, bash redirects).
_HL_OPEN  = "\x02"
_HL_CLOSE = "\x03"
_STYLE   = {
    "dim": "\033[2m", "bold": "\033[1m", "cyan": "\033[36m",
    "yellow": "\033[33m", "green": "\033[32m", "magenta": "\033[35m",
}
_SRC_STYLE = {"claude": "magenta", "codex": "yellow", "grok": "green"}
_HOME = str(Path.home())


def _use_color() -> bool:
    """Color only for an interactive terminal with NO_COLOR unset."""
    try:
        return bool(sys.stdout.isatty()) and not os.environ.get("NO_COLOR")
    except Exception:
        return False


def _paint(text: str, *styles: str, on: bool = True) -> str:
    if not on:
        return text
    pre = "".join(_STYLE[s] for s in styles if s in _STYLE)
    return f"{pre}{text}{_RESET}" if pre else text


def _term_width(default: int = 80) -> int:
    try:
        w = shutil.get_terminal_size((default, 20)).columns
        return w if w and w >= 40 else default
    except Exception:
        return default


def _vis_trunc(s: str, n: int) -> str:
    """Truncate to n visible chars, treating ANSI escapes as zero-width. Appends
    an ellipsis when cut and closes any open color so styling never bleeds.
    Collapses internal whitespace first so a multi-line match (e.g. Bash output)
    stays on one row."""
    if n <= 0:
        return ""
    s = re.sub(r"[ \t\r\n\f\v]+", " ", s)
    out, vis, i = [], 0, 0
    while i < len(s):
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group()); i = m.end(); continue
        if vis >= n:
            break
        out.append(s[i]); vis += 1; i += 1
    res = "".join(out)
    if i < len(s):
        res = res.rstrip() + "…"
    if "\033[" in res and not res.endswith(_RESET):
        res += _RESET
    return res


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _short_path(cwd: str, project: str = "") -> str:
    """Display path for a session. Prefer the real cwd (collapsing $HOME → ~);
    fall back to the de-mangled project name only when cwd is unknown — that
    fallback can't tell a real '/' from a '-' in the original directory name."""
    p = (cwd or "").strip().rstrip("/")
    if not p and project:
        demangled = project.lstrip("-").replace("-", "/")
        p = ("/" + demangled) if "/" in demangled else demangled
    if not p:
        return "?"
    if p == _HOME:
        return "~"
    if p.startswith(_HOME + "/"):
        return "~" + p[len(_HOME):]
    return p


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _match_core(entry: str) -> str:
    """The match snippet's text, minus the [role] tag, FTS ellipses and the
    highlight markers — used to detect overlap with opening/last."""
    _, sep, body = entry.partition("] ")
    if not sep:
        body = entry
    body = body.replace(_HL_OPEN, "").replace(_HL_CLOSE, "")
    return body.strip().strip(".")


def _match_redundant(entry: str, opening: str, last: str) -> bool:
    """True when the top match just restates the opening/last line (the common
    case where the query term lives in the first user message). Cards mode hides
    it then; --full always shows it."""
    core = _norm(_match_core(entry))
    if len(core) < 12:
        return False
    for other in (_norm(opening), _norm(last)):
        if other and (core in other or other in core):
            return True
    return False


def _render_match(entry: str, color: bool, budget: int, show_role: bool = False) -> str:
    """Render one match snippet: highlight the matched term (bold/yellow on a
    TTY, «guillemets» otherwise) and truncate to `budget` visible chars."""
    role, sep, body = entry.partition("] ")
    if not sep:
        role, body = "", entry
    if color:
        body = body.replace(_HL_OPEN, _STYLE["bold"] + _STYLE["yellow"]).replace(_HL_CLOSE, _RESET)
    else:
        body = body.replace(_HL_OPEN, "«").replace(_HL_CLOSE, "»")
    if show_role and role:
        body = _paint(role + "]", "dim", on=color) + " " + body
    return _vis_trunc(body, budget)


def _label(text: str, color: bool) -> str:
    return _paint(f"{text:<5}", "dim", on=color)


def _legend(color: bool, example_id: str) -> str:
    """Bottom hint, made copy-paste-able by using the last result's 8-char id —
    a fragment that `clexo resume`/`load` resolve to the full session."""
    return _paint(f"→ resume: clexo resume {example_id}    load: clexo load {example_id}",
                  "dim", on=color)


def _clamp_path(path: str, maxlen: int) -> str:
    if maxlen <= 1 or len(path) <= maxlen:
        return path
    return "…" + path[-(maxlen - 1):]


def _card_header(h: dict, color: bool, width: int, full_id: bool = False) -> str:
    """`N.  DATE  SRC  project ........... id` — id right-aligned to the terminal
    width. Default shows the 8-char id (a fragment that `clexo resume`/`load`
    resolve to the full session); --full prints the complete UUID. Color codes are
    zero visible-width, so padding is computed from the plain tokens and stays
    aligned whether or not color is on."""
    n_str = f"{h['n']}."
    date  = h["date"]
    src   = f"{h['source']:<6}"
    idv   = h["sid"] if full_id else h["sid"][:8]
    prefix = f"{n_str:<3} {date}  {src}  "
    path  = _clamp_path(h["path"], max(10, width - len(prefix) - len(idv) - 2))
    left_plain = prefix + path
    pad = max(2, width - len(left_plain) - len(idv))
    return (_paint(f"{n_str:<3}", "bold", on=color) + f" {date}  " +
            _paint(src, _SRC_STYLE.get(h["source"], ""), on=color) + "  " +
            _paint(path, "cyan", on=color) + " " * pad +
            _paint(idv, "dim", on=color))


def _render_cards(hits: list, header: str, color: bool, mode: str) -> str:
    width  = _term_width()
    budget = max(20, width - 10)
    out = [header, ""]
    for h in hits:
        out.append(_card_header(h, color, width, full_id=(mode == "full")))
        if h.get("title"):
            out.append("    " + _label("title", color) + " " +
                       _vis_trunc(f'"{h["title"]}"', budget))
        if h.get("opening"):
            out.append("    " + _label("open", color) + " " + _vis_trunc(h["opening"], budget))
        if h.get("last") and _norm(h["last"]) != _norm(h.get("opening") or ""):
            out.append("    " + _label("last", color) + " " + _vis_trunc(h["last"], budget))
        matches = h.get("matches") or []
        if mode == "full":
            for entry in matches[:2]:
                out.append("    " + _label("hit", color) + " " +
                           _render_match(entry, color, budget, show_role=True))
        elif matches and not _match_redundant(matches[0], h.get("opening") or "", h.get("last") or ""):
            out.append("    " + _label("hit", color) + " " +
                       _render_match(matches[0], color, budget))
        out.append("")
    out.append(_legend(color, hits[-1]["sid"][:8]))
    return "\n".join(out)


def _render_oneline(hits: list, header: str, color: bool) -> str:
    width = _term_width()
    nw  = max(1, max(len(str(h["n"])) for h in hits))
    pw  = min(24, max(len("PROJECT"), max(len(h["path"]) for h in hits)))
    idw = 8
    prefix_len = nw + 2 + 10 + 2 + 6 + 2 + pw + 2 + idw + 2
    mbudget = max(16, width - prefix_len)
    hdr = (f"{'#':<{nw}}  {'DATE':<10}  {'SRC':<6}  "
           f"{'PROJECT':<{pw}}  {'ID':<{idw}}  MATCH")
    out = [header, "", _paint(hdr, "dim", on=color)]
    for h in hits:
        idv   = h["sid"][:8]
        path  = _clamp_path(h["path"], pw)
        match = _render_match(h["matches"][0], color, mbudget) if h.get("matches") else ""
        out.append(
            _paint(f"{h['n']:<{nw}}", "bold", on=color) + f"  {h['date']:<10}  " +
            _paint(f"{h['source']:<6}", _SRC_STYLE.get(h["source"], ""), on=color) + "  " +
            _paint(f"{path:<{pw}}", "cyan", on=color) + "  " +
            _paint(f"{idv:<{idw}}", "dim", on=color) + "  " + match)
    out.append("")
    out.append(_legend(color, hits[-1]["sid"][:8]))
    return "\n".join(out)


def _render_hits(hits: list, header: str, mode: str, color: bool) -> str:
    if not hits:
        return header
    if mode == "oneline":
        return _render_oneline(hits, header, color)
    return _render_cards(hits, header, color, mode)


def _build_hit(db, n: int, sid: str, ts: str, source: str, path: str,
               thread_name: str, snippets: list) -> dict:
    summary = _session_summary(db, sid)
    return {
        "n": n,
        "date": ts[:10] if ts else "?",
        "source": source,
        "path": path,
        "title": thread_name if source == "codex" else "",
        "opening": summary[0] if summary else None,
        "last": summary[-1] if len(summary) > 1 else None,
        "matches": snippets,
        "sid": sid,
    }


# Session-level ranking for `_search`: relevance and recency are weighted
# evenly — every candidate already matched the query, so recency still
# matters as much as how well it matches. Recency halves every
# _SEARCH_RECENCY_HALF_LIFE_DAYS.
_SEARCH_RECENCY_WEIGHT = 0.5
_SEARCH_RECENCY_HALF_LIFE_DAYS = 14.0


def _recency_score(ts: str, half_life_days: float = _SEARCH_RECENCY_HALF_LIFE_DAYS) -> float:
    """Exponential-decay recency score in (0, 1]: 1.0 for 'now', 0.5 at
    `half_life_days` old. Returns a neutral 0.5 if `ts` fails to parse."""
    try:
        when = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.datetime.now(when.tzinfo) if when.tzinfo else datetime.datetime.now()
        age_days = max(0.0, (now - when).total_seconds() / 86400)
    except Exception:
        return 0.5
    return 0.5 ** (age_days / half_life_days)


def _search(query: str, limit: int = 10, project_filter: str = "",
            source_filter: str = "", cwd_filter: str = "", mode: str = "cards",
            sort: str = "relevance") -> str:
    """Search indexed sessions. Returns a formatted string. `mode` selects the
    layout — 'cards' (default), 'full' (open+last+all matches), or 'oneline'.
    `cwd_filter`, when set, restricts results to sessions whose working directory
    is exactly that path (the --pwd scope). `sort="time"` displays the selected
    results in ascending chronological order (oldest first, latest last) instead
    of by combined relevance/recency rank; selection is unaffected."""
    db = get_db()
    sync_all(db, throttle=True)
    _stat("search_calls", conn=db)
    color = _use_color()

    # If query has no explicit FTS5 operators, quote it so special chars (. @ /) are safe
    _fts_ops = {"AND", "OR", "NOT"}
    if not any(op in query.split() for op in _fts_ops) and '"' not in query:
        fts_query = f'"{query}"'
    else:
        fts_query = query

    # Conditions sans cwd, so the same query can be counted across every
    # directory when scoping is active (to tell the user what --all would add).
    base_conditions = ["messages MATCH ?"]
    base_params: list = [fts_query]
    if project_filter:
        base_conditions.append("m.project LIKE ?")
        base_params.append(f"%{project_filter}%")
    if source_filter:
        base_conditions.append("s.source = ?")
        base_params.append(source_filter)

    conditions = list(base_conditions)
    params = list(base_params)
    if cwd_filter:
        conditions.append("s.cwd = ?")
        params.append(cwd_filter)
    where = " AND ".join(conditions)

    # Pull a candidate pool of raw matches ranked by BM25 — wide enough that
    # the session-level re-ranking below has real sessions to choose from.
    # Limiting to `limit` raw rows here (as before) let one chatty session
    # occupy several slots and crowd other matching sessions out of the
    # results entirely, no matter how recent or relevant they were.
    pool_size = min(500, max(200, limit * 20))
    try:
        pool = db.execute(f"""
            SELECT m.session_id, rank, m.ts
            FROM messages m
            JOIN sessions s ON s.session_id = m.session_id
            WHERE {where}
            ORDER BY rank LIMIT ?
        """, params + [pool_size]).fetchall()
    except Exception as e:
        return f"Search error: {e}"

    # How many sessions in *other* directories match the same query — so a
    # cwd-scoped search is never silently lossy. Only computed when scoping.
    more_elsewhere = 0
    if cwd_filter:
        try:
            total_all = db.execute(
                f"SELECT COUNT(DISTINCT m.session_id) FROM messages m "
                f"JOIN sessions s ON s.session_id = m.session_id "
                f"WHERE {' AND '.join(base_conditions)}", base_params).fetchone()[0]
            here = db.execute(
                f"SELECT COUNT(DISTINCT m.session_id) FROM messages m "
                f"JOIN sessions s ON s.session_id = m.session_id "
                f"WHERE {where}", params).fetchone()[0]
            more_elsewhere = max(0, (total_all or 0) - (here or 0))
        except Exception:
            more_elsewhere = 0

    scope = f" in {_short_path(cwd_filter)}" if cwd_filter else ""
    if not pool:
        if cwd_filter and more_elsewhere:
            return (f"No results for '{query}'{scope} — but {more_elsewhere} "
                    f"in other directories. Use --all to search everywhere.")
        widen = "  (use --all to search every directory)" if cwd_filter else ""
        return f"No results for '{query}'{scope}.{widen}"

    # Best (lowest = most relevant) BM25 rank per session, plus the timestamp
    # of that matching message. Dedup happens here, before any limit is
    # applied — a session can no longer be excluded just because some other
    # session's messages happened to fill the pool first.
    best: dict = {}
    for session_id, rank, ts in pool:
        cur = best.get(session_id)
        if cur is None or rank < cur[0]:
            best[session_id] = (rank, ts)

    ranked = sorted(best.items(), key=lambda kv: kv[1][0])
    n = len(ranked)
    scored = []
    for i, (session_id, (rank, ts)) in enumerate(ranked):
        relevance_score = 1.0 - (i / (n - 1)) if n > 1 else 1.0
        combined = (_SEARCH_RECENCY_WEIGHT * _recency_score(ts) +
                   (1 - _SEARCH_RECENCY_WEIGHT) * relevance_score)
        scored.append((combined, session_id, ts))
    scored.sort(key=lambda t: -t[0])
    winners = scored[:limit]
    if sort == "time":
        winners.sort(key=lambda t: t[2])  # ascending ts — oldest first, latest last

    placeholders = ",".join("?" for _ in winners)
    snippet_rows = db.execute(f"""
        SELECT m.session_id, m.role,
               snippet(messages, 3, char(2), char(3), '...', 20) AS snip
        FROM messages m
        WHERE messages MATCH ? AND m.session_id IN ({placeholders})
        ORDER BY rank
    """, [fts_query] + [sid for _, sid, _ in winners]).fetchall()

    # Cap snippets per session — the renderer only ever shows the first one
    # or two anyway, and an unbounded chatty session shouldn't dominate here.
    seen: dict = {sid: {"snippets": [], "ts": ts} for _, sid, ts in winners}
    for session_id, role, snip in snippet_rows:
        entry = seen.get(session_id)
        if entry is not None and len(entry["snippets"]) < 3:
            entry["snippets"].append(f"[{role}] {snip}")

    for sid, info in seen.items():
        sess = db.execute(
            "SELECT project, first_user_msg, cwd, source, thread_name FROM sessions WHERE session_id=?",
            [sid]
        ).fetchone()
        project = (sess[0] or "") if sess else ""
        cwd     = (sess[2] or "") if sess else ""
        info["source"]      = (sess[3] or "claude") if sess else "claude"
        info["thread_name"] = (sess[4] or "")       if sess else ""
        info["path"]        = _short_path(cwd, project)

    hits = [_build_hit(db, i, sid, seen[sid]["ts"], seen[sid]["source"], seen[sid]["path"],
                       seen[sid]["thread_name"], seen[sid]["snippets"])
            for i, (_, sid, _) in enumerate(winners, 1)]
    header = f'{_plural(len(hits), "session")} · "{query}"{scope}'
    out = _render_hits(hits, header, mode, color)
    if cwd_filter:
        if more_elsewhere:
            out += (f"\n\n+{more_elsewhere} more in other directories · "
                    f"use --all to include them")
        else:
            out += "\n\nscoped to this directory · use --all to search everywhere"
    return out


def _list_recent_sessions(limit: int = 10, project_filter: str = "",
                          source_filter: str = "", cwd_filter: str = "",
                          mode: str = "cards", sort: str = "relevance") -> str:
    """List recent sessions newest-first, optionally narrowed by project / source
    / working directory. Used when search is called with an empty query.
    `sort="time"` reverses the same selected sessions to oldest-first display."""
    db = get_db()
    sync_all(db, throttle=True)
    color = _use_color()
    conditions: list[str] = []
    params: list = []
    if project_filter:
        conditions.append("project LIKE ?")
        params.append(f"%{project_filter}%")
    if source_filter:
        conditions.append("source = ?")
        params.append(source_filter)
    if cwd_filter:
        conditions.append("cwd = ?")
        params.append(cwd_filter)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = db.execute(f"""
        SELECT session_id, project, cwd, last_ts, source, thread_name
        FROM sessions {where}
        ORDER BY last_ts DESC LIMIT ?
    """, params + [limit]).fetchall()
    if sort == "time":
        rows = list(reversed(rows))
    scope = f" in {_short_path(cwd_filter)}" if cwd_filter else ""
    if not rows:
        widen = "  (use --all to list every directory)" if cwd_filter else ""
        return f"No sessions indexed yet{scope}.{widen}"
    hits = [_build_hit(db, i, sid, ts, src or "claude",
                       _short_path(cwd, project), thread_name, [])
            for i, (sid, project, cwd, ts, src, thread_name) in enumerate(rows, 1)]
    header = f'{_plural(len(hits), "recent session")}{scope}'
    out = _render_hits(hits, header, mode, color)
    if cwd_filter:
        out += "\n\nscoped to this directory · use --all to list every directory"
    return out


def search_sessions(query: str = "", limit: int = 10, project_filter: str = "",
                    source_filter: str = "", pwd=None, mode: str = "cards",
                    sort: str = "relevance") -> str:
    """Full-text search across the archive, or list recent sessions when the
    query is empty. Resolves "this"/"cwd"/"." project aliases and applies the
    --pwd working-directory scope (`pwd` True/False overrides the configured
    default). `sort="time"` displays the same results in ascending chronological
    order (oldest first, latest last) instead of by relevance/recency rank.
    Shared entry point for the MCP `search` tool and `clexo search`."""
    pf = _resolve_project_filter(project_filter)
    cwd_filter = _pwd_dir() if _pwd_scope(pwd) else ""
    if not query.strip():
        return _list_recent_sessions(limit, pf, source_filter, cwd_filter, mode, sort)
    return _search(query, limit=limit, project_filter=pf,
                   source_filter=source_filter, cwd_filter=cwd_filter, mode=mode,
                   sort=sort)


# Flags the `clexo search` CLI understands; each takes a value. The rest of the
# argv is joined into the FTS query. Both `--flag value` and `--flag=value` work.
_SEARCH_FLAGS = {
    "--source_filter":  "source_filter",
    "--source":         "source_filter",
    "--project_filter": "project_filter",
    "--project":        "project_filter",
    "--limit":          "limit",
}

# Valueless flags: --pwd / --all scope to (or out of) the current directory;
# --oneline / --full pick the result layout; -t / --time switch to ascending
# chronological display (oldest first, latest last).
_SEARCH_BOOL_FLAGS = {"--pwd", "--all", "--oneline", "--full", "-t", "--time"}

def _parse_search_args(args: list[str]) -> tuple[str, dict]:
    """Split `clexo search` argv into (query, opts), pulling out --flags so they
    aren't swallowed into the FTS query."""
    opts = {"source_filter": "", "project_filter": "", "limit": 10,
            "pwd": None, "mode": "cards", "sort": "relevance"}
    query: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in _SEARCH_BOOL_FLAGS:
            if a == "--pwd":
                opts["pwd"] = True
            elif a == "--all":
                opts["pwd"] = False
            elif a == "--oneline":
                opts["mode"] = "oneline"
            elif a == "--full":
                opts["mode"] = "full"
            elif a in ("-t", "--time"):
                opts["sort"] = "time"
            i += 1
            continue
        key = val = None
        if a.split("=", 1)[0] in _SEARCH_FLAGS and "=" in a:
            name, val = a.split("=", 1)
            key = _SEARCH_FLAGS[name]
        elif a in _SEARCH_FLAGS:
            key = _SEARCH_FLAGS[a]
            val = args[i + 1] if i + 1 < len(args) else ""
            i += 1
        else:
            query.append(a)
        if key == "limit":
            try:
                opts["limit"] = int(val)
            except (TypeError, ValueError):
                pass
        elif key:
            opts[key] = val
        i += 1
    return " ".join(query), opts


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
        # Reader path → through the archive-aware gate, so a snapshot can still
        # be built after Claude has reaped the live file.
        jsonl, source = _resolve_reader_jsonl(session_id)
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
        "SELECT summary, first_user_msg, last_prompt, thread_name, project, cwd "
        "FROM sessions WHERE session_id=?",
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
        cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    chars_min = cfg.get("refresh_tokens_min", 4000) * 4
    chars_max = cfg.get("refresh_tokens_max", 8000) * 4

    msgs = []
    full_chars = 0
    with open(jsonl, encoding="utf-8") as f:
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
                    otype = obj.get('type')
                    if otype == 'attachment':
                        # A prompt the user typed while the agent was still working
                        # is stored as a queued-command attachment, not a `user`
                        # record. It's a real human turn — without it the run of
                        # assistant work between two queued prompts looks like one
                        # unbroken monologue.
                        prompt = _human_attachment(obj)
                        if prompt is not None:
                            text = _extract_content(prompt, include_results=False)
                            if text and not _is_system_noise(text):
                                full_chars += len(text)
                                msgs.append(('user', text, ts))
                        continue
                    if otype not in ('user', 'assistant'): continue
                    msg      = obj.get('message', {})
                    role     = msg.get('role', otype)
                    raw      = msg.get('content', '')
                    text     = _extract_content(raw, include_results=False)
                    if text and not _is_system_noise(text):
                        msgs.append((role, text, ts))
            except Exception:
                continue

    # Keep the most-recent tail of the conversation as-is: every user turn and
    # every assistant message, in order, within the char budget. Tool *output*
    # and system/error noise are already filtered out above; the assistant's
    # reasoning and actions are the whole point of a restore snapshot, so nothing
    # on that side is collapsed or dropped.
    #
    # The old logic paired each user turn with only the *last* assistant message
    # of the following run and threw the rest away. On a long agentic stretch —
    # especially one where the user's prompts were queued (and thus invisible, so
    # the whole stretch read as one assistant run) — that erased hours of work,
    # keeping only the final message, which could be a dead-context error.
    selected, total = [], 0
    for turn in reversed(msgs):
        chunk = len(turn[1])
        if selected and total + chunk > chars_max:
            break
        selected.append(turn)
        total += chunk
        if total >= chars_min:
            break
    selected.reverse()

    total_turns = len(msgs)
    exchanges = []
    for role, mtext, mts in selected:
        tag = "USER" if role == "user" else "ASSISTANT"
        exchanges.append(f"[{tag}] {mts[:19]}\n{mtext[:20000]}\n")

    file_refs = sorted(set(
        re.findall(r'(?:/Users/\w[^\s,\'")\]]{5,}|~/.claude/\S+|~/Code/\S+)',
                   '\n'.join(exchanges))
    ))[:20]

    date            = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    # Prefer the indexed project name; jsonl.parent is meaningless for an archive
    # cache file (it lives under ~/.clexo/cache/<source>/).
    project_raw     = (sess[4] if sess and len(sess) > 4 and sess[4] else jsonl.parent.name)
    project_display = (project_raw or "").lstrip('-').replace('-', '/')
    section_header  = f"## Session {session_id} | {date} | {source} | {project_display}"
    # If older turns didn't fit, say so up front and over the *original*
    # conversation — a reader never sees the snapshot's internal tail, so a count
    # relative to it is meaningless. The total also lets the hook packer report
    # elision over the whole session. pick() retrieves any earlier turn.
    exch_intro = ""
    if total_turns > len(selected):
        exch_intro = (f"(showing the most recent {len(selected)} of {total_turns} "
                      f"exchanges — call pick() to retrieve earlier ones)\n\n")
    section_body    = (
        f"### Summary\n{summary}\n\n"
        f"### Key files\n" + ('\n'.join(file_refs) or '(none)') + "\n\n"
        f"### Recent exchanges\n" + exch_intro + '\n'.join(exchanges)
    )
    new_section = f"{section_header}\n\n{section_body}"

    global _loaded_session_id
    chain_sid = _loaded_session_id
    if not chain_sid and _CHAIN_LOADED.exists():
        chain_sid = _CHAIN_LOADED.read_text(encoding="utf-8").strip()
        _CHAIN_LOADED.unlink(missing_ok=True)

    prior_content = ""
    if chain_sid and chain_sid != session_id:
        old_chain = CLEXO_DIR / f"chain-{chain_sid}.md"
        if old_chain.exists():
            prior_content = old_chain.read_text(encoding="utf-8").rstrip() + "\n\n"
            old_chain.unlink()
        else:
            old_refresh = CLEXO_DIR / f"refresh-{chain_sid}.md"
            if old_refresh.exists():
                prior_content = f"## Session {chain_sid} | [migrated]\n\n{old_refresh.read_text(encoding='utf-8')}\n\n"
                old_refresh.unlink()

    chain_file = CLEXO_DIR / f"chain-{session_id}.md"
    chain_file.write_text(prior_content + new_section, encoding="utf-8")
    _loaded_session_id = session_id
    REFRESH_PENDING.write_text(session_id, encoding="utf-8")
    _stat("refresh_saves", conn=db)

    total_chars = len(prior_content) + len(new_section)
    snap_tok = total_chars // 4

    # Real prefix size of the source session at save time. For Claude we read
    # the last assistant turn's `usage` (input + cache_read + cache_creation)
    # — that's the API prefix this snapshot eliminates. For Codex (no usage
    # data) fall back to extracted-text bytes.
    if source == 'claude':
        src_input, src_cache = _claude_source_prefix(jsonl)
        src_tok = src_input + src_cache
    else:
        src_tok = full_chars // 4

    tokens_compacted = max(0, src_tok - snap_tok)
    try:
        db.execute("""
            INSERT INTO stats(key, value) VALUES('tokens_compacted', ?)
            ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
        """, [tokens_compacted])
        db.commit()
    except Exception:
        pass

    lines = [f"Wrote snapshot: {total_chars:,} chars ≈ {_human_count(snap_tok)} tokens"]
    if src_tok > snap_tok and src_tok > 0:
        pct = round((1 - snap_tok / src_tok) * 100)
        lines.append(f"Compacted from ~{_human_count(src_tok)} prefix tokens ({pct}% smaller)")
    # No auto-tag — a snapshot is reachable by its id fragment; surface it for
    # manual reload. `clexo tag <name>` is available if a friendly name is wanted.
    lines.append(f"Reload later: clexo load {session_id[:8]}")
    lines.append("Run /clear — snapshot auto-restores on next message.")
    return "\n".join(lines)


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
            "  search  — find which session (FTS5; supports project_filter, "
            "source_filter='claude'|'codex', and pwd=true/false to scope to the "
            "current directory; empty query lists recent)\n"
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
               source_filter: str = "", pwd: bool | None = None,
               sort: str = "relevance") -> str:
        """Full-text search across the indexed archive of all past Claude Code,
        Codex, and Grok Build sessions — every user message, assistant reply,
        and tool result. The broad entry point for any backward reference to
        prior conversation.

        Use whenever the user points back to earlier work not in current
        context — e.g. "did we discuss the CSRF fix?", "remember when we
        migrated the database?", "what was that nginx config?", "find that
        session about the OOM bug", "list recent sessions in this project".

        **To search ONLY Grok Build sessions** (recommended when working inside Grok):
            Use source_filter="grok"
            Example: search("auth middleware", source_filter="grok")

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
            source_filter: "claude", "codex", or "grok" to restrict to sessions from one AI.
                           Use source_filter="grok" to search only Grok Build history.
            pwd: Working-directory scope. None (default) follows the user's
                 configured default; True restricts to sessions started in the
                 current directory; False searches every directory. If a result
                 says "+N more in other directories · use --all", that is the CLI
                 hint — call search again with pwd=False to widen across all dirs.
            sort: "relevance" (default, blends text match with recency) or
                  "time" — same selected results, displayed oldest first so
                  the most recent session lands last.
        """
        return search_sessions(query, limit=limit, project_filter=project_filter,
                                source_filter=source_filter, pwd=pwd, sort=sort)

    @app.tool()
    def grok_search(query: str = "", limit: int = 10, project_filter: str = "",
                    pwd: bool | None = None) -> str:
        """Convenience tool to search *only* Grok Build sessions.

        Equivalent to calling search() with source_filter="grok" hardcoded.

        Use this when working inside Grok Build and you only want to search
        your previous Grok sessions (not Claude or Codex history).

        Examples:
          - "Search my previous Grok sessions for the auth middleware discussion"
          - "Find what we talked about in earlier Grok sessions about the media skill"

        Returns ranked Grok sessions with snippets.
        Then use load(session_id) or pick() for more details.
        """
        return search_sessions(query, limit=limit, project_filter=project_filter,
                                source_filter="grok", pwd=pwd)

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

        msgs = _read_raw_messages(jsonl, source)
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
                        f"Try `clexo sync` to re-index.")
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
            REFRESH_PENDING.write_text(session_id, encoding="utf-8")
            # Deliberate load — the next session start should restore it even
            # from a different directory (see the cwd guard in the hook).
            REFRESH_EXPLICIT.write_text(session_id, encoding="utf-8")
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

        The snapshot is reachable later by its session-id fragment
        (`clexo load <fragment>`); the return value surfaces it. Use tag() if
        the user wants a friendly name — snapshots are no longer auto-tagged.

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
    def tags(short: bool = False, keywords: bool = False) -> str:
        """List all tags with their target sessions, opening/closing lines, and
        any saved summary. Use when the user asks "what tags do I have?",
        "list my tagged sessions", etc. Pass short=True for a compact listing of
        just tag name + date, newest first. Pass keywords=True to add per-session
        TF-IDF keywords (slower — it runs an FTS lookup per candidate word)."""
        return _format_tags(short=short, keywords=keywords)

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
        """Return clexo usage stats.

        Includes index size (sessions + messages), two separate token-savings
        counters — `tokens_compacted` (one-shot, credited at save time) and
        `tokens_saved` (per-turn, credited for each assistant message in a
        snapshot-loaded session) — and call counts for saves, loads, picks,
        searches, and tags.
        """
        try:
            return _format_stats()
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
        content = f.read_text(encoding="utf-8")
        if clear_pending:
            REFRESH_PENDING.unlink(missing_ok=True)
        return PREAMBLE + content

    if session_id:
        return _load_file(session_id, clear_pending=False)

    if REFRESH_PENDING.exists():
        sid = REFRESH_PENDING.read_text(encoding="utf-8").strip()
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
            first_line = f.read_text(encoding="utf-8").splitlines()[0]
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

    # The build annotates the true original turn count ("… of N exchanges …"), so
    # the elided count reflects the whole conversation rather than the snapshot's
    # already-trimmed tail (which the reader never sees). Falls back to the turns
    # present when the whole conversation fit in the snapshot.
    m = re.search(r"most recent \d+ of (\d+) exchanges", content)
    total_original = int(m.group(1)) if m else len(turns)

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

    elided = max(0, total_original - len(kept))
    if kept and elided > 0:
        body = (
            f"({elided} earlier exchange{'s' if elided != 1 else ''} not shown — "
            f"call pick() to retrieve)\n\n" + "\n\n".join(kept)
        )
    elif kept:
        body = "\n\n".join(kept)
    else:
        body = "(all exchanges elided — call pick() to retrieve)"

    compact = skeleton.replace(PLACEHOLDER, body) + footer

    return {
        "compact":     compact,
        "total_turns": total_original,
        "kept":        len(kept),
        "elided":      elided,
        "budget":      budget,
        "used":        used,
    }


def _same_session_dir(a: str, b: str) -> bool:
    """True if two recorded working directories refer to the same project dir.

    Used by the SessionStart auto-restore guard: a pending snapshot only
    auto-injects when the new session starts in the directory the saved
    session ran in. Normalises trailing slashes and `~`, but — like `--pwd`
    — compares the path otherwise verbatim (no symlink resolution), since the
    cwd is stored exactly as Claude recorded it at index time."""
    def _norm(p: str) -> str:
        return os.path.normpath(os.path.expanduser((p or "").strip()))
    return _norm(a) == _norm(b)


def _session_start_hook() -> None:
    """Output SessionStart hook JSON by delegating to _refresh_load.

    Only auto-restores if a prior session left REFRESH_PENDING behind.
    Without that marker, emits an empty hook payload — no banner, no
    injected context, no menu. The menu fallback in _refresh_load is
    intended for the interactive load() MCP tool, not the hook.
    """
    if not REFRESH_PENDING.exists():
        # No chain handoff in progress — drop any stale marker from a
        # prior restored-but-never-saved session.
        _CHAIN_LOADED.unlink(missing_ok=True)
        print(json.dumps({}))
        return

    # Peek at pending before _refresh_load clears it — needed for chain handoff
    loaded_sid = REFRESH_PENDING.read_text(encoding="utf-8").strip()

    # An explicit `clexo load` / load() drops this marker so this restore
    # bypasses the same-directory guard below. Consume it once: a later
    # automatic restore must re-earn the guard.
    explicit_sid = ""
    if REFRESH_EXPLICIT.exists():
        try:
            explicit_sid = REFRESH_EXPLICIT.read_text(encoding="utf-8").strip()
        except Exception:
            explicit_sid = ""
        REFRESH_EXPLICIT.unlink(missing_ok=True)
    is_explicit = bool(explicit_sid) and explicit_sid == loaded_sid
    cross_dir_note = None  # (saved_disp, here_disp) when an explicit load crosses dirs

    # New session id + cwd from hook stdin payload (Claude Code passes both).
    new_sid = ""
    new_cwd = ""
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                payload = json.loads(raw)
                new_sid = (payload.get("session_id") or "").strip()
                new_cwd = (payload.get("cwd") or "").strip()
    except Exception:
        new_sid = new_cwd = ""
    # Fall back to the hook process cwd (Claude runs hooks in the project dir).
    if not new_cwd:
        new_cwd = os.environ.get("PWD") or os.getcwd()

    # Directory safeguard: only auto-restore when the new session starts in the
    # same directory the saved session ran in. Without this, a pending snapshot
    # from project A would bleed into an unrelated project B the next time any
    # session starts. The marker is left in place so resuming from the right
    # directory still works. Fails open when either cwd is unknown, or when the
    # guard is disabled via "autoload_cwd_guard": false in config.json.
    if loaded_sid and new_cwd and _config_get("autoload_cwd_guard", True):
        try:
            row = get_db().execute(
                "SELECT cwd FROM sessions WHERE session_id = ?", [loaded_sid]
            ).fetchone()
            saved_cwd = (row[0] if row and row[0] else "") or ""
        except Exception:
            saved_cwd = ""
        if saved_cwd and not _same_session_dir(new_cwd, saved_cwd):
            home = os.path.expanduser("~")
            saved_disp = saved_cwd.replace(home, "~", 1)
            here_disp  = new_cwd.replace(home, "~", 1)
            if is_explicit:
                # User asked for this load — honor it here, but note that the
                # session is being continued in a different directory.
                cross_dir_note = (saved_disp, here_disp)
                if _debug_enabled():
                    with open(CLEXO_DIR / "hook.log", "a", encoding="utf-8") as _lf:
                        _lf.write(
                            f"\n--- {datetime.datetime.now().isoformat()} ---\n"
                            f"MODE: cwd-guard bypass (explicit load) "
                            f"(saved={saved_cwd!r} new={new_cwd!r})\n"
                        )
            else:
                # Different project — defer the restore, don't inject here. Drop
                # any chain-handoff pointer so a save() in this session starts
                # its own chain rather than extending the deferred one.
                _CHAIN_LOADED.unlink(missing_ok=True)
                msg = (
                    "↺  Clexo · Auto-restore deferred — saved session ran in a "
                    "different directory.\n"
                    f"     saved: {saved_disp}\n"
                    f"     here:  {here_disp}\n"
                    "     Start a session there, or run  clexo load <tag>  to "
                    "restore it here."
                )
                if _debug_enabled():
                    with open(CLEXO_DIR / "hook.log", "a", encoding="utf-8") as _lf:
                        _lf.write(
                            f"\n--- {datetime.datetime.now().isoformat()} ---\n"
                            f"MODE: cwd-guard skip "
                            f"(saved={saved_cwd!r} new={new_cwd!r})\n"
                        )
                print(json.dumps({"systemMessage": msg}))
                return

    content = _refresh_load()

    if not re.search(r'^## Session', content, re.MULTILINE) and "# Refresh Context" not in content:
        print(json.dumps({}))
        return

    # Write handoff file so save() in this process knows which chain to extend
    if loaded_sid:
        _CHAIN_LOADED.write_text(loaded_sid, encoding="utf-8")

    # Stamp the new session with the per-turn prefix delta for this load.
    # The "delta" is the size of the API prefix that the source session
    # would have replayed on its next turn — derived from the last assistant
    # message's `usage` block (Claude Code's own accounting), split into
    # fresh input_tokens vs cache (read + creation). Each subsequent
    # assistant turn in this new session credits both into stats.
    prefix_delta_input = 0
    prefix_delta_cache = 0
    snapshot_tok = 0
    if loaded_sid:
        chain_f   = CLEXO_DIR / f"chain-{loaded_sid}.md"
        refresh_f = CLEXO_DIR / f"refresh-{loaded_sid}.md"
        snapshot_path = chain_f if chain_f.exists() else (refresh_f if refresh_f.exists() else None)
        if snapshot_path:
            snapshot_tok = snapshot_path.stat().st_size // 4
    if new_sid and loaded_sid and new_sid != loaded_sid:
        try:
            conn = get_db()
            src_row = conn.execute(
                "SELECT COALESCE(source,'claude') FROM sessions WHERE session_id = ?",
                [loaded_sid],
            ).fetchone()
            src = (src_row[0] if src_row else "claude") or "claude"
            jsonl_path = _find_session_jsonl(loaded_sid, source=src)
            if jsonl_path and src == "claude":
                src_input, src_cache = _claude_source_prefix(jsonl_path)
                prefix_delta_input = max(0, src_input)
                prefix_delta_cache = max(0, src_cache)
                total = prefix_delta_input + prefix_delta_cache
                if total > 0:
                    conn.execute("""
                        INSERT INTO sessions(session_id,
                                             prefix_delta_tokens,
                                             prefix_delta_input_tokens,
                                             prefix_delta_cache_tokens)
                        VALUES(?, ?, ?, ?)
                        ON CONFLICT(session_id) DO UPDATE SET
                            prefix_delta_tokens       = excluded.prefix_delta_tokens,
                            prefix_delta_input_tokens = excluded.prefix_delta_input_tokens,
                            prefix_delta_cache_tokens = excluded.prefix_delta_cache_tokens
                    """, [new_sid, total, prefix_delta_input, prefix_delta_cache])
                    conn.commit()
        except Exception:
            pass
    prefix_delta = prefix_delta_input + prefix_delta_cache

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

    if prefix_delta > 0 and snapshot_tok > 0:
        stats = f"{_human_count(snapshot_tok)}/{_human_count(prefix_delta)} tokens/turn saved"
    else:
        stats = f"ctx {_human_bytes(len(compact))}/{_human_bytes(CAP)}"
    if total_n:
        stats += f" · {kept_n}/{total_n} turns"
        if elided > 0:
            stats += f" ({elided} elided)"
    else:
        stats += " · no turns"

    # Name the restored session by its short id so it's clear which one loaded
    # (and matches the id in the pick() footer / `clexo` commands).
    short = loaded_sid[:8] if loaded_sid else ""
    cdate = _compact_date(date)
    head = f"Session {short} restored" if short else "Session restored"
    line1 = f"  ↺  Clexo · {head} · {cdate}" if cdate else f"  ↺  Clexo · {head}"
    line2 = f"     {summary}" if summary else None
    line3 = f"     {stats}"
    # When an explicit load is continued in a different directory, note the
    # original so the cross-directory restore is visible rather than silent.
    line4 = f"     ↳ from {cross_dir_note[0]} (loaded here)" if cross_dir_note else None

    # Bound the box to the terminal width. A long summary, path, or date could
    # otherwise make a line wider than the display, and when the UI wraps it the
    # right border lands mid-line and the whole box looks shattered. Cap the
    # inner width and truncate any line that would exceed it. get_terminal_size
    # falls back to COLUMNS / 80 when stdout is a pipe (the hook case).
    body = [line1, line3] + ([line2] if line2 else []) + ([line4] if line4 else [])
    try:
        cols = shutil.get_terminal_size((80, 24)).columns or 80
    except Exception:
        cols = 80
    max_inner = max(30, cols - 4)
    inner_w = min(max(len(s) for s in body) + 2, max_inner)

    def _row(s: str) -> str:
        if len(s) > inner_w:
            s = s[: inner_w - 1] + "…"
        return "║" + s + " " * (inner_w - len(s)) + "║"

    rows = ["╔" + "═" * inner_w + "╗", _row(line1)]
    if line2:
        rows.append(_row(line2))
    rows.append(_row(line3))
    if line4:
        rows.append(_row(line4))
    rows.append("╚" + "═" * inner_w + "╝")
    banner = "\n".join(rows)

    # hook.log is debug-only — enable by setting "debug": true in ~/.clexo/config.json
    if _debug_enabled():
        log = CLEXO_DIR / "hook.log"
        with open(log, "a", encoding="utf-8") as _lf:
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


def _clexo_cmd() -> str:
    """How a Claude Code hook should invoke clexo. Prefer the absolute path to the
    installed console script (hooks may run with a minimal PATH); fall back to the
    `python -m clexo` module form so it still works from a bare checkout / venv."""
    exe = shutil.which("clexo")
    return exe if exe else f"{sys.executable} -m clexo"


def _install_hooks(settings_path: Path | None = None) -> str:
    """Idempotently add the SessionStart + SessionEnd hooks to Claude Code's
    settings.json. Backs up + writes ONLY if the JSON actually changes. Recognises
    our hooks (and stale older ones) by the session-start / sync markers plus
    'clexo' in the command, re-pointing the command in place when it has drifted."""
    settings_dir = (settings_path.parent if settings_path
                    else Path.home() / ".claude")
    settings_path = settings_path or (settings_dir / "settings.json")
    clexo = _clexo_cmd()
    start_cmd  = f"{clexo} session-start"
    end_cmd    = f"bash -c '{clexo} sync >> /tmp/clexo-sync.log 2>&1 &'"

    raw = ""
    if settings_path.exists():
        try:
            raw = settings_path.read_text(encoding="utf-8")
            settings = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            return f"Error: {settings_path} is not valid JSON ({e}). Aborted."
        if not isinstance(settings, dict):
            return f"Error: {settings_path} is not a JSON object. Aborted."
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return "Error: settings.json 'hooks' is not an object. Aborted."

    change_lines: list[str] = []

    def _has_clexo_cmd(entries: list, marker: str) -> bool:
        for he in entries if isinstance(entries, list) else []:
            for h in he.get("hooks", []) if isinstance(he, dict) else []:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                if marker in cmd and "clexo" in cmd.lower():
                    return True
        return False

    def _add(event: str, matcher: str, marker: str, command: str, label: str):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event} is not a list")
        # If our command is already present, migrate the matcher in place if needed.
        for e in entries:
            if not isinstance(e, dict):
                continue
            for h in e.get("hooks", []):
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command", "")
                if marker in cmd and "clexo" in cmd.lower():
                    changed = False
                    if e.get("matcher") != matcher:
                        old = e.get("matcher", "")
                        e["matcher"] = matcher
                        change_lines.append(
                            f"  ✎ {label}: matcher updated {old!r} → {matcher!r}")
                        changed = True
                    if h.get("command") != command:
                        h["command"] = command
                        change_lines.append(
                            f"  ✎ {label}: command re-pointed → {command!r}")
                        changed = True
                    if not changed:
                        change_lines.append(f"  ↻ {label}: already installed")
                    return
        # Not present — append to existing entry with the same matcher, or create one.
        target = next((e for e in entries
                       if isinstance(e, dict) and e.get("matcher") == matcher), None)
        if target is None:
            target = {"matcher": matcher, "hooks": []}
            entries.append(target)
        target.setdefault("hooks", []).append({"type": "command", "command": command})
        change_lines.append(f"  + {label}: added")

    try:
        # SessionStart matcher fires on startup AND clear (not resume/compact).
        # This is what enables `clexo load <sid>` then `claude` to restore.
        _add("SessionStart", "startup|clear", "session-start", start_cmd, "SessionStart hook")
        _add("SessionEnd",   "",              "sync",          end_cmd,   "SessionEnd hook")
    except ValueError as e:
        return f"Error: {e}. Aborted (no changes written)."

    new_content = json.dumps(settings, indent=2) + "\n"
    if settings_path.exists() and new_content == raw:
        return f"{settings_path}: already up to date — no changes.\n" + "\n".join(change_lines)

    out_lines: list[str] = []
    if settings_path.exists():
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = settings_path.with_suffix(f".json.bak.{ts}")
        backup.write_text(raw, encoding="utf-8")
        out_lines.append(f"Backed up: {backup}")
    else:
        settings_dir.mkdir(parents=True, exist_ok=True)

    settings_path.write_text(new_content, encoding="utf-8")
    out_lines.extend(change_lines)
    out_lines.append(f"Wrote:     {settings_path}")
    return "\n".join(out_lines)


def _human_count(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


def _compact_date(d: str) -> str:
    """Shorten a session header date for the restore banner: drop the year and
    timezone so it fits on one line. "2026-07-07 14:30 PDT" -> "Jul 7 14:30",
    "2026-05-06" -> "May 6". Returns the input unchanged if it doesn't parse."""
    d = (d or "").strip()
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}:\d{2}))?', d)
    if not m:
        return d
    y, mo, da, tm = m.groups()
    try:
        mon = datetime.date(int(y), int(mo), int(da)).strftime("%b")
    except Exception:
        return d
    return f"{mon} {int(da)} {tm}" if tm else f"{mon} {int(da)}"


def _format_stats() -> str:
    db = get_db()
    rows = {r[0]: r[1] for r in db.execute("SELECT key, value FROM stats").fetchall()}
    sessions = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    by_source = dict(db.execute(
        "SELECT COALESCE(source,'claude'), COUNT(*) FROM sessions GROUP BY source"
    ).fetchall())
    tag_count = db.execute("SELECT COUNT(*) FROM tags").fetchone()[0]

    messages  = rows.get("messages_indexed",   0)
    saves     = rows.get("refresh_saves",      0)
    loads     = rows.get("refresh_loads",      0)
    compacted = rows.get("tokens_compacted",   0)
    cache_saved = rows.get("cache_tokens_saved", 0)
    picks     = rows.get("pick_uses",          0)
    searches  = rows.get("search_calls",       0)

    src_bits = []
    if by_source.get("claude"):
        src_bits.append(f"claude {by_source['claude']:,}")
    if by_source.get("codex"):
        src_bits.append(f"codex {by_source['codex']:,}")
    src_suffix = f"  ({' · '.join(src_bits)})" if src_bits else ""

    breakdown_row = db.execute(
        "SELECT COUNT(*), SUM(prefix_delta_cache_tokens) "
        "FROM sessions WHERE prefix_delta_cache_tokens > 0"
    ).fetchone()
    n_loads = breakdown_row[0] or 0
    sum_cache = breakdown_row[1] or 0

    def _cache_suffix() -> str:
        if cache_saved <= 0 or n_loads <= 0 or sum_cache <= 0:
            return ""
        avg_delta = round(sum_cache / n_loads)
        avg_turns = round(cache_saved / sum_cache)
        return (
            f"  ({n_loads} loads × ~{avg_turns} turns × "
            f"~{_human_count(avg_delta)} tokens/turn)"
        )

    rule = "─" * 40
    lines = [
        "clexo gain — Global",
        rule,
        f"Sessions indexed:    {sessions:>6,}{src_suffix}",
        f"Messages indexed:    {messages:>6,}",
        f"Tokens compacted:    {_human_count(compacted):>6}  (one-shot at save time)",
        f"Cache tokens saved:  {_human_count(cache_saved):>6}{_cache_suffix()}",
        "",
        "Activity",
        f"  Saves     {saves:>5,}",
        f"  Loads     {loads:>5,}",
        f"  Picks     {picks:>5,}",
        f"  Searches  {searches:>5,}",
        f"  Tags      {tag_count:>5,}",
    ]
    return "\n".join(lines)


def _print_stats() -> None:
    try:
        print(_format_stats())
    except Exception as e:
        print(f"Stats unavailable: {e}", file=sys.stderr)
        sys.exit(1)


def _relative_when(ts: str) -> str:
    """Render an ISO timestamp as 'today HH:MM', 'yesterday', 'N days ago',
    or 'YYYY-MM-DD' for older entries. Returns '?' on parse failure."""
    if not ts:
        return "?"
    try:
        when = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return ts[:10] or "?"
    now = datetime.datetime.now(when.tzinfo) if when.tzinfo else datetime.datetime.now()
    delta = now - when
    if delta.days == 0 and when.date() == now.date():
        return f"today {when.strftime('%H:%M')}"
    if delta.days < 2 and (now.date() - when.date()).days == 1:
        return "yesterday"
    if 2 <= delta.days < 7:
        return f"{delta.days}d ago"
    return when.strftime("%Y-%m-%d")


def _pick_short_label(thread_name: str, first_user_msg: str, max_chars: int = 50) -> str:
    """Pick the most informative one-line label for a session row."""
    for cand in (thread_name, first_user_msg):
        s = (cand or "").strip().replace("\n", " ")
        if s and not _is_noise(s):
            return s[:max_chars] + ("…" if len(s) > max_chars else "")
    return "(no title)"


def _search_picker(query: str) -> None:
    """Interactive picker invoked by `clexo load <non-uuid-string>`.

    Searches sessions by FTS query and lets the user pick one to load as a
    snapshot (s, default) or resume fully (r). Exits after acting.
    """
    if not sys.stdout.isatty():
        print(f"No session matched '{query}' as a tag or UUID. "
              "Run `clexo search <query>` to find sessions.", file=sys.stderr)
        sys.exit(1)

    db = get_db()
    sync_all(db, throttle=True)

    fts_query = f'"{query}"' if '"' not in query else query
    try:
        rows = db.execute("""
            SELECT DISTINCT s.session_id, s.last_ts, s.project, s.cwd,
                   s.thread_name, s.first_user_msg, s.source,
                   (SELECT GROUP_CONCAT(tag, ',') FROM tags
                    WHERE session_id = s.session_id) AS tag_list
            FROM messages m
            JOIN sessions s ON s.session_id = m.session_id
            WHERE messages MATCH ?
            ORDER BY rank
            LIMIT 20
        """, [fts_query]).fetchall()
    except Exception:
        rows = []

    if not rows:
        print(f"No sessions found matching '{query}'. "
              "Try `clexo search <query>` for more options.", file=sys.stderr)
        sys.exit(1)

    print(f"Sessions matching '{query}' — pick one:")
    print()
    items = []
    for i, (sid, ts, project, cwd, thread_name, first_user_msg, source, tag_list) in enumerate(rows, 1):
        proj = _project_slug(project or "", cwd or "") or (project or "?")
        when = _relative_when(ts)
        tag  = (tag_list.split(",")[0] if tag_list else "")
        ident = tag if tag else f"{sid[:8]}…"
        label = _pick_short_label(thread_name, first_user_msg)
        src_marker = "" if source in (None, "", "claude") else f"[{source}]"
        print(f"  {i:>2}. {ident:<26} {when:<14} {proj:<14} {src_marker:<8} · {label}")
        items.append((sid, source or "claude"))
    print()
    print("Mode: [s] load snapshot (default)  [r] resume full session  [q] quit")
    try:
        raw = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not raw or raw == "q":
        return

    parts = raw.replace(",", " ").split()
    try:
        n = int(parts[0])
    except (ValueError, IndexError):
        print(f"Invalid selection: {raw!r}", file=sys.stderr)
        sys.exit(1)
    if not (1 <= n <= len(items)):
        print(f"Selection out of range (1–{len(items)}).", file=sys.stderr)
        sys.exit(1)
    mode = (parts[1] if len(parts) > 1 else "s")[0]
    if mode not in ("r", "s"):
        print(f"Unknown mode {mode!r} (expected r or s).", file=sys.stderr)
        sys.exit(1)

    sid, source = items[n - 1]
    if mode == "r":
        _exec_resume(sid, source)
    else:
        _exec_load(sid, source)


def _resume_picker() -> None:
    """Interactive picker invoked by bare `clexo resume`.

    Lists the 20 most-recently-active sessions and prompts the user to pick
    one. Selection format: `N` (default mode = full resume) or `N r` / `N s`
    where r = resume full session (execs the correct binary --resume <uuid>
    for claude/grok) and s = load snapshot. `q` or empty quits.
    """
    if not sys.stdout.isatty():
        print("clexo resume (no args) requires an interactive terminal.", file=sys.stderr)
        sys.exit(1)

    db = get_db()
    rows = db.execute("""
        SELECT s.session_id, s.last_ts, s.project, s.cwd,
               s.thread_name, s.first_user_msg, s.source,
               (SELECT GROUP_CONCAT(tag, ',') FROM tags
                WHERE session_id = s.session_id) AS tag_list
        FROM sessions s
        WHERE s.last_ts IS NOT NULL AND s.last_ts != ''
        ORDER BY s.last_ts DESC
        LIMIT 20
    """).fetchall()
    if not rows:
        print("No sessions in the index. Run `clexo sync` first.", file=sys.stderr)
        sys.exit(1)

    print("Recent sessions — pick one to resume:")
    print()
    items = []
    for i, (sid, ts, project, cwd, thread_name, first_user_msg, source, tag_list) in enumerate(rows, 1):
        proj = _project_slug(project or "", cwd or "") or (project or "?")
        when = _relative_when(ts)
        tag  = (tag_list.split(",")[0] if tag_list else "")
        ident = tag if tag else f"{sid[:8]}…"
        label = _pick_short_label(thread_name, first_user_msg)
        src_marker = "" if source in (None, "", "claude") else f"[{source}]"
        print(f"  {i:>2}. {ident:<26} {when:<14} {proj:<14} {src_marker:<8} · {label}")
        items.append((sid, source or "claude"))
    print()
    print("Mode: [r] resume full session  [s] load snapshot  [q] quit")
    try:
        raw = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not raw or raw == "q":
        return

    parts = raw.replace(",", " ").split()
    try:
        n = int(parts[0])
    except (ValueError, IndexError):
        print(f"Invalid selection: {raw!r}", file=sys.stderr)
        sys.exit(1)
    if not (1 <= n <= len(items)):
        print(f"Selection out of range (1–{len(items)}).", file=sys.stderr)
        sys.exit(1)
    mode = (parts[1] if len(parts) > 1 else "r")[0]
    if mode not in ("r", "s"):
        print(f"Unknown mode {mode!r} (expected r or s).", file=sys.stderr)
        sys.exit(1)

    sid, source = items[n - 1]
    if mode == "r":
        _exec_resume(sid, source)
    else:
        _exec_load(sid, source)


def _dispatch():
    if "--sync" in sys.argv:
        n = sync_all()
        print(f"Indexed {n} new messages.", flush=True)
    elif "--session-start" in sys.argv:
        _session_start_hook()
    elif "--stats" in sys.argv or (len(sys.argv) > 1 and sys.argv[1] in ("stats", "gain")):
        # Match the bare command words only in the command position — otherwise a
        # search query like `clexo search clexo gain` would route to stats.
        _print_stats()
    elif "--search" in sys.argv:
        idx = sys.argv.index("--search")
        query, opts = _parse_search_args(sys.argv[idx + 1:])
        # Empty query is valid — lists recent sessions (optionally filtered).
        print(search_sessions(query, limit=opts["limit"],
                              project_filter=opts["project_filter"],
                              source_filter=opts["source_filter"],
                              pwd=opts["pwd"], mode=opts["mode"], sort=opts["sort"]))
    elif "--save" in sys.argv:
        idx = sys.argv.index("--save")
        arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        # If a non-empty arg was given but doesn't look like a UUID or known tag,
        # error before falling through to "No session JSONL found".
        if arg:
            resolved = _resolve_session_or_tag(arg)
            if not _TAG_UUID_RE.match(resolved.lower()):
                print(f"'{arg}' is not a complete UUID (need 36 chars, "
                      f"8-4-4-4-12) and not a known tag. "
                      f"Run `clexo tags` to list, or check that the sid wasn't truncated.",
                      file=sys.stderr)
                sys.exit(1)
            arg = resolved
        print(refresh_save(arg))
    # ── Tag CLI ────────────────────────────────────────────────────────────
    elif "--tag" in sys.argv:
        rest = sys.argv[sys.argv.index("--tag") + 1:]
        if not rest:
            # Bare `clexo tag` — show current session's tag(s), or auto-generate one.
            sid, _src = _resolve_current_or_given_session()
            if not sid:
                print("No current session found. Pass a name (`clexo tag <name>`) "
                      "or run inside a Claude/Codex session.", file=sys.stderr)
                sys.exit(1)
            db = get_db()
            rows = db.execute(
                "SELECT tag, created_ts FROM tags WHERE session_id=? ORDER BY created_ts DESC",
                [sid],
            ).fetchall()
            primary = None
            if rows:
                for t, ts in rows:
                    ts_short = (ts or "")[:16].replace("T", " ")
                    print(f"{t}  · created {ts_short}")
                primary = rows[0][0]
            else:
                candidate = _apply_auto_tag(sid, db)
                if candidate:
                    print(f"Tagged '{candidate}'")
                    primary = candidate
                else:
                    print("Could not generate a tag automatically. "
                          "Run `clexo tag <name>`.", file=sys.stderr)
                    sys.exit(1)
            if primary:
                print(f"  clexo resume {primary}   (full session)")
                print(f"  clexo load   {primary}   (compacted snapshot)")
            sys.exit(0)
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
            print("Usage: clexo untag <name>", file=sys.stderr)
            sys.exit(1)
        out = _remove_tag(sys.argv[idx + 1])
        print(out)
        if out.startswith("No tag") or out.startswith("Error:"):
            sys.exit(2)
    elif "--tags" in sys.argv:
        print(_format_tags(short="--short" in sys.argv,
                           keywords="--keywords" in sys.argv))
    elif "--saved" in sys.argv:
        print(_format_saved(short="--short" in sys.argv))
    elif "--load" in sys.argv:
        idx = sys.argv.index("--load")
        arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if not arg:
            print("Usage: clexo load <tag-or-uuid>", file=sys.stderr)
            sys.exit(1)
        sid = _resolve_session_or_tag(arg)
        if not _TAG_UUID_RE.match(sid.lower()):
            # Not a UUID or known tag — treat as a search query and show picker.
            _search_picker(arg)
            sys.exit(0)
        # TTY → exec the right binary (fresh session; SessionStart hook injects
        # snapshot). Non-tty → print the command paste-ready. Reaped sessions
        # rebuild the snapshot from the archive inside _exec_load.
        db = get_db()
        row = db.execute("SELECT source FROM sessions WHERE session_id = ?", [sid]).fetchone()
        src = (row[0] if row and row[0] else "claude")
        _exec_load(sid, src)
    elif "--resume" in sys.argv:
        idx = sys.argv.index("--resume")
        if idx + 1 >= len(sys.argv):
            # No arg → interactive picker (resume full / load snapshot).
            _resume_picker()
            sys.exit(0)
        name = sys.argv[idx + 1]
        sid = _resolve_session_or_tag(name)
        if not _TAG_UUID_RE.match(sid.lower()):
            print(f"'{name}' is not a complete UUID (need 36 chars, "
                  f"8-4-4-4-12) and not a known tag. "
                  f"Run `clexo tags` to list.", file=sys.stderr)
            sys.exit(1)

        # Look up the source so we can use the correct binary (grok vs claude).
        db = get_db()
        row = db.execute(
            "SELECT source FROM sessions WHERE session_id = ?",
            [sid]
        ).fetchone()
        source = (row[0] if row and row[0] else "claude")

        # TTY → exec full-resume (degrades to snapshot-load if the live file was
        # reaped); else print the command paste-ready.
        _exec_resume(sid, source)
    elif "--install-hooks" in sys.argv:
        print(_install_hooks())
    elif "--show" in sys.argv:
        idx = sys.argv.index("--show")
        arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if not arg:
            print("Usage: clexo show <tag-or-uuid>", file=sys.stderr)
            sys.exit(1)
        sid = _resolve_session_or_tag(arg)
        if not _TAG_UUID_RE.match(sid.lower()):
            print(f"'{arg}' is not a complete UUID and not a known tag.",
                  file=sys.stderr)
            sys.exit(1)
        # Ensure a snapshot exists, then print it without touching REFRESH_PENDING
        # (so we don't accidentally schedule a restore on the next claude startup).
        err = _ensure_snapshot(sid)
        if err:
            print(err, file=sys.stderr)
            sys.exit(1)
        print(_refresh_load(sid))
    else:
        _run_server()


_USAGE = """\
Usage: clexo <command>

Commands:
  serve                Run the MCP server (Claude Code registers this as `clexo serve`)
  install              Wire clexo into Claude Code (MCP server + hooks); re-runnable
  stats / gain         Show usage stats
  sync                 Index new messages now
  search [query]       Search chat history (empty lists recent)
                       [--source_filter claude|codex|grok]
                       [--project_filter <name>|this] [--limit N]
                       [--pwd | --all]      Scope to / out of the current dir
                       [--oneline | --full] Compact table / verbose layout
  save [sid|tag]       Snapshot current (or given) session for restore
  saved [--short]      List saved snapshots (newest first), with their id
                       fragment for `clexo load`

Tags:
  tag <name> [--force] [sid]   Tag current (or given) session; --force to replace
  tags [--short] [--keywords]  List all tags with session info, newest first
  untag <name>                 Remove a tag
  load <name|sid>              Set pending snapshot + launch claude (fresh session;
                               the SessionStart hook injects the snapshot)
  resume [name|sid]            Resume the original session (claude --resume); with
                               no arg, an interactive picker over recent sessions
  show <name|sid>              Print the saved snapshot to stdout (inspect only)

Setup:
  install-hooks        Wire just the SessionStart + SessionEnd hooks
"""


def _wire_mcp() -> int:
    """Register clexo's MCP server with Claude Code as `clexo serve`. Idempotent;
    re-points a stale registration left by an older install (python3 .../server.py)."""
    claude = shutil.which("claude")
    if not claude:
        print("  ⚠ claude CLI not found on PATH — skipping MCP registration.")
        print("    After installing it, run:")
        print("      claude mcp add --scope user clexo clexo serve")
        return 0
    try:
        listing = subprocess.run(
            [claude, "mcp", "list"], capture_output=True, text=True, timeout=30
        ).stdout
    except Exception as e:                       # best-effort wiring
        print(f"  ⚠ could not run `claude mcp list` ({e}); skipping MCP step.")
        return 0
    entry = next((ln for ln in listing.splitlines()
                  if re.match(r'^\s*clexo[\s:]', ln)), None)
    if entry is None:
        subprocess.run([claude, "mcp", "add", "--scope", "user",
                        "clexo", "clexo", "serve"], check=False)
        print("  + MCP server registered: clexo serve")
    elif "serve" in entry and "server.py" not in entry:
        print("  ↻ MCP server already registered (clexo serve)")
    else:
        subprocess.run([claude, "mcp", "remove", "--scope", "user", "clexo"],
                       check=False)
        subprocess.run([claude, "mcp", "add", "--scope", "user",
                        "clexo", "clexo", "serve"], check=False)
        print("  ✎ MCP server re-pointed: clexo serve")
    return 0


def _heal_old_symlink() -> None:
    """Remove a stale ~/.local/bin/clexo symlink left by the old bash-wrapper
    install. Only ever unlinks a symlink (never a real file), and only when the
    active `clexo` console script resolves somewhere else."""
    link = Path.home() / ".local" / "bin" / "clexo"
    if not link.is_symlink():
        return
    installed = shutil.which("clexo")
    target = os.path.realpath(link)
    if installed and os.path.realpath(installed) == target:
        return                                   # this symlink IS the live clexo
    try:
        link.unlink()
        print(f"  ✗ removed stale wrapper symlink: {link} → {target or '(dangling)'}")
    except OSError as e:
        print(f"  ⚠ could not remove old symlink {link}: {e}")


def _cmd_install() -> int:
    """`clexo install` — wire the MCP server + hooks into Claude Code and clean up
    any older bash-wrapper install. Safe to re-run."""
    print("clexo install — wiring into Claude Code\n")
    rc = _wire_mcp()
    print(_install_hooks())
    _heal_old_symlink()
    print("\nDone. Try: clexo help")
    return rc


def _force_utf8_io():
    """Windows defaults stdout/stderr to the console codepage (cp1252), so the
    box-drawing, em-dash, ellipsis and middot chars clexo prints raise
    UnicodeEncodeError ('charmap' codec can't encode). Force UTF-8 on the
    standard streams. No-op where already UTF-8 or where the stream can't be
    reconfigured (e.g. a pytest capture object). errors='replace' keeps a
    legacy console that can't render a glyph from crashing."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv=None):
    """Console entry point. Maps friendly subcommands onto the internal flag
    dispatch, so `clexo save` etc. work without the old bash wrapper."""
    _force_utf8_io()
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""

    if cmd in ("", "help", "-h", "--help"):
        print(_USAGE)
        return
    if cmd == "serve":
        _run_server()
        return
    if cmd == "install":
        sys.exit(_cmd_install())

    # Friendly subcommand → the internal --flag that _dispatch() understands.
    SUBCMD = {
        "sync":          "--sync",
        "session-start": "--session-start",
        "search":        "--search",
        "tag":           "--tag",
        "tags":          "--tags",
        "saved":         "--saved",
        "untag":         "--untag",
        "load":          "--load",
        "resume":        "--resume",
        "show":          "--show",
        "install-hooks": "--install-hooks",
    }
    if cmd == "save":
        # Default the session id to $CLAUDE_CODE_SESSION_ID when none is given,
        # matching the old wrapper so `clexo save` snapshots the current session.
        rest = args[1:]
        if not rest:
            sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
            rest = [sid] if sid else []
        sys.argv = [sys.argv[0], "--save", *rest]
    elif cmd in SUBCMD:
        sys.argv = [sys.argv[0], SUBCMD[cmd], *args[1:]]
    elif cmd in ("stats", "gain"):
        sys.argv = [sys.argv[0], cmd]
    else:
        # Raw flags (`clexo --sync`) or anything else — pass through unchanged.
        sys.argv = [sys.argv[0], *args]

    _dispatch()


if __name__ == "__main__":
    main()
