#!/usr/bin/env python3
"""
browser_use_relogin.py — session recovery for the persistent browser profile.

When a saved session expires (the agent lands on a login page instead of the
dashboard), this script re-authenticates ONE site deterministically — no LLM,
no agent loop — using selectors from a local credentials store, then writes
the fresh cookies into the SAME persistent profile the browser-use 1.0 agent
uses (`~/.local/share/jarvis-browser-profile/bu`, passed as `user_data_dir=`).
Next agent run is logged in again. (browser-use 1.0 removed the old
`storage_state.json` primitive; a profile directory is the supported one.)

Credentials store (created by hand, NEVER committed, chmod 600 enforced):
  ~/.local/share/jarvis-browser-profile/site_credentials.json

  {
    "mybank": {
      "login_url":        "https://mybank.example/login",
      "username_selector": "input[name=email]",
      "password_selector": "input[name=password]",
      "submit_selector":   "button[type=submit]",
      "username":         "me@example.com",
      "password":         "...",
      "success_text":     "Dashboard",       // optional marker after login
      "success_url_part": "/dashboard",      // optional URL marker after login
      "cookies_domains":  ["mybank.example"] // optional: domains to clear first
    }
  }

Usage:
  python3 scripts/browser_use_relogin.py <site_key> [--visible] [--clear]
  python3 scripts/browser_use_relogin.py --list

  --clear   drop existing cookies for the site first (stale-session reset)
  --visible show the browser (2FA/captcha logins — never do those unattended)

Exit codes: 0 = re-login verified, 1 = login failed (see stderr),
2 = bad usage / missing store / permissions problem.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

STATE_DIR = Path.home() / ".local" / "share" / "jarvis-browser-profile"
# browser-use 1.0 removed `storage_state`. The supported persistence primitive
# is now a profile directory passed to the agent as `user_data_dir=`, so this
# script writes into the SAME profile the runner uses: a login recovered here
# is the login the browser agent has on its next run.
PROFILE_DIR = STATE_DIR / "bu"
CREDS = STATE_DIR / "site_credentials.json"
CHROME = "/usr/bin/google-chrome"

REQUIRED_FIELDS = (
    "login_url",
    "username_selector",
    "password_selector",
    "submit_selector",
    "username",
    "password",
)


def _load_store(require: bool = True) -> dict:
    if not CREDS.exists():
        print(
            f"credentials store missing: {CREDS}\n"
            f"Create it from scripts/site_credentials.template.json and "
            f"run: chmod 600 {CREDS}",
            file=sys.stderr,
        )
        if require:
            raise SystemExit(2)
        return {}
    mode = stat.S_IMODE(CREDS.stat().st_mode)
    if mode & 0o077:
        print(
            f"REFUSING to run: {CREDS} is readable by group/others "
            f"(mode {oct(mode)}). Fix with: chmod 600 {CREDS}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        return json.loads(CREDS.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        print(f"credentials store unreadable: {e}", file=sys.stderr)
        raise SystemExit(2)


def _check_entry(key: str, entry: dict) -> None:
    missing = [f for f in REQUIRED_FIELDS if not str(entry.get(f, "")).strip()]
    if missing:
        print(f"site '{key}' missing fields: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)


def relogin(key: str, visible: bool = False, clear: bool = False) -> int:
    """Re-login one site via Playwright; returns 0 on verified success."""
    from playwright.sync_api import sync_playwright

    entry = _load_store().get(key)
    if entry is None:
        print(
            f"no credentials for site '{key}'. Known: "
            f"{', '.join(sorted(_load_store(require=False))) or '(none)'}",
            file=sys.stderr,
        )
        return 2
    _check_entry(key, entry)

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # A persistent context IS the profile: cookies written by this login
        # stay on disk for the browser-use agent's next `user_data_dir=` run.
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=not visible,
            executable_path=CHROME,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                *(["--ozone-platform=x11"] if visible else []),
            ],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            if clear:
                domains = entry.get("cookies_domains") or []
                cookies = ctx.cookies()
                keep = [
                    c
                    for c in cookies
                    if not any(d in c.get("domain", "") for d in domains)
                ]
                dropped = len(cookies) - len(keep)
                if dropped:
                    ctx.clear_cookies()
                    if keep:
                        ctx.add_cookies(keep)
                    print(
                        f"cleared {dropped} stale cookie(s) for {domains}",
                        file=sys.stderr,
                    )

            print(f"[relogin] opening {entry['login_url']}", file=sys.stderr)
            page.goto(entry["login_url"], timeout=30000)
            page.fill(entry["username_selector"], entry["username"])
            page.fill(entry["password_selector"], entry["password"])
            page.click(entry["submit_selector"])
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1500)  # let post-login redirects settle

            body = page.inner_text("body")
            url = page.url
            ok_text = (
                str(entry.get("success_text", "")).lower() in body.lower()
                if entry.get("success_text")
                else None
            )
            ok_url = (
                str(entry.get("success_url_part", "")) in url
                if entry.get("success_url_part")
                else None
            )
            still_on_login = "login" in url.lower()

            if (
                (ok_text is False or ok_url is False or still_on_login)
                and ok_text is not True
                and ok_url is not True
            ):
                print(
                    f"[relogin] FAILED for {key}: url={url[:100]} "
                    f"page='{body.strip()[:80]}'",
                    file=sys.stderr,
                )
                return 1

            # Cookies are committed to the profile when the context closes;
            # report the count so a caller can see the session is real.
            saved = len(ctx.cookies())
            print(
                f"[relogin] session saved -> {PROFILE_DIR} ({saved} cookies)",
                file=sys.stderr,
            )
            print(f"[relogin] {key}: logged in (url={url[:100]})", file=sys.stderr)
            return 0
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-login a site into the " "persistent browser profile"
    )
    ap.add_argument("site", nargs="?", help="site key from the credentials store")
    ap.add_argument(
        "--visible",
        action="store_true",
        help="show the browser (for 2FA/captcha — stay present)",
    )
    ap.add_argument(
        "--clear",
        action="store_true",
        help="drop the site's stale cookies before logging in",
    )
    ap.add_argument(
        "--list", action="store_true", help="list configured site keys and exit"
    )
    args = ap.parse_args()

    if args.list:
        store = _load_store(require=False)
        for k, v in sorted(store.items()):
            print(f"  {k:20} {v.get('login_url', '?')}")
        if not store:
            print("  (credentials store empty or missing)")
        return 0
    if not args.site:
        ap.print_usage(sys.stderr)
        return 2
    return relogin(args.site, visible=args.visible, clear=args.clear)


if __name__ == "__main__":
    sys.exit(main())
