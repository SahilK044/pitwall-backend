#!/usr/bin/env python3
"""
Helper utility to capture F1TV session cookies/storage on your local machine.
Runs an interactive browser window. Once you log in, it saves `auth.json`.
You can then paste the content of `auth.json` into GitHub Secrets as `F1_STORAGE_STATE`.
"""

import sys
from pathlib import Path

def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed! Run:")
        print("    pip install playwright")
        print("    playwright install chromium")
        sys.exit(1)

    output_path = Path(__file__).parent / "auth.json"
    print("\n=======================================================")
    print(" F1TV Interactive Session Saver")
    print("=======================================================")
    print("1. A browser window will open.")
    print("2. Log into your F1TV Pro account.")
    print("3. Once logged in and on f1tv.formula1.com, close the browser.")
    print("=======================================================\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.goto("https://f1tv.formula1.com")

        print("Waiting for browser to be closed after you finish logging in...")
        try:
            page.wait_for_event("close", timeout=300000)
        except Exception:
            pass

        # Save storage state
        context.storage_state(path=str(output_path))
        browser.close()

    print(f"\n✅ Session saved successfully to: {output_path.resolve()}")
    print("To use in GitHub Actions:")
    print("1. Open `auth.json` and copy its entire contents.")
    print("2. Go to your GitHub repository -> Settings -> Secrets and variables -> Actions.")
    print("3. Add secret `F1_STORAGE_STATE` and paste the content.")


if __name__ == "__main__":
    main()
