"""Every test gets its own Store on a throwaway database.

`grok_bridge.config` computes DB_PATH at import time, so the env var has to be
set before the modules are imported and they have to be reloaded per test --
otherwise tests share the developer's real bridge.db.
"""
from __future__ import annotations

import importlib
import tempfile

import pytest


@pytest.fixture()
def bridge(monkeypatch):
    monkeypatch.setenv("GROK_BRIDGE_DIR", tempfile.mkdtemp())
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
