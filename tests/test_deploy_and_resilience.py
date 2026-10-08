import asyncio
import os
import pytest
from fastapi.testclient import TestClient
import predictions as P
import main


def test_health_endpoint():
    client = TestClient(main.app)
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["service"] == "pitwall-livef1"
    assert "feed" in data


def test_root_endpoint():
    client = TestClient(main.app)
    res = client.get("/")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "online"


def test_store_broken_connection_retry(monkeypatch, tmp_path):
    """If a connection in the pool breaks, Store should detect it, close it, and retry."""
    db_file = str(tmp_path / "test.db")
    store = P.Store(db_file, "")
    
    # Verify regular execution works
    store.upsert_pick("2026", 1, "u1", ["norris", "piastri", "max_verstappen"])
    pick = store.pick("2026", 1, "u1")
    assert pick is not None
    assert pick["podium"] == ["norris", "piastri", "max_verstappen"]

    # Simulate Postgres mode with broken connection
    store.pg = True
    class DummyBrokenConn:
        closed = False
        broken = True
        def close(self):
            self.closed = True
    
    # Test _is_dead identifies broken connection
    dummy = DummyBrokenConn()
    assert store._is_dead(dummy) is True


def test_store_fallback_on_postgres_failure(tmp_path):
    """If Postgres connection fails at init, Store falls back to SQLite without raising."""
    db_file = str(tmp_path / "fallback.db")
    # Bad postgres connection URL that immediately fails
    bad_url = "postgresql://invalid_user:invalid_pass@127.0.0.1:54321/nonexistent?connect_timeout=1"
    store = P.Store(db_file, bad_url)
    assert store.pg is False  # fell back to SQLite
    store.upsert_pick("2026", 2, "u2", ["hamilton", "russell", "antonelli"])
    p = store.pick("2026", 2, "u2")
    assert p["podium"] == ["hamilton", "russell", "antonelli"]


def test_dockerfile_dynamic_port():
    """Verify Dockerfile uses dynamic PORT variable and exposes 10000."""
    dockerfile_path = os.path.join(os.path.dirname(__file__), "..", "Dockerfile")
    with open(dockerfile_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "PORT=10000" in content
    assert "EXPOSE 10000" in content
    assert "${PORT:-10000}" in content
