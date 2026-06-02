"""Core tests for clexo server functions."""
import json
import sys
import tempfile
from pathlib import Path

import pytest

# Add parent to path so we can import server directly
sys.path.insert(0, str(Path(__file__).parent.parent))
import server


# ── DB / sync ─────────────────────────────────────────────────────────────────

def test_get_db_creates_tables():
    db = server.get_db()
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "messages" in tables
    assert "sessions" in tables
    assert "file_state" in tables


def test_sync_all_returns_int():
    db = server.get_db()
    result = server.sync_all(db)
    assert isinstance(result, int)
    assert result >= 0


def test_db_has_indexed_sessions():
    db = server.get_db()
    count = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert count > 0, "Expected at least one indexed session"


# ── Search ────────────────────────────────────────────────────────────────────

def test_search_returns_results():
    result = server._search("refresh")
    assert "Found" in result or "No results" in result


def test_search_finds_known_term():
    result = server._search("clexo")
    # We've discussed clexo in recent sessions — should find something
    assert "No results" not in result or "claudex" in result.lower()


def test_search_source_filter():
    result = server._search("refresh", source_filter="claude")
    assert "[codex]" not in result.lower() or "No results" in result


def test_search_bad_query_returns_error_not_exception():
    # Explicit FTS5 operator-only query — should return error string, not raise
    result = server._search("AND OR NOT")
    assert isinstance(result, str)

def test_search_special_chars_in_query():
    # Dots, slashes etc. should not crash (auto-quoted)
    result = server._search("CsrfTokenController.php")
    assert isinstance(result, str)
    assert not result.startswith("Search error")


def test_search_no_results():
    result = server._search("xyzzy_no_such_term_qqqq")
    assert "No results" in result


# ── Refresh load ──────────────────────────────────────────────────────────────

def _isolate_clexo_dir(tmp_path, monkeypatch):
    """Redirect every CLEXO_DIR-derived path constant to tmp_path so the
    refresh-pending file, chain-loaded marker, and DB don't bleed across
    runs. Module-level constants don't follow CLEXO_DIR after import, so
    they must be patched explicitly."""
    monkeypatch.setattr(server, "CLEXO_DIR",        tmp_path)
    monkeypatch.setattr(server, "DB_PATH",          tmp_path / "index.db")
    monkeypatch.setattr(server, "REFRESH_PENDING",  tmp_path / "refresh-pending")
    monkeypatch.setattr(server, "_CHAIN_LOADED",    tmp_path / "chain-loaded")
    monkeypatch.setattr(server, "ARCHIVE_DIR",      tmp_path / "archive")
    monkeypatch.setattr(server, "ARCHIVE_CACHE",    tmp_path / "cache")


def test_refresh_load_no_pending(tmp_path, monkeypatch):
    _isolate_clexo_dir(tmp_path, monkeypatch)
    result = server._refresh_load()
    assert "No saved sessions found" in result


def test_refresh_load_with_pending(tmp_path, monkeypatch):
    _isolate_clexo_dir(tmp_path, monkeypatch)
    sid = "test-session-1234"
    content = "# Refresh Context — 2026-05-06 — claude: test-session-1234\n\n## Summary\n- test entry\n"
    (tmp_path / f"refresh-{sid}.md").write_text(content)
    (tmp_path / "refresh-pending").write_text(sid)
    result = server._refresh_load()
    assert "# Refresh Context" in result
    assert "Previous session context" in result
    assert not (tmp_path / "refresh-pending").exists()


def test_refresh_load_clears_pending(tmp_path, monkeypatch):
    _isolate_clexo_dir(tmp_path, monkeypatch)
    sid = "abc123"
    (tmp_path / f"refresh-{sid}.md").write_text("# Refresh Context — 2026-05-06 — claude: abc123\n\n## Summary\n- something\n")
    (tmp_path / "refresh-pending").write_text(sid)
    server._refresh_load()
    assert not (tmp_path / "refresh-pending").exists()


def test_refresh_load_missing_file(tmp_path, monkeypatch):
    _isolate_clexo_dir(tmp_path, monkeypatch)
    (tmp_path / "refresh-pending").write_text("nonexistent-uuid")
    result = server._refresh_load()
    assert "not found" in result


# ── Session start hook ────────────────────────────────────────────────────────

def test_session_start_hook_no_pending(tmp_path, monkeypatch, capsys):
    _isolate_clexo_dir(tmp_path, monkeypatch)
    server._session_start_hook()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data == {}


def test_session_start_hook_no_pending_with_chain_files_on_disk(tmp_path, monkeypatch, capsys):
    _isolate_clexo_dir(tmp_path, monkeypatch)
    (tmp_path / "chain-aaaa1111-2222-3333-4444-555566667777.md").write_text(
        "## Session aaaa1111-2222-3333-4444-555566667777 | 2026-05-17 16:00 IST | claude | x\n\n"
        "### Summary\n- leftover session\n"
    )
    (tmp_path / "chain-bbbb1111-2222-3333-4444-555566667777.md").write_text(
        "## Session bbbb1111-2222-3333-4444-555566667777 | 2026-05-16 10:00 IST | claude | y\n\n"
        "### Summary\n- another leftover\n"
    )
    server._session_start_hook()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data == {}


def test_session_start_hook_no_pending_clears_stale_chain_loaded(tmp_path, monkeypatch, capsys):
    _isolate_clexo_dir(tmp_path, monkeypatch)
    (tmp_path / "chain-loaded").write_text("stale-uuid-from-prior-session")
    server._session_start_hook()
    assert not (tmp_path / "chain-loaded").exists()
    assert json.loads(capsys.readouterr().out) == {}


def test_session_start_hook_with_pending(tmp_path, monkeypatch, capsys):
    _isolate_clexo_dir(tmp_path, monkeypatch)
    sid = "test-uuid-hook"
    content = "# Refresh Context — 2026-05-06 — claude: test-uuid-hook\n\n## Summary\n- hook test session\n"
    (tmp_path / f"refresh-{sid}.md").write_text(content)
    (tmp_path / "refresh-pending").write_text(sid)
    server._session_start_hook()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "↺" in data["systemMessage"]
    assert "2026-05-06" in data["systemMessage"]
    assert "hook test session" in data["systemMessage"]
    assert "Previous session context" in data["hookSpecificOutput"]["additionalContext"]


# ── Tags ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Isolated sqlite DB so tag CRUD tests don't pollute ~/.clexo/index.db."""
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "test.db")
    return server.get_db()


def test_tags_table_created(isolated_db):
    tables = {r[0] for r in isolated_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "tags" in tables


def test_validate_tag_accepts_valid():
    assert server._validate_tag("my-auth-fix") is None
    assert server._validate_tag("foo_bar123") is None
    assert server._validate_tag("a") is None


def test_validate_tag_rejects_empty():
    assert server._validate_tag("") is not None
    assert server._validate_tag("   ") is not None


def test_validate_tag_rejects_uuid_shape():
    err = server._validate_tag("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert err and "uuid" in err.lower()


def test_validate_tag_rejects_bad_chars():
    assert server._validate_tag("has space") is not None
    assert server._validate_tag("foo!") is not None
    assert server._validate_tag("-foo") is not None
    assert server._validate_tag("foo/bar") is not None


def test_validate_tag_normalizes_case():
    # MixedCase is silently lowercased rather than rejected.
    assert server._validate_tag("MixedCase") is None


def test_create_and_resolve_tag(isolated_db):
    sid = "11111111-2222-3333-4444-555555555555"
    out = server._create_tag("alpha", sid)
    assert "Tagged" in out
    assert server._resolve_tag("alpha") == sid


def test_tag_normalizes_case(isolated_db):
    sid = "11111111-2222-3333-4444-555555555555"
    # Mixed case is silently lowercased on create and on lookup.
    server._create_tag("Alpha", sid)
    assert server._resolve_tag("alpha") == sid
    assert server._resolve_tag("ALPHA") == sid


def test_tag_collision_without_replace(isolated_db):
    sid1 = "11111111-2222-3333-4444-555555555555"
    sid2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    server._create_tag("beta", sid1)
    out = server._create_tag("beta", sid2)
    assert "already exists" in out
    assert server._resolve_tag("beta") == sid1  # unchanged


def test_tag_replace_overwrites(isolated_db):
    sid1 = "11111111-2222-3333-4444-555555555555"
    sid2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    server._create_tag("gamma", sid1)
    out = server._create_tag("gamma", sid2, replace=True)
    assert "Replaced" in out
    assert server._resolve_tag("gamma") == sid2


def test_tag_same_session_idempotent(isolated_db):
    sid = "11111111-2222-3333-4444-555555555555"
    server._create_tag("eta", sid)
    out = server._create_tag("eta", sid)
    assert "already points at this session" in out


def test_many_tags_per_session(isolated_db):
    sid = "11111111-2222-3333-4444-555555555555"
    server._create_tag("one", sid)
    server._create_tag("two", sid)
    assert server._resolve_tag("one") == sid
    assert server._resolve_tag("two") == sid


def test_untag(isolated_db):
    sid = "11111111-2222-3333-4444-555555555555"
    server._create_tag("delta", sid)
    out = server._remove_tag("delta")
    assert "Removed" in out
    assert server._resolve_tag("delta") is None


def test_untag_nonexistent(isolated_db):
    out = server._remove_tag("no-such-tag")
    assert "No tag" in out


def test_resolve_session_or_tag_uuid_passthrough(isolated_db):
    uuid = "11111111-2222-3333-4444-555555555555"
    assert server._resolve_session_or_tag(uuid) == uuid


def test_resolve_session_or_tag_lookup(isolated_db):
    sid = "11111111-2222-3333-4444-555555555555"
    server._create_tag("epsilon", sid)
    assert server._resolve_session_or_tag("epsilon") == sid


def test_resolve_session_or_tag_unknown_passthrough(isolated_db):
    assert server._resolve_session_or_tag("unknown-name") == "unknown-name"


def test_format_tags_empty(isolated_db):
    assert "No tags yet" in server._format_tags()


def test_format_tags_with_entries(isolated_db):
    sid = "11111111-2222-3333-4444-555555555555"
    server._create_tag("zeta", sid)
    out = server._format_tags()
    assert "@zeta" in out
    assert sid in out


def test_format_tags_short_empty(isolated_db):
    assert "No tags yet" in server._format_tags(short=True)


def test_format_tags_short_is_compact(isolated_db):
    sid = "11111111-2222-3333-4444-555555555555"
    server._create_tag("zeta", sid)
    out = server._format_tags(short=True)
    assert "@zeta" in out
    # Compact mode: tag name + date, no session id / opening / keywords.
    assert sid not in out
    assert "Opening:" not in out and "Keywords:" not in out


def test_format_tags_short_reverse_date_order(isolated_db):
    db = server.get_db()
    # Two tagged sessions with known last-activity dates; expect newest first.
    for sid, last in [
        ("11111111-1111-1111-1111-111111111111", "2026-01-10T00:00:00+05:30"),
        ("22222222-2222-2222-2222-222222222222", "2026-05-20T00:00:00+05:30"),
    ]:
        db.execute("INSERT INTO sessions(session_id, source, last_ts) VALUES(?,?,?)",
                   [sid, "claude", last])
    db.commit()
    server._create_tag("older", "11111111-1111-1111-1111-111111111111")
    server._create_tag("newer", "22222222-2222-2222-2222-222222222222")
    out = server._format_tags(short=True)
    assert out.index("@newer") < out.index("@older")
    assert "2026-05-20" in out and "2026-01-10" in out
    # Long mode shares the same newest-first ordering.
    long_out = server._format_tags()
    assert long_out.index("@newer") < long_out.index("@older")


def test_format_tags_keywords_opt_in(isolated_db):
    db = server.get_db()
    sid = "33333333-3333-3333-3333-333333333333"
    db.execute("INSERT INTO sessions(session_id, source) VALUES(?,?)", [sid, "claude"])
    db.execute(
        "INSERT INTO messages(session_id, role, content) VALUES(?,?,?)",
        [sid, "user", "kubernetes kubernetes ingress ingress controller controller"],
    )
    db.commit()
    server._create_tag("kube", sid)
    # Default long mode omits the (expensive) keyword line.
    assert "Keywords:" not in server._format_tags()
    # Opt-in restores it.
    assert "Keywords:" in server._format_tags(keywords=True)


def test_create_tag_rejects_bad_uuid(isolated_db):
    out = server._create_tag("badsid", "not-a-uuid")
    assert "doesn't look like a UUID" in out


# ── Hook installer ────────────────────────────────────────────────────────────

def test_install_hooks_creates_settings(tmp_path):
    settings = tmp_path / "settings.json"
    result = server._install_hooks(settings_path=settings)
    assert "added" in result
    assert settings.exists()
    data = json.loads(settings.read_text())
    start = data["hooks"]["SessionStart"]
    end   = data["hooks"]["SessionEnd"]
    assert any(h["matcher"] == "startup|clear" for h in start)
    assert any(h["matcher"] == ""               for h in end)
    assert any("--session-start" in h["command"]
               for entry in start for h in entry["hooks"])
    assert any("--sync" in h["command"]
               for entry in end   for h in entry["hooks"])


def test_install_hooks_is_idempotent(tmp_path):
    settings = tmp_path / "settings.json"
    server._install_hooks(settings_path=settings)
    snapshot = settings.read_text()
    backups_before = set(tmp_path.glob("settings.json.bak.*"))
    result2 = server._install_hooks(settings_path=settings)
    assert "already up to date" in result2
    assert "already installed" in result2
    # Body should be byte-identical (no duplicate entries appended)
    assert settings.read_text() == snapshot
    # No new backup should be created on a no-op call
    assert set(tmp_path.glob("settings.json.bak.*")) == backups_before


def test_install_hooks_preserves_existing_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "model": "sonnet",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "echo before-bash"}
            ]}]
        }
    }))
    server._install_hooks(settings_path=settings)
    data = json.loads(settings.read_text())
    assert data["model"] == "sonnet"
    assert data["permissions"]["allow"] == ["Bash(ls:*)"]
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo before-bash"
    assert "SessionStart" in data["hooks"]


def test_install_hooks_appends_to_matching_matcher(tmp_path):
    """If a SessionStart entry already exists with matcher='startup|clear' and
    a different command, append ours to that entry rather than create a duplicate."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"matcher": "startup|clear", "hooks": [
                {"type": "command", "command": "other-tool --start"}
            ]}]
        }
    }))
    server._install_hooks(settings_path=settings)
    data = json.loads(settings.read_text())
    start = data["hooks"]["SessionStart"]
    assert len(start) == 1, "should reuse the existing matcher entry"
    commands = [h["command"] for h in start[0]["hooks"]]
    assert any("other-tool" in c for c in commands)
    assert any("--session-start" in c for c in commands)


def test_install_hooks_creates_separate_entry_for_different_matcher(tmp_path):
    """If a SessionStart entry exists with matcher='clear' (someone else's),
    we add ours as a NEW entry with matcher='startup|clear' — don't share."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"matcher": "clear", "hooks": [
                {"type": "command", "command": "other-tool --start"}
            ]}]
        }
    }))
    server._install_hooks(settings_path=settings)
    data = json.loads(settings.read_text())
    start = data["hooks"]["SessionStart"]
    assert len(start) == 2, "different matcher → separate entry"
    matchers = sorted(e["matcher"] for e in start)
    assert matchers == ["clear", "startup|clear"]


def test_install_hooks_migrates_old_matcher(tmp_path):
    """Existing installs may have matcher='clear' from an older version.
    Re-running install should rewrite that matcher to 'startup|clear' in place."""
    settings = tmp_path / "settings.json"
    # Simulate an old install: matcher='clear' with our marker command.
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"matcher": "clear", "hooks": [
                {"type": "command", "command": "python3 /old/path/clexo/server.py --session-start"}
            ]}]
        }
    }))
    result = server._install_hooks(settings_path=settings)
    assert "matcher updated" in result
    data = json.loads(settings.read_text())
    start = data["hooks"]["SessionStart"]
    assert len(start) == 1, "should update in place, not duplicate"
    assert start[0]["matcher"] == "startup|clear"


def test_install_hooks_backs_up_existing(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"foo": "bar"}')
    result = server._install_hooks(settings_path=settings)
    assert "Backed up" in result
    backups = list(tmp_path.glob("settings.json.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == '{"foo": "bar"}'


def test_install_hooks_rejects_malformed_json(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{ not json")
    result = server._install_hooks(settings_path=settings)
    assert result.startswith("Error:")
    assert "not valid JSON" in result


# ── Keyword extraction & chain summary ────────────────────────────────────────

def test_session_keywords_basic(isolated_db):
    sid = "11111111-2222-3333-4444-555555555555"
    # Seed a small session: the topic word "encryption" appears multiple times,
    # stopwords + singletons should be filtered out.
    rows = [
        (sid, "p", "user",      "we need to add encryption to the storage layer", "t1"),
        (sid, "p", "assistant", "okay — encryption can use AES or chacha20",      "t2"),
        (sid, "p", "user",      "let's go with AES encryption for the storage",  "t3"),
        (sid, "p", "assistant", "AES it is",                                      "t4"),
    ]
    for r in rows:
        isolated_db.execute(
            "INSERT INTO messages(session_id,project,role,content,ts) VALUES(?,?,?,?,?)", r
        )
    isolated_db.commit()
    kws = server._session_keywords(isolated_db, sid, top=5)
    assert "encryption" in kws
    assert "storage" in kws
    # Stopwords / fillers excluded
    assert "the" not in kws
    assert "okay" not in kws
    # Singletons excluded (chacha20 appears only once)
    assert "chacha20" not in kws


def test_session_keywords_empty_for_missing_session(isolated_db):
    assert server._session_keywords(isolated_db, "no-such-session") == []


def test_session_keywords_idf_cache_is_reused(isolated_db):
    sid = "11111111-2222-3333-4444-555555555555"
    isolated_db.execute(
        "INSERT INTO messages(session_id,project,role,content,ts) VALUES(?,?,?,?,?)",
        (sid, "p", "user", "encryption encryption storage storage", "t1")
    )
    isolated_db.commit()
    cache: dict = {}
    server._session_keywords(isolated_db, sid, idf_cache=cache)
    assert "encryption" in cache and "storage" in cache


def test_chain_summary_extracts_section(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CLEXO_DIR", tmp_path)
    sid = "abc"
    content = (
        "## Session abc | 2026-05-17 | claude | x\n\n"
        "### Summary\n"
        "- Title: A useful session\n"
        "- Opening: hello\n\n"
        "### Key files\n"
        "(none)\n"
    )
    (tmp_path / f"chain-{sid}.md").write_text(content)
    out = server._chain_summary(sid)
    assert "Title: A useful session" in out
    assert "Key files" not in out


def test_chain_summary_picks_last_in_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CLEXO_DIR", tmp_path)
    sid = "abc"
    content = (
        "## Session old\n\n### Summary\n- old summary\n\n### Key files\n(none)\n\n"
        "## Session abc\n\n### Summary\n- newer summary\n\n### Key files\n(none)\n"
    )
    (tmp_path / f"chain-{sid}.md").write_text(content)
    out = server._chain_summary(sid)
    assert "newer summary" in out
    assert "old summary" not in out


def test_chain_summary_returns_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CLEXO_DIR", tmp_path)
    assert server._chain_summary("missing") == ""


def test_resolve_current_uses_claude_sessions_when_env_unset(tmp_path, monkeypatch):
    """When CLAUDE_CODE_SESSION_ID is unset, fall back to ~/.claude/sessions/*.json
    matching $PWD rather than mtime-across-everything."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    fake_home = tmp_path
    sessions_dir = fake_home / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    project_cwd = "/Users/test/some-project"
    monkeypatch.setenv("PWD", project_cwd)
    monkeypatch.setattr(server.Path, "home", classmethod(lambda cls: fake_home))

    target_sid = "abcd1234-aaaa-bbbb-cccc-dddddddddddd"
    (sessions_dir / "active.json").write_text(json.dumps({
        "sessionId": target_sid, "cwd": project_cwd,
    }))
    (sessions_dir / "other.json").write_text(json.dumps({
        "sessionId": "ffff0000-aaaa-bbbb-cccc-dddddddddddd",
        "cwd": "/Users/test/somewhere-else",
    }))

    sid, source = server._resolve_current_or_given_session()
    assert sid == target_sid
    assert source == "claude"


def test_resolve_current_cwd_fallback_when_env_jsonl_missing(tmp_path, monkeypatch):
    """If env CLAUDE_CODE_SESSION_ID is set but its JSONL doesn't exist (rare —
    typically only in tests / stale env), fall through to cwd-match."""
    fake_home = tmp_path
    sessions_dir = fake_home / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    project_cwd = "/Users/test/proj"
    monkeypatch.setenv("PWD", project_cwd)
    monkeypatch.setattr(server.Path, "home", classmethod(lambda cls: fake_home))

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID",
                       "ffff0000-aaaa-bbbb-cccc-dddddddddddd")  # no JSONL
    target_sid = "abcd1234-aaaa-bbbb-cccc-dddddddddddd"
    (sessions_dir / "x.json").write_text(json.dumps({
        "sessionId": target_sid, "cwd": project_cwd,
    }))

    sid, _ = server._resolve_current_or_given_session()
    assert sid == target_sid


# ── Durable transcript archive ────────────────────────────────────────────────

def test_strip_archive_line_claude_keeps_commands_drops_output():
    # assistant turn: text + tool_use (command) + thinking → keep text + tool_use
    asst = {
        "type": "assistant", "timestamp": "t1",
        "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "secret reasoning"},
            {"type": "text", "text": "running it"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
        ]},
    }
    kept = server._strip_archive_line(asst, "claude")
    types = [b["type"] for b in kept["message"]["content"]]
    assert types == ["text", "tool_use"]          # thinking dropped
    assert kept["message"]["content"][1]["input"]["command"] == "ls -la"

    # a pure tool_result carrier (user message) → dropped entirely
    tr = {"type": "user", "toolUseResult": {"stdout": "..."},
          "message": {"role": "user", "content": [
              {"type": "tool_result", "content": "huge output"}]}}
    assert server._strip_archive_line(tr, "claude") is None

    # non user/assistant line → dropped
    assert server._strip_archive_line({"type": "ai-title"}, "claude") is None


def test_strip_archive_line_codex():
    user = {"type": "event_msg", "timestamp": "t",
            "payload": {"type": "user_message", "message": "hi"}}
    assert server._strip_archive_line(user, "codex")["payload"]["message"] == "hi"
    asst = {"type": "response_item", "timestamp": "t",
            "payload": {"role": "assistant",
                        "content": [{"type": "output_text", "text": "yo"}]}}
    assert server._strip_archive_line(asst, "codex")["payload"]["role"] == "assistant"
    # function output (tool result) → dropped
    fout = {"type": "response_item", "payload": {"type": "function_call_output"}}
    assert server._strip_archive_line(fout, "codex") is None


def _write_fake_claude_session(tmp_path, monkeypatch, sid):
    proj = tmp_path / "claude_projects" / "-Users-x-proj"
    proj.mkdir(parents=True)
    monkeypatch.setattr(server, "CLAUDE_PROJECTS", tmp_path / "claude_projects")
    lines = [
        {"type": "user", "timestamp": "t0",
         "message": {"role": "user", "content": "do the thing"}},
        {"type": "assistant", "timestamp": "t1",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Bash", "input": {"command": "make build"}}]}},
        {"type": "user", "timestamp": "t2",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "content": "BIG TOOL OUTPUT"}]}},
    ]
    f = proj / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps(o) for o in lines) + "\n")
    return f


def test_archive_roundtrip_and_gate_fallback(tmp_path, monkeypatch):
    _isolate_clexo_dir(tmp_path, monkeypatch)
    sid = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    live = _write_fake_claude_session(tmp_path, monkeypatch, sid)

    # gate returns the live file while it exists
    assert server._find_session_jsonl(sid, "claude") == live

    # archive it, then simulate Claude's reap
    server._write_archive(sid, "claude", live)
    assert server._archive_path(sid, "claude").exists()
    live.unlink()
    assert server._live_session_jsonl(sid, "claude") is None

    # gate now falls back to the materialized archive
    gated = server._find_session_jsonl(sid, "claude")
    assert gated is not None and "cache" in str(gated)

    # transcript survives: command kept, tool output gone
    msgs = server._read_raw_messages(gated, "claude")
    rendered = "\n".join(server._extract_content(m["content"]) for m in msgs)
    assert "make build" in rendered           # tool command preserved
    assert "BIG TOOL OUTPUT" not in rendered   # tool output dropped


def test_exec_resume_degrades_to_load_when_reaped(tmp_path, monkeypatch):
    _isolate_clexo_dir(tmp_path, monkeypatch)
    sid = "aaaabbbb-cccc-dddd-eeee-ffff00002222"
    live = _write_fake_claude_session(tmp_path, monkeypatch, sid)
    server._write_archive(sid, "claude", live)
    live.unlink()  # reaped

    calls = {}
    monkeypatch.setattr(server, "_exec_load",
                        lambda s, src="claude", allow_print=True: calls.update(sid=s, src=src))
    # would otherwise os.execvp; ensure it never reaches a true resume
    monkeypatch.setattr(server.os, "execvp",
                        lambda *a: (_ for _ in ()).throw(AssertionError("should not exec --resume")))
    server._exec_resume(sid, "claude")
    assert calls == {"sid": sid, "src": "claude"}   # degraded to load


def test_archive_retention_default_is_forever(tmp_path, monkeypatch):
    _isolate_clexo_dir(tmp_path, monkeypatch)
    db = server.get_db()
    assert server._archive_retention_days() == 0          # no config → forever
    sid = "11110000-0000-0000-0000-000000000001"
    db.execute("INSERT INTO sessions(session_id, source, last_ts) VALUES(?,?,?)",
               [sid, "claude", "2020-01-01T00:00:00+00:00"])   # ancient
    db.commit()
    arc = server._archive_path(sid, "claude")
    arc.parent.mkdir(parents=True, exist_ok=True)
    arc.write_bytes(b"x")
    assert server._prune_archives(db) == 0
    assert arc.exists()                                   # kept forever


def test_archive_retention_prunes_old_keeps_recent_and_tagged(tmp_path, monkeypatch):
    import datetime as _dt
    _isolate_clexo_dir(tmp_path, monkeypatch)
    (tmp_path / "config.json").write_text(json.dumps({"archive_retention_days": 180}))
    db = server.get_db()
    old    = (_dt.datetime.now().astimezone() - _dt.timedelta(days=400)).isoformat()
    recent = (_dt.datetime.now().astimezone() - _dt.timedelta(days=10)).isoformat()
    rows = [
        ("aaaa0000-0000-0000-0000-000000000001", old),     # old, untagged → prune
        ("bbbb0000-0000-0000-0000-000000000002", recent),  # recent → keep
        ("cccc0000-0000-0000-0000-000000000003", old),     # old but tagged → keep
    ]
    for sid, ts in rows:
        db.execute("INSERT INTO sessions(session_id, source, last_ts) VALUES(?,?,?)",
                   [sid, "claude", ts])
        a = server._archive_path(sid, "claude")
        a.parent.mkdir(parents=True, exist_ok=True)
        a.write_bytes(b"x")
    db.execute("INSERT INTO tags(tag, session_id, created_ts) VALUES('keepme', ?, ?)",
               [rows[2][0], "t"])
    db.commit()
    assert server._prune_archives(db) == 1
    assert not server._archive_path(rows[0][0], "claude").exists()   # old pruned
    assert server._archive_path(rows[1][0], "claude").exists()       # recent kept
    assert server._archive_path(rows[2][0], "claude").exists()       # tagged kept
