"""
Convert a Cookie-Editor JSON export into a Playwright session file.

Workflow:
  1. In Chrome, log into the IKEA account manually
  2. Open Cookie-Editor extension → Export → Export as JSON → save the file
  3. Run: python import_cookies.py <cookie-file.json> <account-label>

Example:
    python import_cookies.py account1_cookies.json account-1

The session file is written to sessions/<account-label>.json and is
picked up automatically by ikea_poc.py and record.py.
"""

import json
import sys
import time
from pathlib import Path

SESSIONS_DIR = Path("sessions")

SAMESIDE_MAP = {
    "Strict": "Strict",
    "Lax": "Lax",
    "None": "None",
    "no_restriction": "None",
    "lax": "Lax",
    "strict": "Strict",
    "unspecified": "Lax",   # safe default
}


def convert(raw_cookies: list[dict]) -> dict:
    """Convert Cookie-Editor format to Playwright storage_state format."""
    pw_cookies = []
    for c in raw_cookies:
        # Cookie-Editor uses 'expirationDate'; -1 means session cookie
        expires = c.get("expirationDate", -1)
        if expires is None:
            expires = -1
        else:
            expires = int(expires)

        same_site = SAMESIDE_MAP.get(c.get("sameSite", "Lax"), "Lax")

        # Playwright requires domain to start with a dot for host cookies
        domain = c.get("domain", "")

        pw_cookies.append({
            "name":     c["name"],
            "value":    c["value"],
            "domain":   domain,
            "path":     c.get("path", "/"),
            "expires":  expires,
            "httpOnly": c.get("httpOnly", False),
            "secure":   c.get("secure", False),
            "sameSite": same_site,
        })

    return {"cookies": pw_cookies, "origins": []}


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python import_cookies.py <cookie-file.json> <account-label>")
        sys.exit(1)

    cookie_file = Path(sys.argv[1])
    label = sys.argv[2]

    if not cookie_file.exists():
        print(f"File not found: {cookie_file}")
        sys.exit(1)

    raw = json.loads(cookie_file.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        print("Expected a JSON array from Cookie-Editor. Got something else.")
        sys.exit(1)

    state = convert(raw)

    SESSIONS_DIR.mkdir(exist_ok=True)
    out = SESSIONS_DIR / f"{label}.json"
    out.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # Warn about cookies that expire soon
    now = time.time()
    expiring = [
        c["name"] for c in state["cookies"]
        if 0 < c["expires"] < now + 86_400   # expires within 24 h
    ]
    if expiring:
        print(f"Warning: these cookies expire within 24 h: {expiring}")

    print(f"Saved {len(state['cookies'])} cookies -> {out}")
    print(f"Run the automation with:  python ikea_poc.py --{label}")


if __name__ == "__main__":
    main()
