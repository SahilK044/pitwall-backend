"""
Pitwall's Privacy Policy and Terms of Service as web pages: the app links here, and the same words
are shown inside the app. Plain, readable HTML in the app's colours; no scripts, no trackers.
"""
import html
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["legal"])

SUPPORT_EMAIL = "pitwallsupport@gmail.com"
WEBSITE = "https://www.pitwallf1.website"
INSTAGRAM = "https://www.instagram.com/pitwall._f1/"
UPDATED = "October 2026"

PRIVACY = [
    ("Information we collect", [
        "Account data: when you link your Google Play account, Pitwall receives your email address and display name, used only to identify and restore Pitwall Pro subscriptions across devices.",
        "Podium predictions: a random install ID (not linked to your Google account), the username you choose, and your podium picks are stored on Pitwall's server to keep your score, history and place on the leaderboard. Other players only see your username and points.",
        "Diagnostics: anonymised crash logs, device model and Android version, used only to fix problems with the app and live data.",
        "No location tracking: Pitwall does not use your device's location. Weather and rain alerts are for the circuit, not for where you are.",
    ]),
    ("Subscriptions and payments", [
        "All payments and subscriptions are handled by Google Play Billing. Pitwall never sees or stores your card or other payment details.",
    ]),
    ("Sharing and third parties", [
        "Pitwall does not sell, rent or trade your personal data. Data is shared only with Google Play services (sign-in and purchase checks) and with Pitwall's hosting providers that run the prediction and live-data servers.",
        "Live timing, results and weather come from public sources (Formula 1 live timing, OpenF1, Jolpica and Open-Meteo). Pitwall sends them no personal data.",
    ]),
    ("Your choices", [
        "You can sign out of your linked Google Play account at any time in Settings. To have your prediction username and picks deleted, email us with your username.",
    ]),
    ("Contact", [
        f"Email: {SUPPORT_EMAIL}",
        f"Website: {WEBSITE}",
        f"Instagram: {INSTAGRAM}",
    ]),
]

TERMS = [
    ("Acceptance", [
        "By downloading, installing or using Pitwall you agree to these terms. If you don't agree, please don't use the app.",
    ]),
    ("Subscriptions and billing", [
        "Pitwall Pro subscriptions are billed through your Google Play account when you confirm the purchase.",
        "Subscriptions renew automatically unless cancelled at least 24 hours before the end of the current period.",
        "You can manage or cancel a subscription in the Google Play Store under Payments and subscriptions. Cancelling takes effect at the end of the current billing period.",
    ]),
    ("Live data", [
        "Live timing, telemetry and team radio depend on third-party timing feeds and your connection. Pitwall can't guarantee they are available for every session.",
        "Some figures in the app are Pitwall's own calculations (for example race ratings, track dominance and tyre life estimates) and are labelled as such. They are not official Formula 1 data.",
    ]),
    ("Podium predictions", [
        "Predictions are a free game for fun, with no prizes or money involved. Usernames must not impersonate others or be offensive; Pitwall may remove ones that are.",
    ]),
    ("Trademarks", [
        "Pitwall is an independent fan app. It is unofficial and is not associated in any way with the Formula 1 companies. F1, FORMULA ONE, FORMULA 1, FIA FORMULA ONE WORLD CHAMPIONSHIP, GRAND PRIX and related marks are trade marks of Formula One Licensing B.V.",
    ]),
    ("Liability", [
        "Pitwall and its developers are not liable for any indirect, incidental or consequential damages arising from using or being unable to use the app.",
    ]),
    ("Contact", [
        f"Email: {SUPPORT_EMAIL}",
        f"Website: {WEBSITE}",
        f"Instagram: {INSTAGRAM}",
    ]),
]


def page(title: str, sections) -> str:
    body = "".join(
        f"<section><h2>{i}. {html.escape(h)}</h2>" + "".join(f"<p>{html.escape(p)}</p>" for p in ps) + "</section>"
        for i, (h, ps) in enumerate(sections, 1)
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · Pitwall</title>
<style>
:root{{--bg:#0A0B10;--card:#14151C;--ink:#F4F5F9;--soft:#A4A7B5;--line:#24252F;--red:#E10600}}
@media (prefers-color-scheme: light){{:root{{--bg:#F5F6F9;--card:#FFFFFF;--ink:#0E1016;--soft:#4A4F5E;--line:#E4E6EC}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:40px 20px 64px}}
.brand{{font-weight:900;font-style:italic;letter-spacing:.02em}}.brand span{{color:var(--red)}}
h1{{font-size:32px;line-height:1.15;margin:18px 0 4px}}.updated{{color:var(--soft);margin:0 0 28px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:18px 20px;margin:12px 0}}
h2{{font-size:17px;margin:0 0 8px}}p{{color:var(--soft);margin:8px 0}}a{{color:var(--ink)}}
</style></head><body><main>
<div class="brand">PIT<span>WALL</span></div>
<h1>{html.escape(title)}</h1><p class="updated">Last updated {UPDATED}</p>
{body}
<p style="margin-top:28px"><a href="{WEBSITE}">pitwallf1.website</a></p>
</main></body></html>"""


@router.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return page("Privacy Policy", PRIVACY)


@router.get("/terms", response_class=HTMLResponse)
async def terms():
    return page("Terms of Service", TERMS)


def as_text(sections) -> str:
    """The same words as plain text, for the in-app copy (kept in step by tests)."""
    return "\n\n".join(f"{i}. {h}\n" + "\n".join(f"• {p}" for p in ps) for i, (h, ps) in enumerate(sections, 1))
