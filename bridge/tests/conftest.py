"""Every test gets its own Store on a throwaway database.

`grok_bridge.config` computes DB_PATH at import time, so the env var has to be
set before the modules are imported and they have to be reloaded per test --
otherwise tests share the developer's real bridge.db.
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_grok_install(monkeypatch, tmp_path):
    """Give every test the small on-disk Grok catalogue it expects.

    Production reads model metadata from the user's ~/.grok directory. CI has
    no Grok installation, so tests must not depend on whichever developer
    happens to run them. The ACP tests replace GROK_BIN with their fake agent
    after this fixture has prepared the catalogue.
    """
    grok_home = tmp_path / "grok"
    grok_bin = grok_home / "bin" / "grok"
    grok_bin.parent.mkdir(parents=True)
    grok_bin.write_text("#!/bin/sh\nexit 0\n")
    grok_bin.chmod(0o755)

    cache = {
        "models": {
            "grok-4.6": {"info": {
                "name": "Grok 4.6",
                "description": "CI fixture model",
                "context_window": 500000,
                "reasoning_efforts": [
                    {"id": "low"}, {"id": "high"}, {"id": "xhigh"},
                ],
                "supports_reasoning_effort": True,
            }},
            "grok-4.5": {"info": {
                "name": "Grok 4.5",
                "description": "CI fixture model",
                "context_window": 256000,
                "reasoning_efforts": [{"id": "low"}, {"id": "high"}],
                "supports_reasoning_effort": True,
            }},
        }
    }
    (grok_home / "models_cache.json").write_text(json.dumps(cache))
    (grok_home / "config.toml").write_text(
        "[models]\ndefault = \"grok-4.6\"\n\n"
        "[model.local-fixture]\nname = \"Local fixture\"\n"
        "context_window = 128000\n"
    )
    monkeypatch.setenv("GROK_BIN", str(grok_bin))

    # The bridge fixtures reload config/app between tests. Remove these
    # adapters first so their module-level paths are rebuilt from this test's
    # isolated GROK_BIN rather than retaining a previous test's temp directory.
    for module_name in ("grok_bridge.grok_disk", "grok_bridge.commands"):
        sys.modules.pop(module_name, None)


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
