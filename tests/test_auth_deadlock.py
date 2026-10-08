"""The token manager must never freeze: an expiring token once deadlocked the refresh thread and,
behind it, every caller of get_token (the live feed's reconnect) and set_token (manual sync)."""
import base64
import json
import threading
import time

import auth_manager as am


def jwt(exp: float) -> str:
    def b64(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64({'exp': int(exp), 'iat': int(exp) - 3600})}.sig"


def run_with_timeout(fn, seconds=3.0):
    done = threading.Event()
    def target():
        fn(); done.set()
    threading.Thread(target=target, daemon=True).start()
    return done.wait(seconds)


def test_expiring_token_never_freezes(monkeypatch):
    m = am.F1AuthManager()
    m._token = jwt(time.time() + 600)                      # inside the 2-hour renewal window
    monkeypatch.setattr(am.requests, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    # What the background loop does each cycle.
    assert run_with_timeout(lambda: m.check_once()), "background check froze"
    assert run_with_timeout(lambda: m.get_token()), "get_token froze"
    assert run_with_timeout(lambda: m.set_token(jwt(time.time() + 86400))), "set_token froze"


def test_get_token_does_not_wait_for_a_renewal_in_progress(monkeypatch):
    m = am.F1AuthManager()
    m._token = jwt(time.time() + 60)
    m._refresh_lock.acquire()                              # a slow renewal elsewhere
    try:
        assert run_with_timeout(lambda: m.get_token(), 1.0)
    finally:
        m._refresh_lock.release()


def test_expired_token_is_reported_unusable():
    m = am.F1AuthManager()
    m._token = jwt(time.time() - 60)
    st = m.public_status()
    assert st["is_expired"] is True and st["valid"] is False
