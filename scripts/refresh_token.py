#!/usr/bin/env python3
"""
F1TV Token Auto-Refresher via Playwright
Extracts a fresh entitlement_token from F1TV and syncs it to the Pitwall backend.
"""

import os
import sys
import time
import json
import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("f1_token_refresher")

BACKEND_URL = os.environ.get("BACKEND_URL", "https://pitwall-backend-rj02.onrender.com").rstrip("/")
F1_EMAIL = os.environ.get("F1_EMAIL", "").strip()
F1_PASSWORD = os.environ.get("F1_PASSWORD", "").strip()
F1_STORAGE_STATE = os.environ.get("F1_STORAGE_STATE", "").strip()


def inspect_jwt(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {"valid": False}
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "===").decode("utf-8", errors="replace"))
        exp = payload.get("exp", 0)
        iat = payload.get("iat", 0)
        now = time.time()
        return {
            "valid": True,
            "subscriber_id": payload.get("SubscriberId"),
            "name": f"{payload.get('FirstName', '')} {payload.get('LastName', '')}".strip(),
            "product": payload.get("SubscribedProduct"),
            "exp_utc": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else None,
            "remaining_hours": round((exp - now) / 3600, 1),
            "is_expired": now >= exp
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}


def push_to_backend(token: str) -> bool:
    url = f"{BACKEND_URL}/api/v1/auth/token"
    logger.info(f"Pushing fresh token to {url}...")
    try:
        res = requests.post(url, json={"token": token}, timeout=15)
        if res.status_code == 200:
            data = res.json()
            if data.get("success"):
                logger.info(f"Successfully updated backend token! Info: {data.get('token_info')}")
                return True
            else:
                logger.error(f"Backend rejected token update: {data}")
        else:
            logger.error(f"Backend returned HTTP {res.status_code}: {res.text}")
    except Exception as e:
        logger.error(f"Failed to push token to backend: {e}")
    return False


def run():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright is not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    logger.info("Starting headless browser for F1TV session refresh...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
        )

        context_kwargs = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "viewport": {"width": 1920, "height": 1080},
            "locale": "en-US",
            "timezone_id": "Asia/Kolkata"
        }

        # Check for storage state (cookies/session saved)
        temp_state_file = None
        if F1_STORAGE_STATE:
            if os.path.exists(F1_STORAGE_STATE):
                context_kwargs["storage_state"] = F1_STORAGE_STATE
                logger.info(f"Using storage state file: {F1_STORAGE_STATE}")
            else:
                try:
                    # Treat as JSON string
                    state_data = json.loads(F1_STORAGE_STATE)
                    temp_state_file = Path("temp_storage_state.json")
                    temp_state_file.write_text(json.dumps(state_data), encoding="utf-8")
                    context_kwargs["storage_state"] = str(temp_state_file)
                    logger.info("Loaded storage state from environment variable.")
                except Exception as e:
                    logger.warning(f"Could not parse F1_STORAGE_STATE as JSON: {e}")

        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        # Stealth evasion
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.navigator.chrome = { runtime: {} };
        """)

        entitlement_token = None

        # Hook network responses to detect entitlement tokens
        def handle_response(response):
            nonlocal entitlement_token
            if "USER/ENTITLEMENT" in response.url and response.status == 200:
                try:
                    body = response.json()
                    tok = body.get("resultObj", {}).get("entitlementToken")
                    if tok:
                        logger.info("Captured fresh entitlementToken from network response!")
                        entitlement_token = tok
                except Exception:
                    pass

        page.on("response", handle_response)

        # 1. First attempt: Visit F1TV directly (works if session cookies are valid)
        logger.info("Navigating to https://f1tv.formula1.com ...")
        try:
            page.goto("https://f1tv.formula1.com", timeout=30000, wait_until="networkidle")
            time.sleep(3)
        except Exception as e:
            logger.warning(f"Initial navigation notice: {e}")

        # Check cookies for entitlement_token
        cookies = context.cookies(["https://f1tv.formula1.com", "https://formula1.com"])
        for c in cookies:
            if c.get("name") == "entitlement_token" and c.get("value"):
                entitlement_token = c["value"]
                logger.info("Found entitlement_token in cookies!")
                break

        # 2. If token not found and credentials provided, perform login
        if not entitlement_token and F1_EMAIL and F1_PASSWORD:
            logger.info("Session not active. Attempting login with credentials...")
            try:
                page.goto("https://account.formula1.com/#/en/login", timeout=30000, wait_until="domcontentloaded")
                time.sleep(2)

                # Dismiss cookie consent if present
                try:
                    consent_btn = page.query_selector("#onetrust-accept-btn-handler") or page.query_selector("button:has-text('Accept All')")
                    if consent_btn:
                        consent_btn.click()
                        time.sleep(1)
                except Exception:
                    pass

                # Fill login form
                logger.info("Submitting login form...")
                page.fill('input[name="Login"], input[type="email"]', F1_EMAIL)
                page.fill('input[name="Password"], input[type="password"]', F1_PASSWORD)
                time.sleep(1)

                submit_btn = page.query_selector('button[type="submit"]') or page.query_selector('.actions button')
                if submit_btn:
                    submit_btn.click()
                    page.wait_for_timeout(5000)

                # Redirect to F1TV
                logger.info("Returning to F1TV to mint entitlement token...")
                page.goto("https://f1tv.formula1.com", timeout=30000, wait_until="networkidle")
                time.sleep(3)

                cookies = context.cookies(["https://f1tv.formula1.com", "https://formula1.com"])
                for c in cookies:
                    if c.get("name") == "entitlement_token" and c.get("value"):
                        entitlement_token = c["value"]
                        break
            except Exception as e:
                logger.error(f"Login flow encountered error: {e}")

        # Clean up temporary state file
        if temp_state_file and temp_state_file.exists():
            temp_state_file.unlink(missing_ok=True)

        browser.close()

    # 3. Validate and Push
    if entitlement_token:
        info = inspect_jwt(entitlement_token)
        logger.info(f"Acquired token details: {info}")
        if info.get("valid") and not info.get("is_expired"):
            success = push_to_backend(entitlement_token)
            if success:
                logger.info(f"✅ Token refresh successful! Valid for {info.get('remaining_hours')} hours.")
                sys.exit(0)
            else:
                logger.error("❌ Failed to push token to backend.")
                sys.exit(1)
        else:
            logger.error(f"❌ Extracted token is invalid or expired: {info}")
            sys.exit(1)
    else:
        logger.error(
            "❌ Could not obtain entitlement_token.\n"
            "If Akamai blocked automated login, export your browser session cookies\n"
            "as storageState JSON and configure the F1_STORAGE_STATE secret."
        )
        sys.exit(1)


if __name__ == "__main__":
    run()
