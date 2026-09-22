"""mangago.me login helper: drives a real headless Firefox instance end to
end, but doesn't need you to touch it -- it reads the captcha itself.

mangago requires a logged-in session to read a full chapter in one request
(see README.md "Why mangago needs an account"). Its login form is behind an
image captcha and, as it turned out, Cloudflare bot-management that no pure
HTTP client can satisfy -- an earlier version of this script tried to
replicate a browser closely enough via httpx and then curl_cffi (matching
headers, cookies, and even real browser TLS fingerprints), and logins still
silently "succeeded" (normal-looking redirect, session never actually
authenticated) no matter how exactly the request was constructed. That
points at either a JS-executed bot-detection challenge (something no raw
HTTP request can produce) or anti-brute-force throttling from the many
attempts spent diagnosing that.

The fix was a genuine browser (via Playwright) instead of ever-closer HTTP
mimicry -- real TLS, real JS execution, so none of the above applies. What
this version automates on top of that: it screenshots just the captcha
image and waits for ANSWER_PATH to contain the text, instead of waiting for
a human to type it into the page. Whoever/whatever can read the image
writes the 5 characters to that file and this script takes it from there
(fills the field, clicks submit, confirms login, saves the session cookie).
In practice that's Claude reading the screenshot with its own vision and
writing the answer file -- see the workflow this docstring ends with.

Usage (from the repo root, needs two terminals/processes since step 1
blocks waiting for step 2):
  python -m tests.mangago_login
    -> prints when data/mangago_captcha.png is ready, then blocks
  (read the image, write its text to data/.mangago_captcha_answer.txt)
    -> the waiting process picks it up, logs in, writes MANGAGO_COOKIE

Not part of the app or the pytest run -- a standalone tool, like
tests/benchmark_server.py, and needs `pip install playwright` (the
`mangago` extra) plus a one-time `playwright install firefox`.
"""

from __future__ import annotations

import os
import re
import sys
import time

import httpx
from dotenv import load_dotenv, set_key
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

sys.path.insert(0, ".")
from config import ROOT_DIR  # noqa: E402

LOGIN_PAGE = "https://www.mangago.me/home/accounts/login/?redir=https://www.mangago.me/"
HOME_URL = "https://www.mangago.me/"
ENV_PATH = ROOT_DIR / ".env"
DATA_DIR = ROOT_DIR / "data"
CAPTCHA_PATH = DATA_DIR / "mangago_captcha.png"
ANSWER_PATH = DATA_DIR / ".mangago_captcha_answer.txt"

POLL_INTERVAL_S = 1.5
LOGIN_TIMEOUT_S = 120  # after the captcha answer is filled in and submitted
ANSWER_WAIT_TIMEOUT_S = 300  # waiting for the answer file to show up
CAPTCHA_LOAD_ATTEMPTS = 10
CAPTCHA_LOAD_TIMEOUT_MS = 4000


def _open_login_page(page, email: str | None, password: str | None) -> None:
    # Straight to the login page, no homepage stopover -- this is a real
    # browser now, not something trying to look like a natural click-through.
    page.goto(LOGIN_PAGE)
    if email:
        page.fill("#email", email)
    if password:
        page.fill("#password", password)


def _captcha_loaded(page) -> bool:
    """The captcha <img> 404s intermittently even on a fresh page load -- a
    broken image still renders as an <img> element, just with naturalWidth
    0, so checking visibility alone isn't enough."""
    img = page.locator('img[src*="/captcha/"]')
    try:
        img.wait_for(state="visible", timeout=CAPTCHA_LOAD_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return False
    return bool(img.evaluate("el => el.naturalWidth > 0"))


def _wait_for_answer() -> str:
    print(f"Waiting up to {ANSWER_WAIT_TIMEOUT_S}s for {ANSWER_PATH} to contain the captcha text...")
    deadline = time.monotonic() + ANSWER_WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if ANSWER_PATH.exists():
            text = ANSWER_PATH.read_text(encoding="utf-8").strip()
            ANSWER_PATH.unlink()
            if text:
                return text
        time.sleep(0.5)
    print(f"Timed out after {ANSWER_WAIT_TIMEOUT_S}s waiting for a captcha answer.")
    sys.exit(1)


def _is_logged_in_html(html: str) -> bool:
    return 'id="user-avatar"' in html or bool(re.search(r'href="https://www\.mangago\.me/home/accounts/logout/"', html))


def _logged_in(page) -> bool:
    return _is_logged_in_html(page.content())


def _cached_cookie_still_valid(cookie: str) -> bool:
    """A quick plain-HTTP check, not a login attempt -- just confirms an
    already-established session is still recognized, so a re-run doesn't
    pop a browser window when yesterday's session is still perfectly good."""
    try:
        resp = httpx.get(HOME_URL, headers={"Cookie": cookie}, timeout=15, follow_redirects=True)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200 and _is_logged_in_html(resp.text)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ANSWER_PATH.unlink(missing_ok=True)
    load_dotenv(ENV_PATH)
    email = os.environ.get("MANGAGO_EMAIL")
    password = os.environ.get("MANGAGO_PASSWORD")

    existing_cookie = os.environ.get("MANGAGO_COOKIE")
    if existing_cookie and _cached_cookie_still_valid(existing_cookie):
        print("Existing MANGAGO_COOKIE session is still valid -- nothing to do.")
        return

    with sync_playwright() as pw:
        browser = pw.firefox.launch(headless=True)
        page = browser.new_page()
        _open_login_page(page, email, password)

        for attempt in range(1, CAPTCHA_LOAD_ATTEMPTS + 1):
            if _captcha_loaded(page):
                break
            print(f"  captcha didn't load (attempt {attempt}/{CAPTCHA_LOAD_ATTEMPTS}), reloading...")
            _open_login_page(page, email, password)
        else:
            print(f"Captcha never loaded after {CAPTCHA_LOAD_ATTEMPTS} attempts. Try again later.")
            browser.close()
            sys.exit(1)

        page.locator('img[src*="/captcha/"]').screenshot(path=str(CAPTCHA_PATH))
        print(f"Captcha saved to {CAPTCHA_PATH}")

        captcha_text = _wait_for_answer()
        page.fill("#captcha", captcha_text)
        page.click("#submit_login")

        deadline = time.monotonic() + LOGIN_TIMEOUT_S
        logged_in = False
        while time.monotonic() < deadline:
            if _logged_in(page):
                logged_in = True
                break
            time.sleep(POLL_INTERVAL_S)

        if not logged_in:
            print(f"Login did not succeed within {LOGIN_TIMEOUT_S}s -- likely a wrong captcha guess.")
            print("Rerun this script for a fresh captcha and try again.")
            browser.close()
            sys.exit(1)

        cookies = page.context.cookies()
        phpsessid = next((c["value"] for c in cookies if c["name"] == "PHPSESSID"), None)
        browser.close()

        if not phpsessid:
            print("Logged in, but couldn't find a PHPSESSID cookie -- site behavior may have changed.")
            sys.exit(1)

        set_key(str(ENV_PATH), "MANGAGO_COOKIE", f"PHPSESSID={phpsessid}")
        print(f"Login succeeded. Wrote MANGAGO_COOKIE to {ENV_PATH}.")


if __name__ == "__main__":
    main()
