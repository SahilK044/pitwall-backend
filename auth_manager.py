import os
import time
import json
import base64
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional
import requests

logger = logging.getLogger("f1_auth_manager")

# Load local .env if present without requiring external libraries
ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    if k and k not in os.environ:
                        os.environ[k] = v
    except Exception as e:
        logger.debug(f"Failed to load .env: {e}")


class F1AuthManager:
    """Manages F1TV entitlement tokens, JWT claims inspection, and automatic renewals."""

    def __init__(self):
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._token: str = os.environ.get("F1TV_TOKEN", "").strip()
        self._login_session: str = os.environ.get("F1TV_LOGIN_SESSION", "").strip()
        self._last_refresh_time: float = 0.0
        self._refresh_worker: Optional[threading.Thread] = None
        self._running: bool = False

    def start_scheduler(self):
        """Starts a background daemon thread that checks token expiration periodically."""
        with self._lock:
            if self._refresh_worker is not None and self._refresh_worker.is_alive():
                self._running = False
                self._stop_event.set()
                self._refresh_worker.join(timeout=2.0)
            self._stop_event.clear()
            self._running = True
            self._refresh_worker = threading.Thread(
                target=self._background_check_loop,
                daemon=True,
                name="F1Auth-Scheduler"
            )
            self._refresh_worker.start()
            logger.info("F1 token auto-refresh scheduler started.")

    def stop_scheduler(self):
        with self._lock:
            self._running = False
            self._stop_event.set()
            if self._refresh_worker is not None and self._refresh_worker.is_alive():
                self._refresh_worker.join(timeout=2.0)


    def current_token(self) -> str:
        """The token held now, without trying a renewal (never blocks on the network)."""
        return self._token

    def public_status(self) -> Dict[str, Any]:
        """Expiry only. The JWT also names the account holder; that is never served."""
        info = self.inspect_token(self._token)
        out = {k: info.get(k) for k in ("valid", "error", "exp_utc", "iat_utc", "remaining_seconds", "is_expired")}
        # "valid" means usable: an expired token is not, whatever its format.
        if out.get("is_expired"):
            out["valid"] = False
            out["error"] = out.get("error") or "Token expired"
        return out

    def get_token(self) -> str:
        """The current entitlement token, renewed first if it is close to expiry. Never waits on a
        renewal already running elsewhere: the live feed calls this when it (re)connects."""
        if self.is_expiring(threshold_minutes=60):
            self._do_refresh(wait=False)
        with self._lock:
            return self._token

    def set_token(self, token: str) -> Dict[str, Any]:
        """Manually updates the entitlement token and writes to .env if available."""
        token = token.strip()
        info = self.inspect_token(token)
        if not info.get("valid"):
            return info
        with self._lock:
            self._token = token
            if ENV_FILE.exists():
                try:
                    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
                    new_lines = []
                    found = False
                    for line in lines:
                        if line.strip().startswith("F1TV_TOKEN="):
                            new_lines.append(f"F1TV_TOKEN={token}")
                            found = True
                        else:
                            new_lines.append(line)
                    if not found:
                        new_lines.append(f"F1TV_TOKEN={token}")
                    tmp_file = ENV_FILE.with_suffix('.tmp')
                    tmp_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                    os.replace(tmp_file, ENV_FILE)
                except Exception as e:
                    logger.warning(f"Could not persist token to .env: {e}")
        return info

    def is_expiring(self, threshold_minutes: int = 60) -> bool:
        """Checks if token expires within the specified number of minutes."""
        info = self.inspect_token(self._token)
        if not info["valid"]:
            return bool(self._token)
        remaining = info.get("remaining_seconds", 0)
        return remaining <= (threshold_minutes * 60)

    @staticmethod
    def inspect_token(token_str: str) -> Dict[str, Any]:
        """Decodes JWT payload claims without network overhead."""
        if not token_str:
            return {"valid": False, "error": "No token configured"}
        try:
            parts = token_str.strip().split(".")
            if len(parts) != 3:
                return {"valid": False, "error": "Invalid JWT format"}

            payload_b64 = parts[1]
            rem = len(payload_b64) % 4
            if rem > 0:
                payload_b64 += "=" * (4 - rem)

            payload_str = base64.urlsafe_b64decode(payload_b64).decode("utf-8", errors="replace")
            payload = json.loads(payload_str)

            exp = payload.get("exp", 0)
            iat = payload.get("iat", 0)
            now = time.time()
            remaining_sec = max(0, exp - now)

            exp_utc = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else None
            iat_utc = datetime.fromtimestamp(iat, tz=timezone.utc).isoformat() if iat else None

            return {
                "valid": True,
                "subscriber_id": payload.get("SubscriberId"),
                "first_name": payload.get("FirstName"),
                "last_name": payload.get("LastName"),
                "subscription_status": payload.get("SubscriptionStatus"),
                "subscribed_product": payload.get("SubscribedProduct"),
                "subscription": payload.get("Subscription"),
                "exp_timestamp": exp,
                "exp_utc": exp_utc,
                "iat_utc": iat_utc,
                "remaining_seconds": int(remaining_sec),
                "is_expired": remaining_sec <= 0
            }
        except Exception as e:
            return {"valid": False, "error": str(e)}

    def refresh(self) -> Dict[str, Any]:
        """External call to force a refresh."""
        success = self._do_refresh()
        with self._lock:
            info = self.inspect_token(self._token)
        info["refreshed"] = success
        return info

    def _do_refresh(self, wait: bool = True) -> bool:
        """Internal refresh routine using F1TV Entitlement check endpoint. Callers must NOT hold
        self._lock: it is taken briefly inside (it is not re-entrant). With [wait] False it returns
        at once if another renewal is in progress."""
        if not self._refresh_lock.acquire(blocking=wait):
            return False
        try:
            return self._renew()
        finally:
            self._refresh_lock.release()

    def _renew(self) -> bool:
        now = time.time()
        if now - self._last_refresh_time < 60:
            return False

        self._last_refresh_time = now
        logger.info("[Auth] Checking token renewal...")

        with self._lock:
            current_token = self._token
            login_session = self._login_session

        # Fast HTTP renewal against F1TV Entitlement endpoint
        try:
            url = "https://f1tv.formula1.com/2.0/R/ENG/WEB_DASH/ALL/USER/ENTITLEMENT"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "entitlementToken": current_token,
                "ascendontoken": current_token
            }
            cookies = {}
            if login_session:
                cookies["login-session"] = login_session

            res = requests.get(url, headers=headers, cookies=cookies, timeout=10)
            if res.status_code == 200:
                data = res.json()
                new_token = data.get("resultObj", {}).get("entitlementToken")
                if new_token:
                    with self._lock:
                        self._token = new_token
                    logger.info("[Auth] Token refreshed successfully via F1TV API.")
                    return True
        except Exception as e:
            logger.warning(f"[Auth] HTTP token refresh failed: {e}")

        logger.warning("[Auth] Token refresh could not acquire a new token. Retaining current token.")
        return False

    def check_once(self) -> None:
        """One pass of the background check: renew when within 2 hours of expiry. No self._lock
        here: _do_refresh takes it itself (holding it first deadlocked this thread, and behind it
        every caller of get_token and set_token, once expiry neared)."""
        if self.is_expiring(threshold_minutes=120):
            self._do_refresh()

    def _background_check_loop(self):
        """Checks every 15 minutes and refreshes if within 2 hours of expiry."""
        logger.info("[Auth] Background token scheduler loop active.")
        while self._running and not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception as e:
                logger.error(f"[Auth] Error in background scheduler loop: {e}")

            if self._stop_event.wait(timeout=900):
                break



# Global singleton instance
auth_manager = F1AuthManager()
