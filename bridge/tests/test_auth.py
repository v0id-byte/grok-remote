"""Pairing and device-auth hardening (plan v2 ss0.2).

These exist because removing Cloudflare Access collapsed two identity layers
into one: the Bridge's bearer token is now the only thing between a public
tunnel and an agent that can run shell commands.
"""
from __future__ import annotations


def _pair(client, store, name="iPhone"):
    token = store.create_pairing_token()
    r = client.post("/v1/pair", json={"pairing_token": token, "device_name": name})
    assert r.status_code == 200, r.text
    c = r.json()
    return token, f"{c['device_id']}.{c['secret']}", c["device_id"]


def test_pair_endpoint_hides_itself_when_nothing_is_redeemable(client):
    # 404 rather than 400: a scanner should not be able to tell it exists.
    assert client.post("/v1/pair", json={"pairing_token": "nope"}).status_code == 404


def test_pairing_token_is_single_use(client, bridge):
    token, _, _ = _pair(client, bridge.store)
    bridge.store.create_pairing_token()          # keep the endpoint "open"
    assert client.post("/v1/pair", json={"pairing_token": token}).status_code == 400


def test_expired_pairing_token_is_rejected(client, bridge):
    bridge.store.create_pairing_token()          # keep the endpoint "open"
    expired = bridge.store.create_pairing_token(ttl_seconds=-1)
    assert client.post("/v1/pair", json={"pairing_token": expired}).status_code == 400


def test_legacy_null_expiry_tokens_are_backfilled(client, bridge):
    """Rows predating the TTL had expires_at = NULL and stayed valid forever.
    The real database contained a 22-day-old unredeemed token when this landed."""
    import sqlite3
    store = bridge.store
    token = store.create_pairing_token()
    conn = sqlite3.connect(store.db_path)
    conn.execute("UPDATE pairing_tokens SET expires_at = NULL, created_at = created_at - 1e6 "
                 "WHERE token = ?", (token,))
    conn.commit()
    conn.close()

    from grok_bridge.db import Store
    reopened = Store(store.db_path)               # __init__ runs the migration
    assert reopened.purge_expired_pairing_tokens() == 1
    assert not reopened.has_redeemable_pairing_token()


def test_authenticated_routes_require_a_valid_bearer(client, bridge):
    _, bearer, _ = _pair(client, bridge.store)
    assert client.get("/v1/sessions", headers={"Authorization": f"Bearer {bearer}"}).status_code == 200
    assert client.get("/v1/sessions").status_code == 401
    assert client.get("/v1/sessions", headers={"Authorization": "Bearer x.y"}).status_code == 401


def test_revoked_device_stops_working(client, bridge):
    _, bearer, device_id = _pair(client, bridge.store)
    h = {"Authorization": f"Bearer {bearer}"}
    assert client.delete(f"/v1/devices/{device_id}", headers=h).json()["revoked"] is True
    assert client.get("/v1/sessions", headers=h).status_code == 401


def test_a_device_may_only_revoke_itself(client, bridge):
    _, bearer, _ = _pair(client, bridge.store)
    r = client.delete("/v1/devices/someone-else",
                      headers={"Authorization": f"Bearer {bearer}"})
    assert r.status_code == 403


def test_repeated_bad_tokens_are_throttled(client, bridge):
    codes = [client.get("/v1/sessions", headers={"Authorization": "Bearer bad.token"}).status_code
             for _ in range(bridge.config.AUTH_FAIL_MAX + 3)]
    assert 429 in codes


def test_health_reports_auth_signals(client):
    body = client.get("/health").json()
    assert "auth_failures" in body and "pairing_open" in body
