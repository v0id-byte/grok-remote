"""Sandbox enforcement.

This is the Bridge's only non-bypassable boundary: `grok agent stdio` rejects
--allow/--deny, and grok never delegates permission to the ACP client. grok
itself is fail-OPEN -- it logs a warning and runs unconfined if the sandbox
cannot be applied -- so the Bridge must be fail-CLOSED.

The event shape asserted here is the real one grok wrote during the Phase 0
spike, including the `enforced` flag that distinguishes a live Seatbelt policy
from the fail-open path.
"""
from __future__ import annotations

import importlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture()
def sb(monkeypatch):
    home = Path(tempfile.mkdtemp())
    monkeypatch.setenv("GROK_BRIDGE_DIR", tempfile.mkdtemp())
    import grok_bridge.config as config
    importlib.reload(config)
    monkeypatch.setattr(config, "SANDBOX_PATH", home / "sandbox.toml")
    monkeypatch.setattr(config, "SANDBOX_EVENTS_PATH", home / "sandbox-events.jsonl")
    monkeypatch.setattr(config, "SANDBOX_REQUIRED", True)
    import grok_bridge.sandbox as sandbox
    importlib.reload(sandbox)
    monkeypatch.setattr(sandbox, "config", config)
    return sandbox, config


def _write_event(config, *, enforced=True, profile=None, event_type="ProfileApplied"):
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event_type": event_type,
        "profile": profile or config.SANDBOX_PROFILE_NAME,
        "workspace": "/private/tmp",
        "platform": "macos/seatbelt",
        "enforced": enforced,
        "deny_paths": ["/Users/v0id/.ssh"],
    }
    with config.SANDBOX_EVENTS_PATH.open("a") as f:
        f.write(json.dumps(event) + "\n")


def test_profile_is_created_when_missing(sb):
    sandbox, config = sb
    path = sandbox.ensure_profile()
    body = path.read_text()
    assert f"[profiles.{config.SANDBOX_PROFILE_NAME}]" in body
    assert 'extends = "workspace"' in body
    assert "/.ssh" in body


def test_existing_profile_is_never_rewritten(sb):
    """The file belongs to the user; a hand-tuned profile must survive."""
    sandbox, config = sb
    config.SANDBOX_PATH.write_text(
        f'[profiles.{config.SANDBOX_PROFILE_NAME}]\nextends = "strict"\n')
    sandbox.ensure_profile()
    assert 'extends = "strict"' in config.SANDBOX_PATH.read_text()


def test_other_profiles_are_preserved(sb):
    sandbox, config = sb
    config.SANDBOX_PATH.write_text('[profiles.mine]\nextends = "read-only"\n')
    sandbox.ensure_profile()
    body = config.SANDBOX_PATH.read_text()
    assert "[profiles.mine]" in body
    assert f"[profiles.{config.SANDBOX_PROFILE_NAME}]" in body


def test_enforced_profile_is_accepted(sb):
    sandbox, config = sb
    since = time.time() - 1
    _write_event(config, enforced=True)
    assert sandbox.verify_enforced(since=since, timeout=1)["enforced"] is True


def test_unenforced_profile_is_refused(sb):
    """grok's fail-open path: profile reported, but not actually applied."""
    sandbox, config = sb
    since = time.time() - 1
    _write_event(config, enforced=False)
    with pytest.raises(sandbox.SandboxNotEnforced, match="enforced=False"):
        sandbox.verify_enforced(since=since, timeout=1)


def test_missing_event_is_refused(sb):
    sandbox, _ = sb
    with pytest.raises(sandbox.SandboxNotEnforced, match="no ProfileApplied event"):
        sandbox.verify_enforced(since=time.time(), timeout=1)


def test_a_different_profile_does_not_count(sb):
    sandbox, config = sb
    since = time.time() - 1
    _write_event(config, enforced=True, profile="workspace")
    with pytest.raises(sandbox.SandboxNotEnforced):
        sandbox.verify_enforced(since=since, timeout=1)


def test_a_stale_event_does_not_count(sb):
    """An event from an earlier run must not vouch for the agent just spawned."""
    sandbox, config = sb
    _write_event(config, enforced=True)
    with pytest.raises(sandbox.SandboxNotEnforced):
        sandbox.verify_enforced(since=time.time() + 5, timeout=1)


def test_override_is_available_for_local_development(sb, monkeypatch):
    sandbox, config = sb
    monkeypatch.setattr(config, "SANDBOX_REQUIRED", False)
    assert sandbox.verify_enforced(since=time.time(), timeout=0.5) == {}
