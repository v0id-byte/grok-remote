"""Every test gets its own Store on a throwaway database.

`grok_bridge.config` computes DB_PATH at import time, so the env var has to be
set before the modules are imported and they have to be reloaded per test --
otherwise tests share the developer's real bridge.db.
"""
from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def bridge(monkeypatch):
    monkeypatch.setenv("GROK_BRIDGE_DIR", tempfile.mkdtemp())
    import grok_bridge.config as config
    importlib.reload(config)
    # Test sessions use pytest's temporary directories. Production callers
    # still go through validate_cwd; this only makes the fixture's synthetic
    # workspaces legal under the same allowlist.
    config.ALLOWED_ROOTS.append(Path(tempfile.gettempdir()).resolve())
    import grok_bridge.db as db
    importlib.reload(db)
    import grok_bridge.app as app
    importlib.reload(app)
    return app


@pytest.fixture()
def client(bridge):
    from fastapi.testclient import TestClient
    return TestClient(bridge.app)
