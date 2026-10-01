"""The token routes change what every live feed runs on: without the admin key they must not exist."""
import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

import main


def fake_jwt(exp: float) -> str:
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part({'exp': int(exp), 'FirstName': '<script>alert(1)</script>', 'LastName': 'Holder'})}.sig"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("PITWALL_ADMIN_KEY", "k" * 32)
    set_calls = []
    monkeypatch.setattr(main.auth_manager, "set_token", lambda t: set_calls.append(t) or main.auth_manager.inspect_token(t))
    monkeypatch.setattr(main.engine, "update_token", lambda t: None)
    c = TestClient(main.app)                     # no "with": the live engine never starts
    c.set_calls = set_calls
    return c


def test_token_routes_hidden_without_key(client):
    tok = fake_jwt(time.time() + 3600)
    assert client.post("/api/v1/auth/token", json={"token": tok}).status_code == 404
    assert client.post("/api/v1/auth/token", json={"token": tok}, headers={"X-Admin-Key": "wrong"}).status_code == 404
    assert client.get("/auth/sync", params={"token": tok}).status_code == 404
    assert client.get("/api/v1/auth/sync", params={"token": tok, "key": "wrong"}).status_code == 404
    assert client.set_calls == []                # the token was never touched


def test_token_routes_with_key_and_no_name_leak(client):
    tok = fake_jwt(time.time() + 3600)
    r = client.post("/api/v1/auth/token", json={"token": tok}, headers={"X-Admin-Key": "k" * 32})
    assert r.status_code == 200 and r.json()["success"] is True
    assert "Holder" not in r.text and "script" not in r.text
    page = client.get("/auth/sync", params={"token": tok, "key": "k" * 32})
    assert page.status_code == 200 and "Synced to Pitwall" in page.text
    assert "Holder" not in page.text and "<script>alert" not in page.text


def test_refresh_needs_key(client, monkeypatch):
    monkeypatch.delenv("PITWALL_ADMIN_KEY")
    assert client.post("/api/v1/auth/refresh", headers={"X-Admin-Key": "anything"}).status_code == 404
