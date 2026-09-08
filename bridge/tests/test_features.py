"""Phase 3 bridge features: models, workspace picker, history, commands."""
from __future__ import annotations

import importlib
import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def bridge(monkeypatch):
    monkeypatch.setenv("GROK_BRIDGE_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("GROK_BRIDGE_ALLOW_UNSANDBOXED", "1")
    import grok_bridge.config as config
    importlib.reload(config)
    import grok_bridge.db as db
    importlib.reload(db)
    import grok_bridge.app as app
    importlib.reload(app)
    return app


@pytest.fixture()
def client(bridge):
    from fastapi.testclient import TestClient
    return TestClient(bridge.app)


@pytest.fixture()
def auth(client, bridge):
    token = bridge.store.create_pairing_token()
    creds = client.post("/v1/pair", json={"pairing_token": token}).json()
    return {"Authorization": f"Bearer {creds['device_id']}.{creds['secret']}"}


# ------------------------------------------------------------------ models

def test_models_come_from_files_not_a_screen_scrape(client, auth):
    body = client.get("/v1/models", headers=auth).json()
    ids = {m["id"] for m in body["models"]}
    # Both sources must be present: xAI models live in models_cache.json, the
    # local ones only in config.toml.
    assert "grok-4.6" in ids
    assert any(m["source"] == "config" for m in body["models"])
    by_id = {m["id"]: m for m in body["models"]}
    assert by_id["grok-4.6"]["contextWindow"] == 500000
    # Per-model efforts: grok-4.6 offers xhigh, grok-4.5 does not.
    assert "xhigh" in by_id["grok-4.6"]["reasoningEfforts"]
    assert "xhigh" not in by_id["grok-4.5"]["reasoningEfforts"]


# --------------------------------------------------------------- workspace

def test_fs_list_flags_repos_and_refuses_paths_outside_the_roots(client, auth, tmp_path):
    repo = Path.home() / tempfile.mkdtemp(prefix="fs-test-", dir=Path.home())
    (repo / "proj").mkdir()
    (repo / "proj" / ".git").mkdir()
    try:
        body = client.get("/v1/fs/list", params={"path": str(repo)}, headers=auth).json()
        assert [e["name"] for e in body["entries"]] == ["proj"]
        assert body["entries"][0]["isRepo"] is True
        assert client.get("/v1/fs/list", params={"path": "/etc"},
                          headers=auth).status_code == 403
        assert client.get("/v1/fs/list", params={"path": str(repo / "nope")},
                          headers=auth).status_code == 404
    finally:
        import shutil
        shutil.rmtree(repo, ignore_errors=True)


def test_worktree_branch_is_read_through_the_gitdir_pointer(tmp_path, monkeypatch):
    """A worktree's .git is a FILE containing `gitdir: ...`; reading .git/HEAD
    directly fails on it, and this machine has four such repos."""
    from grok_bridge import workspace
    real = tmp_path / "real" / ".git"
    (real / "worktrees" / "wt").mkdir(parents=True)
    (real / "worktrees" / "wt" / "HEAD").write_text("ref: refs/heads/feature/x\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {real / 'worktrees' / 'wt'}\n")

    assert workspace.is_repo(wt)
    assert workspace.head_branch(wt) == "feature/x"


def test_detached_head_is_reported_as_a_short_sha(tmp_path):
    from grok_bridge import workspace
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("0f429d0f96ee70d2a6c259c4ecc6c6e18e0d23ff\n")
    assert workspace.head_branch(repo) == "0f429d0f96ee"


# ----------------------------------------------------------------- history

def test_history_filters_grok_internal_entries(tmp_path, monkeypatch):
    """grok's transcript contains system prompts, reasoning traces, tool results
    and synthetic compaction summaries. Replaying those would show the user
    'messages' nobody wrote."""
    from grok_bridge import grok_disk
    session_dir = tmp_path / "enc" / "sess"
    session_dir.mkdir(parents=True)
    (session_dir / "chat_history.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"type": "system", "content": "you are grok"},
        {"type": "user", "content": [{"type": "text", "text": "hello"}]},
        {"type": "reasoning", "summary": "thinking"},
        {"type": "user", "content": [{"type": "text", "text": "summary"}],
         "synthetic_reason": "compaction_meta"},
        {"type": "assistant", "content": "hi there", "model_id": "grok-4.5"},
        {"type": "tool_result", "content": "output", "tool_call_id": "x"},
        {"type": "assistant", "content": "",
         "tool_calls": [{"id": "1", "name": "read_file"}]},
    ]) + "\n{ this line is being written right now")

    monkeypatch.setattr(grok_disk, "session_dir", lambda cwd, sid: session_dir)
    page = grok_disk.read_history("/x", "sess")
    assert [m["role"] for m in page["messages"]] == ["user", "assistant", "assistant"]
    assert page["messages"][0]["text"] == "hello"
    assert page["messages"][1]["model"] == "grok-4.5"
    assert page["messages"][2]["toolCalls"][0]["name"] == "read_file"
    assert page["total"] == 3


def test_history_unwraps_user_query_and_drops_env_preamble(tmp_path, monkeypatch):
    """grok stores the first turn's <user_info>/<git_status>/<rules> block as its
    own user record and wraps every real prompt in <user_query>. Neither the
    preamble nor the wrapper tags should reach the phone."""
    from grok_bridge import grok_disk
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    (session_dir / "chat_history.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "content":
            "<user_info>\nOS: macos\n</user_info>\n<git_status>\nclean\n</git_status>"},
        {"type": "user", "content":
            "<user_query>\nRead notes.txt and tell me the magic word.\n</user_query>"},
        {"type": "assistant", "content": "The magic word is ELDERBERRY_9042."},
        {"type": "user", "content":
            "<image_files>\nx\n</image_files>\n<user_query>\nwhat colour?\n</user_query>"},
        {"type": "user", "content": "plain follow-up with no wrapper"},
    ]) + "\n")
    monkeypatch.setattr(grok_disk, "session_dir", lambda cwd, sid: session_dir)

    page = grok_disk.read_history("/x", "s")
    assert [m["role"] for m in page["messages"]] == \
        ["user", "assistant", "user", "user"]
    assert page["messages"][0]["text"] == "Read notes.txt and tell me the magic word."
    assert page["messages"][2]["text"] == "what colour?"
    assert page["messages"][3]["text"] == "plain follow-up with no wrapper"
    assert "<user_info>" not in json.dumps(page)


def test_history_pagination_is_stable(tmp_path, monkeypatch):
    from grok_bridge import grok_disk
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    (session_dir / "chat_history.jsonl").write_text("\n".join(
        json.dumps({"type": "user", "content": [{"type": "text", "text": str(i)}]})
        for i in range(10)))
    monkeypatch.setattr(grok_disk, "session_dir", lambda cwd, sid: session_dir)

    last = grok_disk.read_history("/x", "s", limit=3)
    assert [m["text"] for m in last["messages"]] == ["7", "8", "9"]
    assert last["hasMore"] and last["nextBefore"] == 7
    earlier = grok_disk.read_history("/x", "s", limit=3, before=last["nextBefore"])
    assert [m["text"] for m in earlier["messages"]] == ["4", "5", "6"]


def test_messages_endpoint_404s_on_an_unknown_session(client, auth):
    assert client.get("/v1/sessions/nope/messages", headers=auth).status_code == 404


# ---------------------------------------------------------------- commands

def test_command_palette_labels_where_each_command_comes_from(client, auth, bridge,
                                                              tmp_path):
    sid = bridge.store.create_session(str(tmp_path), "t")
    body = client.get(f"/v1/sessions/{sid}/commands", headers=auth).json()
    by_name = {c["name"]: c for c in body["commands"]}
    # Not on ACP at all -- implemented here, and labelled as such.
    assert by_name["model"]["source"] == "bridge"
    assert by_name["status"]["kind"] == "shell"
    assert by_name["model"]["argType"] == "enum"
    assert any(o["value"] == "grok-4.6" for o in by_name["model"]["options"])


def test_aliases_resolve(bridge):
    from grok_bridge import commands
    assert commands.resolve("m") == ("model", "bridge")
    assert commands.resolve("new") == ("clear", "bridge")
    assert commands.resolve("compact") == ("compact", "acp")


def test_rename_updates_the_session_and_announces_it(client, auth, bridge, tmp_path):
    sid = bridge.store.create_session(str(tmp_path), "old")
    r = client.post(f"/v1/sessions/{sid}/command",
                    json={"name": "rename", "args": "new title"}, headers=auth)
    assert r.status_code == 200
    assert bridge.store.get_session(sid)["title"] == "new title"
    assert any(e["type"] == "session.titled"
               for e in bridge.store.events_after(sid, 0))


def test_acp_commands_are_not_runnable_as_command_calls(client, auth, bridge, tmp_path):
    """They stream output, so they belong on the prompt path."""
    sid = bridge.store.create_session(str(tmp_path), "t")
    r = client.post(f"/v1/sessions/{sid}/command",
                    json={"name": "compact"}, headers=auth)
    assert r.status_code == 400
    assert "send it as a prompt" in r.json()["detail"]


def test_unknown_command_is_rejected_not_sent_to_the_model(client, auth, bridge,
                                                           tmp_path):
    sid = bridge.store.create_session(str(tmp_path), "t")
    r = client.post(f"/v1/sessions/{sid}/command", json={"name": "asdf"}, headers=auth)
    assert r.status_code == 400


# ------------------------------------------------------- session lifecycle

def test_patch_and_delete_a_session(client, auth, bridge, tmp_path):
    sid = bridge.store.create_session(str(tmp_path), "t")
    assert client.patch(f"/v1/sessions/{sid}",
                        json={"title": "renamed", "reasoningEffort": "high"},
                        headers=auth).status_code == 200
    row = bridge.store.get_session(sid)
    assert row["title"] == "renamed" and row["reasoning_effort"] == "high"

    bridge.agents.emit(sid, {"type": "message.delta", "data": "x"})
    assert client.delete(f"/v1/sessions/{sid}", headers=auth).json()["deleted"] is True
    assert bridge.store.get_session(sid) is None
    assert bridge.store.latest_seq(sid) == 0
