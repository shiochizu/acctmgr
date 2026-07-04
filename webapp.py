"""
FastAPI backend for the IKEA Automation Manager.
Manages accounts, schedules, run logs, and real-time WebSocket updates.
"""

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import secrets

import aiosqlite
import httpx
from fastapi import FastAPI, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------
from import_cookies import convert as convert_cookies
from api_client import create_favourites_list_via_page, join_and_confirm_event

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = Path("ikea_manager.db")
SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)
STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)

LOYALTY_API = "https://web-api.ikea.com/customer-engagement/reward-keys-experience/v2/customer/balance?keyExpirationDetail=true"
HISTORY_API  = "https://web-api.ikea.com/customer-engagement/reward-keys-experience/v2/customer/history/pl/pl"
LOYALTY_HEADERS = {
    "x-client-id": "fbe97bda-0003-4c45-894a-c6d9b89ce11c",
    "rexConsumerId": "rexFE-721b",
    "Accept": "application/json",
}
EVENT_ID = "0d551a19-7700-4807-b354-0f6047d7ab41"
LOYALTY_REFRESH_INTERVAL = 7200   # 2 hours
SCHEDULER_INTERVAL = 60           # 1 minute

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
ws_connections: list[WebSocket] = []
running_accounts: set[str] = set()

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_PASSWORD = "Dupa1337!"
AUTH_COOKIE   = "ikea_session"
_valid_sessions: set[str] = set()

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>IKEA Manager — Login</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#111;color:#fff;font-family:"Helvetica Neue",Helvetica,Arial,sans-serif;
      display:flex;align-items:center;justify-content:center;min-height:100vh}}
.box{{background:#1a1a1a;border:1px solid #2a2a2a;border-top:3px solid #FFDB00;
      border-radius:4px;padding:40px;width:100%;max-width:360px}}
.logo{{font-size:18px;font-weight:700;color:#FFDB00;letter-spacing:.05em;margin-bottom:24px}}
label{{display:block;font-size:12px;color:#888;margin-bottom:6px}}
input{{width:100%;padding:10px 12px;background:#111;border:1px solid #2a2a2a;
       border-radius:4px;color:#fff;font-size:14px;outline:none}}
input:focus{{border-color:#FFDB00}}
button{{width:100%;margin-top:16px;padding:10px;background:#FFDB00;color:#111;
        border:none;border-radius:4px;font-weight:700;font-size:14px;cursor:pointer}}
button:hover{{background:#ffe84d}}
.err{{margin-top:12px;color:#e74c3c;font-size:13px;text-align:center}}
</style>
</head>
<body>
<div class="box">
  <div class="logo">&#9632; IKEA Manager</div>
  <form method="post" action="/login">
    <label for="pw">Password</label>
    <input type="password" id="pw" name="password" autofocus placeholder="Enter password"/>
    {error}
    <button type="submit">Sign in</button>
  </form>
</div>
</body>
</html>"""


_PUBLIC_PATHS = {"/login", "/logout", "/static/install.sh", "/static/ikea-manager.tar.zst"}

class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        session = request.cookies.get(AUTH_COOKIE)
        if session and session in _valid_sessions:
            return await call_next(request)
        return RedirectResponse("/login", status_code=302)


IS_LINUX = sys.platform != "win32"
VNC_DISPLAY = ":99"
VNC_PORT = 5900
_vnc_procs: list = []  # running [xvfb_proc, x11vnc_proc] during a browser login


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                label                TEXT PRIMARY KEY,
                email                TEXT DEFAULT '',
                session_json         TEXT DEFAULT '{}',
                schedule_json        TEXT DEFAULT 'null',
                enabled              INTEGER DEFAULT 1,
                loyalty_points       INTEGER,
                loyalty_updated_at   TEXT,
                last_run_at          TEXT,
                last_run_status      TEXT,
                last_run_errors      TEXT DEFAULT '[]',
                session_refreshed_at TEXT,
                session_refresh_failed INTEGER DEFAULT 0
            )
        """)
        # Add columns to existing DBs that predate these fields
        for col_sql in [
            "ALTER TABLE accounts ADD COLUMN session_refreshed_at TEXT",
            "ALTER TABLE accounts ADD COLUMN session_refresh_failed INTEGER DEFAULT 0",
            "ALTER TABLE accounts ADD COLUMN points_delta INTEGER DEFAULT 0",
            "ALTER TABLE accounts ADD COLUMN points_delta_at TEXT",
            "ALTER TABLE accounts ADD COLUMN vouchers_json TEXT DEFAULT '[]'",
            "ALTER TABLE accounts ADD COLUMN vouchers_updated_at TEXT",
            "ALTER TABLE accounts ADD COLUMN vouchers_error TEXT",
        ]:
            try:
                await db.execute(col_sql)
                await db.commit()
            except Exception:
                pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS run_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT,
                started_at  TEXT,
                finished_at TEXT,
                status      TEXT,
                errors      TEXT DEFAULT '[]'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS points_log (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                label  TEXT NOT NULL,
                ts     TEXT NOT NULL,
                points INTEGER NOT NULL,
                delta  INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reward_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT NOT NULL,
                event_id    TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                value       INTEGER NOT NULL,
                datetime    TEXT NOT NULL,
                description TEXT,
                UNIQUE(label, event_id)
            )
        """)
        await db.commit()


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------

async def broadcast(message: dict) -> None:
    dead = []
    text = json.dumps(message)
    for ws in ws_connections:
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in ws_connections:
            ws_connections.remove(ws)


async def broadcast_log(label: str, level: str, message: str) -> None:
    await broadcast({
        "type": "log",
        "label": label,
        "level": level,
        "message": message,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


async def broadcast_status(label: str, status: str, errors: list, points: Any = None) -> None:
    await broadcast({
        "type": "status",
        "label": label,
        "status": status,
        "errors": errors,
        "points": points,
    })


# ---------------------------------------------------------------------------
# Loyalty points
# ---------------------------------------------------------------------------

def extract_jwt(session_json: str) -> str:
    """Extract the idp_reguser JWT from a session JSON string."""
    try:
        state = json.loads(session_json)
        for cookie in state.get("cookies", []):
            if cookie.get("name") == "idp_reguser":
                return cookie.get("value", "")
    except Exception:
        pass
    return ""


def get_jwt_expiry(session_json: str) -> datetime | None:
    """Decode the JWT payload and return its expiry as a UTC datetime, or None."""
    import base64
    jwt = extract_jwt(session_json)
    if not jwt:
        return None
    try:
        parts = jwt.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        if exp:
            return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        pass
    return None


async def fetch_loyalty_points(session_json: str) -> int | None:
    """Fetch loyalty points for a session. Returns None on failure."""
    jwt = extract_jwt(session_json)
    if not jwt:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                LOYALTY_API,
                headers={"Authorization": f"Bearer {jwt}", **LOYALTY_HEADERS},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("keysBalance")
    except Exception as exc:
        log.warning(f"Loyalty fetch failed: {exc}")
        return None


async def fetch_reward_history(session_json: str, label: str) -> list[dict]:
    """
    Fetch all reward transaction history from the IKEA API (handles pagination).
    Returns a list of raw item dicts.
    """
    jwt = extract_jwt(session_json)
    if not jwt:
        return []
    items: list[dict] = []
    url = f"{HISTORY_API}?_={int(_time.time() * 1000)}&schemaVersion=v2"
    for _page in range(20):   # safety limit
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {jwt}", **LOYALTY_HEADERS},
                )
            if resp.status_code != 200:
                log.warning(f"[{label}] History API returned {resp.status_code}")
                break
            data = resp.json()
            page_items = data.get("items", [])
            items.extend(page_items)
            next_url = data.get("nextPage")
            if not next_url:
                break
            url = next_url
        except Exception as exc:
            log.warning(f"[{label}] History fetch error (page {_page}): {exc}")
            break
    log.info(f"[{label}] Fetched {len(items)} reward history items from API")
    return items


async def save_reward_history(label: str, items: list[dict]) -> int:
    """
    Upsert reward history items into reward_history table.
    Returns the number of newly inserted rows.
    """
    if not items:
        return 0
    inserted = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for item in items:
            description = (
                (item.get("reason") or {}).get("details", {}).get("interactionType")
                or (item.get("reason") or {}).get("description", "")
                or ""
            )
            cur = await db.execute(
                """INSERT OR IGNORE INTO reward_history
                   (label, event_id, event_type, value, datetime, description)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    label,
                    item.get("id", ""),
                    item.get("eventType", ""),
                    item.get("value", 0),
                    item.get("datetime", ""),
                    description,
                ),
            )
            inserted += cur.rowcount
        await db.commit()
    return inserted


async def compute_24h_delta(label: str) -> int:
    """Sum of TokenAdded values in the last 24 h from the stored reward_history."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT COALESCE(SUM(value), 0) AS total
               FROM reward_history
               WHERE label = ? AND event_type = 'TokenAdded' AND datetime >= ?""",
            (label, cutoff),
        ) as cur:
            row = await cur.fetchone()
    return int(row["total"]) if row else 0


async def refresh_account_points(label: str) -> int | None:
    """
    Fetch and store loyalty points for a single account.
    When points change: fetches fresh history from the API and saves new items.
    Always recomputes points_delta as the 24 h sum from stored history.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT session_json, loyalty_points FROM accounts WHERE label = ?", (label,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        session_json = row["session_json"]
        old_points   = row["loyalty_points"]

    points = await fetch_loyalty_points(session_json)
    now = datetime.now(timezone.utc).isoformat()

    # Fetch fresh history from the API when the balance changed, on first run,
    # or when the account has no history stored yet.
    has_history = False
    if points is not None:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT 1 FROM reward_history WHERE label = ? LIMIT 1", (label,)
            ) as cur:
                has_history = await cur.fetchone() is not None

    if points is not None and (points != old_points or not has_history):
        history_items = await fetch_reward_history(session_json, label)
        new_rows = await save_reward_history(label, history_items)
        if new_rows:
            log.info(f"[{label}] Saved {new_rows} new reward history items")

    # Recompute 24 h delta from the (now-updated) local DB every time
    delta_24h = await compute_24h_delta(label)

    async with aiosqlite.connect(DB_PATH) as db:
        if points is None:
            await db.execute(
                "UPDATE accounts SET loyalty_updated_at = ? WHERE label = ?",
                (now, label),
            )
        else:
            await db.execute(
                """UPDATE accounts
                   SET loyalty_points = ?, loyalty_updated_at = ?,
                       points_delta = ?, points_delta_at = ?
                   WHERE label = ?""",
                (points, now, delta_24h, now, label),
            )
            # Keep legacy points_log entry when balance increases
            if old_points is not None and points > old_points:
                await db.execute(
                    "INSERT INTO points_log (label, ts, points, delta) VALUES (?, ?, ?, ?)",
                    (label, now, points, points - old_points),
                )
        await db.commit()

    return points


# ---------------------------------------------------------------------------
# Voucher fetching (ikeafamily.eu)
# ---------------------------------------------------------------------------

VOUCHER_URL = "https://www.ikeafamily.eu/Profil/Twoje-Kody-Rabatowe"

# ---------------------------------------------------------------------------
# Helpers for httpx-based voucher fetching
# ---------------------------------------------------------------------------

import re
import time as _time
from urllib.parse import urlparse as _urlparse, urljoin as _urljoin, parse_qs as _parse_qs, urlencode as _urlencode, urlunparse as _urlunparse
from html.parser import HTMLParser as _HTMLParser


class _FormParser(_HTMLParser):
    """Minimal HTML parser that extracts the first POST form and its hidden inputs."""
    def __init__(self):
        super().__init__()
        self.form_action: str = ""
        self.form_method: str = ""
        self.fields: dict[str, str] = {}
        self._in_form = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._in_form = True
            self.form_action = a.get("action", "")
            self.form_method = a.get("method", "get").lower()
        elif tag == "input" and self._in_form:
            name = a.get("name")
            if name:
                self.fields[name] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False


def _cookies_for_url(cookies_list: list[dict], url: str) -> dict[str, str]:
    """Return stored cookies applicable to *url*, skipping expired ones."""
    host = _urlparse(url).hostname or ""
    now = _time.time()
    result: dict[str, str] = {}
    for c in cookies_list:
        exp = c.get("expires", -1)
        if exp != -1 and exp < now:
            continue
        domain = c.get("domain", "").lstrip(".")
        if host == domain or host.endswith("." + domain):
            result[c["name"]] = c["value"]
    return result


def _parse_vouchers_html(html: str, label: str) -> list[dict]:
    """
    Parse voucher rows out of the ikeafamily.eu ASP.NET page HTML.
    Returns list of dicts: issued, expires, amount, location, active, code.
    Falls back gracefully if the table is absent (no vouchers).
    """
    today = datetime.now().date()

    # Locate the vouchers-table block
    tbl_start = html.find('class="vouchers-table"')
    if tbl_start == -1:
        tbl_start = html.find("vouchers-table")
    if tbl_start == -1:
        log.info(f"[{label}] no vouchers-table in HTML → 0 vouchers")
        return []

    # Grab everything from table open tag to </table>
    open_tag = html.rfind("<table", 0, tbl_start)
    if open_tag == -1:
        open_tag = tbl_start
    close_tag = html.find("</table>", tbl_start)
    table_html = html[open_tag: close_tag + 8] if close_tag != -1 else html[open_tag:]

    # Extract all voucher-row <tr> blocks
    rows = re.findall(r'<tr[^>]*class="[^"]*voucher-row[^"]*"[^>]*>(.*?)</tr>', table_html, re.S)
    log.info(f"[{label}] found {len(rows)} voucher rows (httpx)")

    vouchers = []
    for row_html in rows:
        # Extract all <td> text content (strip inner tags)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if len(cells) < 4:
            continue
        issued, expires, amount, location = cells[0], cells[1], cells[2], cells[3]
        try:
            exp_date = datetime.strptime(expires, "%d.%m.%Y").date()
            active = exp_date >= today
        except ValueError:
            active = False
        vouchers.append({
            "issued":   issued,
            "expires":  expires,
            "amount":   amount,
            "location": location,
            "code":     "",   # retrieved separately for active vouchers
            "active":   active,
        })
    return vouchers


async def _fetch_voucher_code_aspnet(
    client: httpx.AsyncClient,
    voucher_page_url: str,
    page_html: str,
    row_index: int,
    ifam_cookies: dict[str, str],
    label: str,
) -> str:
    """
    Trigger the ASP.NET UpdatePanel postback to reveal a voucher code.
    Returns the code string, or "" on failure.

    The button href contains the exact __EVENTTARGET, e.g.:
      javascript:__doPostBack('ctl00$MainContentHolder$rpVouchers$ctl00$btShowVoucher','')
      javascript:__doPostBack('ctl00$MainContentHolder$rpVouchers$ctl01$btShowVoucher','')
    We parse these directly instead of guessing from element IDs.
    """
    # Extract hidden ASP.NET form fields
    vs  = re.search(r'id="__VIEWSTATE"[^>]*value="([^"]*)"', page_html)
    vsg = re.search(r'id="__VIEWSTATEGENERATOR"[^>]*value="([^"]*)"', page_html)
    ev  = re.search(r'id="__EVENTVALIDATION"[^>]*value="([^"]*)"', page_html)

    # Extract __EVENTTARGET values for btShowVoucher (non-mobile) from href attributes.
    # The raw HTML uses HTML entities: href="javascript:__doPostBack(&#39;ctl00$...&#39;,&#39;&#39;)"
    # so we match &#39; (or a literal ' as fallback) as the quote character.
    _Q = r"(?:&#39;|')"
    targets = re.findall(r"__doPostBack\(" + _Q + r"([^'&<>]*\$btShowVoucher)" + _Q, page_html)
    targets = [t for t in targets if "Mobile" not in t]

    if not targets or row_index >= len(targets):
        log.debug(f"[{label}] No btShowVoucher targets found for row {row_index} "
                  f"(found {len(targets)} total)")
        return ""

    target = targets[row_index]
    log.debug(f"[{label}] UpdatePanel postback target: {target}")

    payload = {
        "__EVENTTARGET":        target,
        "__EVENTARGUMENT":      "",
        "__VIEWSTATE":          vs.group(1) if vs else "",
        "__VIEWSTATEGENERATOR": vsg.group(1) if vsg else "",
        "__EVENTVALIDATION":    ev.group(1) if ev else "",
        "__ASYNCPOST":          "true",
        "dummyUp":              "",
    }
    try:
        resp = await client.post(
            voucher_page_url,
            data=payload,
            cookies=ifam_cookies,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-MicrosoftAjax": "Delta=true",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": voucher_page_url,
            },
            timeout=10,
        )
        delta = resp.text

        # Primary: "Kod rabatowy:" label followed by code in the next col-md-7 div
        # Structure: <div class="col-md-5">Kod rabatowy:</div>
        #            <div class="col-md-7">\n  5635LNXCQGV35JRU\n</div>
        m = re.search(
            r'Kod rabatowy:.*?<div[^>]*class="[^"]*col-md-7[^"]*"[^>]*>\s*([A-Z0-9]{6,})\s*</div>',
            delta, re.S | re.I,
        )
        if m:
            return m.group(1).strip()

        # Fallback: any 12–24 char uppercase alphanumeric token in the delta
        # (real codes are ~16 chars; __VIEWSTATEGENERATOR is only 8 hex chars)
        m = re.search(r'\b([A-Z0-9]{12,24})\b', delta)
        if m:
            return m.group(1)

    except Exception as exc:
        log.debug(f"[{label}] UpdatePanel code fetch failed: {exc}")
    return ""


async def _try_silent_ifam_reauth(
    label: str,
    cookies_list: list[dict],
    authorize_url: str,
) -> dict[str, str] | None:
    """
    Try to silently re-authenticate for ikeafamily.eu without browser interaction.

    Flow:
      1. Exchange the stored ikea.com rtoken for a fresh id_token (+ rotated rtoken).
      2. Save the rotated rtoken so future ikea.com session refreshes keep working.
      3. Replay the ikeafamily.eu /authorize URL with prompt=none + id_token_hint.
      4. If Auth0 returns a form_post (code), POST it to the ikeafamily callback.
      5. Follow the redirect, collect all Set-Cookie headers.
      6. Return the fresh ikeafamily.eu session cookies on success, None on failure.
    """
    HEADERS = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    }

    # --- Step 1: get the rtoken from stored cookies ---
    rtoken = next(
        (c["value"] for c in cookies_list
         if c.get("name") == "rtoken" and "accounts.ikea.com" in c.get("domain", "")),
        None,
    )
    if not rtoken:
        log.info(f"[{label}] silent_ifam_reauth: no rtoken in stored session")
        return None

    # --- Step 2: exchange rtoken → id_token (ikea.com client) ---
    try:
        async with httpx.AsyncClient(timeout=15) as _tc:
            _tr = await _tc.post(
                "https://pl.accounts.ikea.com/oauth/token",
                json={
                    "grant_type":    "refresh_token",
                    "refresh_token": rtoken,
                    "client_id":     "f2LIjaqFXyKIqZG4LomjnpmJZOENncsc",
                    "scope":         "openid profile email",
                },
                headers={"Content-Type": "application/json"},
            )
        if _tr.status_code != 200:
            log.info(f"[{label}] silent_ifam_reauth: token endpoint {_tr.status_code}: {_tr.text[:200]}")
            return None
        _tdata      = _tr.json()
        id_token    = _tdata.get("id_token")
        new_rtoken  = _tdata.get("refresh_token")
        if not id_token:
            log.info(f"[{label}] silent_ifam_reauth: no id_token in token response")
            return None
        log.info(f"[{label}] silent_ifam_reauth: got id_token, attempting prompt=none authorize")

        # Persist the rotated refresh token so future ikea.com session refreshes work
        if new_rtoken:
            async with aiosqlite.connect(DB_PATH) as _db:
                _db.row_factory = aiosqlite.Row
                async with _db.execute(
                    "SELECT session_json FROM accounts WHERE label = ?", (label,)
                ) as _cur:
                    _row = await _cur.fetchone()
            if _row:
                _sd = json.loads(_row["session_json"])
                _new_cks = []
                for _c in _sd.get("cookies", []):
                    if _c.get("name") == "rtoken" and "accounts.ikea.com" in _c.get("domain", ""):
                        _c = dict(_c)
                        _c["value"] = new_rtoken
                    _new_cks.append(_c)
                _sd["cookies"] = _new_cks
                _new_sj = json.dumps(_sd)
                async with aiosqlite.connect(DB_PATH) as _db2:
                    await _db2.execute(
                        "UPDATE accounts SET session_json = ? WHERE label = ?", (_new_sj, label)
                    )
                    await _db2.commit()
                # Also update local cookies_list in-place so get_cookies() uses the new value
                for _c in cookies_list:
                    if _c.get("name") == "rtoken" and "accounts.ikea.com" in _c.get("domain", ""):
                        _c["value"] = new_rtoken

    except Exception as _exc:
        log.info(f"[{label}] silent_ifam_reauth: token request failed: {_exc}")
        return None

    # --- Step 3: build prompt=none authorize URL ---
    _parsed = _urlparse(authorize_url)
    _params = {k: v[0] for k, v in _parse_qs(_parsed.query).items()}
    _params["prompt"]        = "none"
    _params["id_token_hint"] = id_token
    silent_url = _urlunparse(_parsed._replace(query=_urlencode(_params)))

    # Include any auth0 session cookies from stored session (may help if not fully expired)
    auth0_cks: dict[str, str] = {}
    for _c in cookies_list:
        if "accounts.ikea.com" in _c.get("domain", ""):
            auth0_cks[_c["name"]] = _c["value"]

    fresh: dict[str, str] = {}

    def _save_fresh(resp: httpx.Response) -> None:
        for _sc in resp.headers.get_list("set-cookie"):
            _kv = _sc.split(";")[0].strip()
            if "=" in _kv:
                _n, _, _v = _kv.partition("=")
                fresh[_n.strip()] = _v.strip()

    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(20.0),
            headers=HEADERS,
        ) as client:

            # --- Step 4: GET /authorize with prompt=none + id_token_hint ---
            resp = await client.get(silent_url, cookies=auth0_cks)
            _save_fresh(resp)

            if resp.status_code in (301, 302, 303, 307, 308):
                _loc = resp.headers.get("location", "")
                log.info(f"[{label}] silent_ifam_reauth: authorize redirect → {_loc[:100]}")
                # Any redirect here means silent auth failed (login_required, etc.)
                return None

            if resp.status_code != 200:
                log.info(f"[{label}] silent_ifam_reauth: authorize returned {resp.status_code}")
                return None

            # --- Step 5: parse form_post ---
            _fp = _FormParser()
            _fp.feed(resp.text[:10_000])
            if not (_fp.form_method == "post" and _fp.form_action):
                log.info(f"[{label}] silent_ifam_reauth: no form_post in authorize response")
                return None

            _action = _fp.form_action if _fp.form_action.startswith("http") \
                      else _urljoin(str(resp.url), _fp.form_action)

            if "ikeafamily.eu" not in _action:
                log.info(f"[{label}] silent_ifam_reauth: form action not ikeafamily.eu: {_action[:80]}")
                return None

            log.info(f"[{label}] silent_ifam_reauth: form_post → {_action[:60]}")

            # --- Step 6: POST to ikeafamily callback, follow redirects ---
            _cb_cookies: dict[str, str] = {}
            _cb_url  = _action
            _cb_data: dict | None = _fp.fields

            for _ in range(6):
                if _cb_data is not None:
                    _cr = await client.post(
                        _cb_url, data=_cb_data, cookies=_cb_cookies,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                    _cb_data = None
                else:
                    _cr = await client.get(_cb_url, cookies=_cb_cookies)
                _save_fresh(_cr)
                _cb_cookies.update(fresh)

                if _cr.status_code in (301, 302, 303, 307, 308):
                    _loc = _cr.headers.get("location", "")
                    if _loc.startswith("/"):
                        _cb_url = f"https://www.ikeafamily.eu{_loc}"
                    elif not _loc.startswith("http"):
                        _cb_url = _urljoin(_cb_url, _loc)
                    else:
                        _cb_url = _loc
                    # Once redirected to /Profil we have the session — no need to load it here
                    if "/Profil" in _cb_url or "ikeafamily.eu" not in _cb_url:
                        break
                    continue
                break  # 200 or error

            if fresh:
                log.info(f"[{label}] silent_ifam_reauth: success! cookies: {list(fresh.keys())}")
                return fresh

            log.info(f"[{label}] silent_ifam_reauth: callback set no cookies")
            return None

    except Exception as _exc:
        log.info(f"[{label}] silent_ifam_reauth failed: {_exc}")
        return None


async def _merge_runtime_into_session(
    label: str,
    cookies_list: list[dict],
    runtime: dict[str, dict[str, str]],
) -> None:
    """
    Persist cookies collected during the httpx redirect chain back into the
    stored session_json for this account.

    Why this matters:
      - Auth0 uses *rolling sessions*: each successful /authorize call issues a
        new auth0 / auth0_compat cookie.  If we don't save the updated value,
        the next voucher fetch sends the old (now-invalidated) cookie and lands
        on the login page.
      - ikeafamily.eu sets its own session cookie during the OAuth callback.
        Saving it lets subsequent fetches skip the OAuth redirect entirely
        (direct GET to the voucher page).
    """
    if not runtime:
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT session_json FROM accounts WHERE label = ?", (label,)
            ) as cur:
                _row = await cur.fetchone()
        if not _row:
            return

        sd = json.loads(_row["session_json"])
        changed = False

        for rt_host, cks in runtime.items():
            for name, value in cks.items():
                # Try to update an existing stored cookie whose domain covers rt_host
                matched = False
                for c in sd.get("cookies", []):
                    if c.get("name") != name:
                        continue
                    c_dom = c.get("domain", "").lstrip(".")
                    if rt_host == c_dom or rt_host.endswith("." + c_dom):
                        if c.get("value") != value:
                            c["value"] = value
                            changed = True
                        matched = True
                        break
                if not matched:
                    # New cookie (e.g. ikeafamily.eu session) — add it
                    sd.setdefault("cookies", []).append({
                        "name":     name,
                        "value":    value,
                        "domain":   "." + rt_host,
                        "path":     "/",
                        "sameSite": "Lax",
                        "expires":  -1,
                    })
                    changed = True

        if changed:
            new_sj = json.dumps(sd)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE accounts SET session_json = ? WHERE label = ?", (new_sj, label)
                )
                await db.commit()
            # Keep local cookies_list in sync so the same call can reuse fresh values
            for rt_host, cks in runtime.items():
                for name, value in cks.items():
                    for c in cookies_list:
                        c_dom = c.get("domain", "").lstrip(".")
                        if c.get("name") == name and (rt_host == c_dom or rt_host.endswith("." + c_dom)):
                            c["value"] = value
            log.debug(f"[{label}] session cookies updated from runtime: "
                      f"{[(h, list(v.keys())) for h, v in runtime.items()]}")
    except Exception as exc:
        log.debug(f"[{label}] _merge_runtime_into_session error: {exc}")


async def fetch_account_vouchers(label: str) -> list[dict]:
    """
    Fetch ikeafamily.eu vouchers using httpx (no browser).
    Completes the OAuth redirect chain with stored cookies, then parses
    the server-rendered HTML.  Finishes in < 5 s on success, < 2 s on
    auth failure — vs 85+ s with the old Playwright approach.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT session_json FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()
    if not row:
        return []

    cookies_list: list[dict] = json.loads(row["session_json"] or "{}").get("cookies", [])

    HEADERS = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    }

    # Runtime cookies set by servers during the redirect chain
    runtime: dict[str, dict[str, str]] = {}

    def get_cookies(url: str) -> dict[str, str]:
        c = _cookies_for_url(cookies_list, url)
        host = _urlparse(url).hostname or ""
        for dom, cks in runtime.items():
            if host == dom or host.endswith("." + dom):
                c.update(cks)
        return c

    def save_cookies(resp: httpx.Response) -> None:
        host = _urlparse(str(resp.url)).hostname or ""
        for sc in resp.headers.get_list("set-cookie"):
            kv = sc.split(";")[0].strip()
            if "=" in kv:
                n, _, v = kv.partition("=")
                runtime.setdefault(host, {})[n.strip()] = v.strip()

    async def _mark_error(err: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE accounts SET vouchers_error = ?, vouchers_updated_at = ? WHERE label = ?",
                (err, now, label),
            )
            await db.commit()

    vouchers: list[dict] = []
    voucher_page_url = VOUCHER_URL
    voucher_page_html = ""

    # Track Auth0 /authorize URL seen during redirects — needed for silent reauth
    _auth0_authorize_url: str = ""
    _silent_reauth_done: bool = False

    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(20.0),
            headers=HEADERS,
        ) as client:
            url, method, fdata = VOUCHER_URL, "GET", None

            for step in range(18):
                cookies = get_cookies(url)
                if method == "POST":
                    resp = await client.post(
                        url, data=fdata, cookies=cookies,
                        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                    )
                else:
                    resp = await client.get(url, cookies=cookies)
                save_cookies(resp)

                log.debug(f"[{label}] httpx step={step} {resp.status_code} {url[:80]}")

                # --- redirect ---
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location", "")
                    if loc.startswith("/"):
                        p = _urlparse(url)
                        loc = f"{p.scheme}://{p.netloc}{loc}"
                    elif not loc.startswith("http"):
                        loc = _urljoin(url, loc)
                    # Capture the Auth0 /authorize URL (has valid state/nonce from ikeafamily.eu)
                    if "accounts.ikea.com" in loc and "/authorize" in loc:
                        _auth0_authorize_url = loc
                    url = loc
                    method, fdata = "GET", None
                    continue

                if resp.status_code != 200:
                    log.warning(f"[{label}] httpx unexpected {resp.status_code} at {url}")
                    break

                # --- Auth0 / pl.accounts.ikea.com ---
                if "accounts.ikea.com" in url or "auth0.com" in url:
                    fp = _FormParser()
                    fp.feed(resp.text[:10_000])
                    if fp.form_method == "post" and fp.form_action:
                        action = fp.form_action if fp.form_action.startswith("http") \
                                 else _urljoin(url, fp.form_action)
                        log.info(f"[{label}] OAuth form_post → {action[:60]}")
                        url, method, fdata = action, "POST", fp.fields
                        continue
                    # No auto-submit form → login page shown, auth0 session expired.
                    # Attempt a silent reauth via id_token_hint before giving up.
                    log.warning(f"[{label}] ikeafamily.eu auth0 session expired (login page at {url[:80]})")
                    if _auth0_authorize_url and not _silent_reauth_done:
                        _silent_reauth_done = True
                        log.info(f"[{label}] Attempting silent ikeafamily.eu reauth via id_token_hint")
                        _fresh = await _try_silent_ifam_reauth(label, cookies_list, _auth0_authorize_url)
                        if _fresh:
                            # Inject fresh ikeafamily.eu session cookies and retry
                            runtime["www.ikeafamily.eu"] = _fresh
                            url, method, fdata = VOUCHER_URL, "GET", None
                            continue
                        log.info(f"[{label}] Silent ikeafamily.eu reauth failed — marking session_expired")
                    await _mark_error("session_expired")
                    return []

                # --- Landed on voucher page ---
                if "ikeafamily.eu/Profil" in url:
                    voucher_page_url = url
                    voucher_page_html = resp.text
                    ifam_cookies = get_cookies(url)
                    vouchers = _parse_vouchers_html(resp.text, label)

                    # Fetch codes for active vouchers via UpdatePanel postback
                    for i, v in enumerate(vouchers):
                        if v["active"]:
                            code = await _fetch_voucher_code_aspnet(
                                client, voucher_page_url, voucher_page_html,
                                i, ifam_cookies, label,
                            )
                            v["code"] = code
                    break

                log.warning(f"[{label}] httpx unexpected final URL: {url}")
                break

    except Exception as exc:
        log.error(f"[{label}] fetch_account_vouchers error: {exc}")

    # Save any updated session/auth0 cookies back to DB so the next fetch
    # can reuse them (auth0 rolling sessions invalidate the old cookie value)
    await _merge_runtime_into_session(label, cookies_list, runtime)

    # Persist vouchers to DB (clear any previous error on success)
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET vouchers_json = ?, vouchers_updated_at = ?, vouchers_error = NULL WHERE label = ?",
            (json.dumps(vouchers), now, label),
        )
        await db.commit()

    return vouchers


# ---------------------------------------------------------------------------
# Session refresh
# ---------------------------------------------------------------------------

SESSION_REFRESH_INTERVAL = 3600    # check every hour
SESSION_REFRESH_THRESHOLD_SECS = 3 * 3600  # refresh if expiring within 3 hours


async def _refresh_via_refresh_token(
    session_data: dict, label: str
) -> tuple[str, str] | None:
    """
    Exchange the stored Auth0 refresh token (rtoken cookie) for a fresh
    access_token + refresh_token via POST /oauth/token.
    Returns (access_token, new_refresh_token) or None on failure.
    idp_reguser must be set to access_token (NOT id_token).
    Auth0 rotates refresh tokens, so the new_refresh_token must replace rtoken.
    """
    import http.client, ssl

    rtoken = next(
        (c["value"] for c in session_data.get("cookies", [])
         if c.get("name") == "rtoken" and "accounts.ikea.com" in c.get("domain", "")),
        None,
    )
    if not rtoken:
        log.warning(f"[{label}] No rtoken cookie in stored session")
        return None

    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": rtoken,
        "client_id": "f2LIjaqFXyKIqZG4LomjnpmJZOENncsc",
        "redirect_uri": "https://www.ikea.com/pl/pl/profile/login/",
        "scope": "openid profile email",
    }).encode()

    def _fetch():
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection("pl.accounts.ikea.com", context=ctx, timeout=20)
        conn.request("POST", "/oauth/token", body=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    try:
        status, data = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        if status != 200:
            log.warning(f"[{label}] Auth0 /oauth/token returned {status}: {data.get('error_description', data)}")
            return None
        access_token = data.get("access_token")
        new_rtoken = data.get("refresh_token")
        if not access_token or not new_rtoken:
            log.warning(f"[{label}] Auth0 response missing access_token or refresh_token")
            return None
        return access_token, new_rtoken
    except Exception as exc:
        log.warning(f"[{label}] Auth0 /oauth/token request failed: {exc}")
        return None


async def _silent_auth_with_audience(session_data: dict, label: str) -> str | None:
    """
    Fallback refresh: Auth0 prompt=none silent auth with explicit audience.
    Returns a fresh RS256 access_token (~2h lifetime) or None on failure.
    Requires auth0 / auth0_compat session cookies on pl.accounts.ikea.com.
    """
    import secrets, urllib.request, urllib.error

    cookies: dict[str, str] = {}
    for c in session_data.get("cookies", []):
        if "accounts.ikea.com" in c.get("domain", ""):
            cookies[c["name"]] = c["value"]

    if not (cookies.get("auth0") or cookies.get("auth0_compat")):
        log.warning(f"[{label}] No Auth0 session cookies — cannot do silent auth")
        return None

    nonce = secrets.token_hex(16)
    url = (
        "https://pl.accounts.ikea.com/authorize"
        "?response_type=token%20id_token"
        "&client_id=f2LIjaqFXyKIqZG4LomjnpmJZOENncsc"
        "&redirect_uri=https%3A%2F%2Fwww.ikea.com%2Fpl%2Fpl%2Fprofile%2Flogin%2F"
        "&prompt=none"
        "&scope=openid%20profile%20email"
        "&audience=https%3A%2F%2Fretail.api.ikea.com"
        f"&nonce={nonce}"
    )
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    class _Cap(urllib.request.HTTPRedirectHandler):
        access_token: str | None = None
        auth0_error: str | None = None

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if "#" in newurl:
                fragment = newurl.split("#", 1)[1]
                params = dict(p.split("=", 1) for p in fragment.split("&") if "=" in p)
                self.access_token = params.get("access_token")
                err = params.get("error")
                if err:
                    self.auth0_error = f"{err}: {params.get('error_description', '')}"
                raise urllib.error.HTTPError(newurl, code, "captured", headers, fp)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    handler = _Cap()

    def _fetch() -> str | None:
        req = urllib.request.Request(url, headers={
            "Cookie": cookie_header,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pl-PL,pl;q=0.9",
        })
        try:
            urllib.request.build_opener(handler).open(req, timeout=20)
        except urllib.error.HTTPError:
            pass
        except Exception as exc:
            log.warning(f"[{label}] Silent auth network error: {exc}")
            return None
        if handler.auth0_error:
            log.warning(f"[{label}] Auth0 silent auth error: {handler.auth0_error}")
            return None
        return handler.access_token

    return await asyncio.get_event_loop().run_in_executor(None, _fetch)


async def refresh_session_via_browser(label: str) -> bool:
    """
    Refresh idp_reguser (the Auth0 access_token) with no browser.
    Strategy 1: rtoken cookie → POST /oauth/token  (24h token, rotates rtoken)
    Strategy 2: auth0 session cookies → prompt=none silent auth (2h token)
    Falls back gracefully and marks refresh_failed only when both fail.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT session_json FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()
    if not row:
        return False

    session_data = json.loads(row["session_json"])

    try:
        # --- Strategy 1: refresh token grant (24h) ---
        new_rtoken: str | None = None
        rtoken_result = await _refresh_via_refresh_token(session_data, label)
        if rtoken_result:
            access_token, new_rtoken = rtoken_result
            method = "refresh_token grant (24h)"
        else:
            # --- Strategy 2: silent auth with audience (2h) ---
            access_token = await _silent_auth_with_audience(session_data, label)
            method = "silent auth (2h)"

        if not access_token:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE accounts SET session_refresh_failed = 1 WHERE label = ?", (label,)
                )
                await db.commit()
            return False

        # Patch idp_reguser and optionally rtoken in stored cookies
        new_cookies = []
        for c in session_data.get("cookies", []):
            name = c.get("name")
            if name == "idp_reguser" and ".ikea.com" in c.get("domain", ""):
                c = dict(c)
                c["value"] = access_token
            elif name == "rtoken" and new_rtoken and "accounts.ikea.com" in c.get("domain", ""):
                c = dict(c)
                c["value"] = new_rtoken
            new_cookies.append(c)
        session_data["cookies"] = new_cookies

        fresh_json = json.dumps(session_data)

        new_expiry = get_jwt_expiry(fresh_json)
        if new_expiry is None:
            log.warning(f"[{label}] Could not decode expiry from new access_token — saving anyway")

        session_file = SESSIONS_DIR / f"{label}.json"
        session_file.write_text(fresh_json, encoding="utf-8")

        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE accounts SET session_json = ?, session_refreshed_at = ?, "
                "session_refresh_failed = 0 WHERE label = ?",
                (fresh_json, now, label),
            )
            await db.commit()

        expiry_str = new_expiry.isoformat() if new_expiry else "unknown"
        log.info(f"[{label}] Session refreshed via {method} — new expiry {expiry_str}")
        return True

    except Exception as exc:
        log.error(f"[{label}] Session refresh failed: {exc}")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE accounts SET session_refresh_failed = 1 WHERE label = ?", (label,)
            )
            await db.commit()
        return False


async def session_refresh_loop() -> None:
    """Every 4 hours, refresh sessions that will expire within 8 hours."""
    await asyncio.sleep(60)  # short initial delay
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT label, session_json, session_refresh_failed FROM accounts"
                ) as cur:
                    rows = await cur.fetchall()

            threshold = datetime.now(timezone.utc).timestamp() + SESSION_REFRESH_THRESHOLD_SECS
            for row in rows:
                label = row["label"]
                # Skip accounts where refresh has already failed — user must re-import cookies
                if row["session_refresh_failed"]:
                    continue
                expiry = get_jwt_expiry(row["session_json"])
                if expiry is None:
                    continue
                if expiry.timestamp() < threshold:
                    log.info(f"[{label}] Session expires {expiry.isoformat()} — auto-refreshing")
                    await refresh_session_via_browser(label)
                    await broadcast({"type": "accounts_update"})

        except Exception as exc:
            log.error(f"Session refresh loop error: {exc}")

        await asyncio.sleep(SESSION_REFRESH_INTERVAL)


# ---------------------------------------------------------------------------
# Account runner
# ---------------------------------------------------------------------------

ALL_TASKS = ["favourites_list", "planning_project", "join_event"]


async def run_account_tasks(label: str, tasks: list[str] | None = None) -> None:
    tasks = [t for t in (tasks or ALL_TASKS) if t in ALL_TASKS]
    """Run all automation tasks for an account."""
    if label in running_accounts:
        log.warning(f"[{label}] Already running — skipping")
        return

    running_accounts.add(label)
    started_at = datetime.now(timezone.utc).isoformat()
    errors: list[dict] = []

    await broadcast_status(label, "running", [])
    await broadcast_log(label, "info", f"Starting automation run for '{label}'")

    # Load session from DB and write to disk
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT session_json FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()

    if not row:
        await broadcast_log(label, "error", f"Account '{label}' not found")
        running_accounts.discard(label)
        return

    session_file = SESSIONS_DIR / f"{label}.json"
    session_file.write_text(row["session_json"], encoding="utf-8")

    try:
        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth

        async with async_playwright() as playwright:
            launch_args = ["--disable-blink-features=AutomationControlled"]
            if IS_LINUX:
                launch_args += ["--no-sandbox", "--disable-dev-shm-usage"]
            browser = await playwright.chromium.launch(
                headless=IS_LINUX,
                channel="chrome",
                args=launch_args,
            )
            context = await browser.new_context(
                storage_state=str(session_file),
                locale="pl-PL",
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()
            page.set_default_timeout(90_000)
            await Stealth().apply_stealth_async(page)

            # Navigate to IKEA Poland homepage
            await broadcast_log(label, "info", "Navigating to IKEA Poland homepage…")
            await page.goto("https://www.ikea.com/pl/pl/", wait_until="domcontentloaded")

            # Accept cookie banner
            try:
                await page.get_by_role("button", name="Akceptuj wszystkie").click(timeout=5000)
                await broadcast_log(label, "info", "Accepted cookie banner")
            except Exception:
                pass

            total = len(tasks)

            # -----------------------------------------------------------------
            # Task: favourites_list
            # -----------------------------------------------------------------
            if "favourites_list" in tasks:
                n = tasks.index("favourites_list") + 1
                await broadcast_log(label, "info", f"Task {n}/{total}: Creating favourites list…")
                try:
                    list_name = f"Ulubione {datetime.now().strftime('%Y-%m-%d')}"
                    result = await create_favourites_list_via_page(page, list_name)
                    await broadcast_log(label, "info", f"Favourites list created: '{result['name']}' (id: {result['listId']})")
                except Exception as exc:
                    errors.append({"task": "favourites_list", "error": str(exc)})
                    await broadcast_log(label, "error", f"Task favourites_list failed: {exc}")

            # -----------------------------------------------------------------
            # Task: planning_project
            # -----------------------------------------------------------------
            if "planning_project" in tasks:
                n = tasks.index("planning_project") + 1
                await broadcast_log(label, "info", f"Task {n}/{total}: Opening Kreativ planning editor…")
                try:
                    await page.goto(
                        "https://www.ikea.com/pl/pl/home-design/room/"
                        "#b3e3c180-dd6b-4e49-8ffe-1fe2e5c29810/0943b0b9-198c-4e74-b287-171db3f4ad35",
                        wait_until="domcontentloaded",
                    )
                    await page.wait_for_selector(
                        "[data-testid='preview-header-save-btn']",
                        state="attached",
                        timeout=90000,
                    )
                    await asyncio.sleep(2)
                    await page.evaluate(
                        "document.querySelector(\"[data-testid='starter-set-sidebar-continue-button']\")?.click()"
                    )
                    await asyncio.sleep(1)
                    await page.wait_for_selector(
                        ".ProductSummaryCard__thumbnailWrapper",
                        state="attached",
                        timeout=30000,
                    )
                    await page.locator(".ProductSummaryCard__thumbnailWrapper").first.click()
                    await asyncio.sleep(4)
                    await page.locator("[data-testid='preview-header-save-btn']").click()
                    await asyncio.sleep(1)
                    await page.get_by_role("button", name="Zapisz", exact=True).first.click()
                    await page.wait_for_selector("text=Twój projekt został zapisany", timeout=15000)
                    await broadcast_log(label, "info", "Planning project saved successfully")
                except Exception as exc:
                    errors.append({"task": "planning_project", "error": str(exc)})
                    await broadcast_log(label, "error", f"Task planning_project failed: {exc}")

            # -----------------------------------------------------------------
            # Task: join_event
            # -----------------------------------------------------------------
            if "join_event" in tasks:
                n = tasks.index("join_event") + 1
                await broadcast_log(label, "info", f"Task {n}/{total}: Joining IKEA event…")
                try:
                    result = await asyncio.to_thread(join_and_confirm_event, label, EVENT_ID)
                    await broadcast_log(
                        label, "info",
                        f"Event joined: {result['event_id']} — timeslot {result['start']} -> {result['end']}"
                    )
                except Exception as exc:
                    errors.append({"task": "join_event", "error": str(exc)})
                    await broadcast_log(label, "error", f"Task join_event failed: {exc}")

            await context.close()
            await browser.close()

    except Exception as exc:
        errors.append({"task": "browser", "error": str(exc)})
        await broadcast_log(label, "error", f"Browser error: {exc}")

    # Determine final status
    if not errors:
        final_status = "ok"
    elif len(errors) == len(tasks):
        final_status = "failed"
    else:
        final_status = "partial"

    finished_at = datetime.now(timezone.utc).isoformat()

    # Refresh loyalty points
    points = await refresh_account_points(label)

    # Update DB
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE accounts SET
               last_run_at = ?, last_run_status = ?, last_run_errors = ?
               WHERE label = ?""",
            (finished_at, final_status, json.dumps(errors), label),
        )
        await db.execute(
            """INSERT INTO run_logs (label, started_at, finished_at, status, errors)
               VALUES (?, ?, ?, ?, ?)""",
            (label, started_at, finished_at, final_status, json.dumps(errors)),
        )
        await db.commit()

    running_accounts.discard(label)

    await broadcast_log(label, "info" if final_status == "ok" else "error",
                        f"Run finished — status: {final_status}, errors: {len(errors)}")
    await broadcast_status(label, final_status, errors, points)
    await broadcast({"type": "accounts_update"})


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------

async def scheduler_loop() -> None:
    """Check every 60 s if any account should run based on its schedule."""
    while True:
        await asyncio.sleep(SCHEDULER_INTERVAL)
        try:
            now = datetime.now()
            weekday = now.weekday()   # 0=Mon, 6=Sun
            current_hour = now.hour
            current_minute = now.minute

            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT label, schedule_json FROM accounts WHERE enabled = 1 AND schedule_json != 'null'"
                ) as cur:
                    rows = await cur.fetchall()

            for row in rows:
                label = row["label"]
                if label in running_accounts:
                    continue
                try:
                    schedule = json.loads(row["schedule_json"])
                    if not schedule:
                        continue
                    days = schedule.get("days", [])
                    hour = schedule.get("hour", 0)
                    minute = schedule.get("minute", 0)
                    if weekday in days and current_hour == hour and current_minute == minute:
                        log.info(f"Scheduler: triggering '{label}'")
                        asyncio.create_task(run_account_tasks(label))
                except Exception as exc:
                    log.error(f"Scheduler error for '{label}': {exc}")
        except Exception as exc:
            log.error(f"Scheduler loop error: {exc}")


async def loyalty_loop() -> None:
    """Refresh loyalty points for all accounts every 2 hours."""
    await asyncio.sleep(30)  # short initial delay
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT label FROM accounts") as cur:
                    rows = await cur.fetchall()

            for row in rows:
                label = row["label"]
                try:
                    points = await refresh_account_points(label)
                    log.info(f"[{label}] Loyalty points refreshed: {points}")
                except Exception as exc:
                    log.warning(f"[{label}] Loyalty refresh failed: {exc}")

            await broadcast({"type": "accounts_update"})
        except Exception as exc:
            log.error(f"Loyalty loop error: {exc}")

        await asyncio.sleep(LOYALTY_REFRESH_INTERVAL)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(loyalty_loop())
    asyncio.create_task(session_refresh_loop())
    yield


app = FastAPI(title="IKEA Manager", lifespan=lifespan)
app.add_middleware(_AuthMiddleware)

# Serve static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Serve noVNC files (Linux only — installed via apt)
_novnc_dir = Path("/usr/share/novnc")
if _novnc_dir.exists():
    app.mount("/novnc", StaticFiles(directory=str(_novnc_dir)), name="novnc")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AddAccountRequest(BaseModel):
    label: str
    cookies_json: str


class RunRequest(BaseModel):
    tasks: list[str] | None = None  # None = run all


class ScheduleRequest(BaseModel):
    days: list[int] | None = None
    hour: int = 0
    minute: int = 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/login", include_in_schema=False)
async def login_get():
    return HTMLResponse(_LOGIN_HTML.format(error=""))


@app.post("/login", include_in_schema=False)
async def login_post(password: str = Form(...)):
    if password == AUTH_PASSWORD:
        token = secrets.token_hex(32)
        _valid_sessions.add(token)
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="lax", max_age=7 * 86400)
        return resp
    return HTMLResponse(
        _LOGIN_HTML.format(error='<p class="err">Wrong password</p>'),
        status_code=401,
    )


@app.get("/logout", include_in_schema=False)
async def logout(request: StarletteRequest):
    _valid_sessions.discard(request.cookies.get(AUTH_COOKIE, ""))
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(AUTH_COOKIE)
    return resp


@app.get("/")
async def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/accounts")
async def list_accounts():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT label, email, session_json, schedule_json, enabled, loyalty_points, "
            "loyalty_updated_at, last_run_at, last_run_status, last_run_errors, "
            "session_refreshed_at, session_refresh_failed, points_delta, points_delta_at, "
            "vouchers_json, vouchers_updated_at, vouchers_error FROM accounts"
        ) as cur:
            rows = await cur.fetchall()

    result = []
    for row in rows:
        session_json = row["session_json"] or "{}"
        expiry = get_jwt_expiry(session_json)
        result.append({
            "label": row["label"],
            "email": row["email"],
            "schedule": json.loads(row["schedule_json"]) if row["schedule_json"] else None,
            "enabled": bool(row["enabled"]),
            "loyalty_points": row["loyalty_points"],
            "loyalty_updated_at": row["loyalty_updated_at"],
            "last_run_at": row["last_run_at"],
            "last_run_status": row["last_run_status"],
            "last_run_errors": json.loads(row["last_run_errors"] or "[]"),
            "running": row["label"] in running_accounts,
            "session_expires_at": expiry.isoformat() if expiry else None,
            "session_refreshed_at": row["session_refreshed_at"],
            "session_refresh_failed": bool(row["session_refresh_failed"]),
            "points_delta": row["points_delta"] or 0,
            "points_delta_at": row["points_delta_at"],
            "vouchers": json.loads(row["vouchers_json"] or "[]"),
            "vouchers_updated_at": row["vouchers_updated_at"],
            "vouchers_error": row["vouchers_error"],
        })
    return result


@app.post("/api/accounts", status_code=201)
async def add_account(req: AddAccountRequest):
    label = req.label.strip()
    if not label:
        raise HTTPException(400, "Label is required")

    # Parse cookies
    try:
        raw_cookies = json.loads(req.cookies_json)
        if not isinstance(raw_cookies, list):
            raise ValueError("Expected a JSON array")
        session_state = convert_cookies(raw_cookies)
    except Exception as exc:
        raise HTTPException(400, f"Invalid cookie JSON: {exc}")

    # Extract email from cookies if present
    email = ""
    for c in raw_cookies:
        if c.get("name") == "idp_user_email":
            email = c.get("value", "")
            break

    session_json = json.dumps(session_state)

    # Write session file
    session_file = SESSIONS_DIR / f"{label}.json"
    session_file.write_text(session_json, encoding="utf-8")

    # Store in DB
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                """INSERT INTO accounts (label, email, session_json)
                   VALUES (?, ?, ?)""",
                (label, email, session_json),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(409, f"Account '{label}' already exists")

    # Fetch initial loyalty points in background
    asyncio.create_task(_fetch_and_store_points(label))

    await broadcast({"type": "accounts_update"})
    return {"label": label, "email": email}


class AddViaBrowserRequest(BaseModel):
    label: str


@app.post("/api/accounts/add-via-browser", status_code=201)
async def add_account_via_browser(req: AddViaBrowserRequest):
    """Create a new account record and immediately trigger a browser login to capture the session."""
    label = req.label.strip()
    if not label:
        raise HTTPException(400, "Label is required")

    empty_session = json.dumps({"cookies": [], "origins": []})
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO accounts (label, session_json) VALUES (?, ?)",
                (label, empty_session),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(409, f"Account '{label}' already exists")

    if label in browser_login_running:
        raise HTTPException(409, "Browser login already in progress")

    asyncio.create_task(_do_browser_login(label))
    await broadcast({"type": "accounts_update"})
    return {"label": label, "triggered": True}


@app.put("/api/accounts/{label}/cookies")
async def update_cookies(label: str, req: AddAccountRequest):
    """Replace the session cookies for an existing account (re-import flow)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT label FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, f"Account '{label}' not found")

    raw = req.cookies_json.strip()
    try:
        cookies_list = json.loads(raw)
        if not isinstance(cookies_list, list):
            raise ValueError("expected JSON array")
    except Exception as e:
        raise HTTPException(400, f"Invalid cookie JSON: {e}")

    session_data = convert_cookies(cookies_list)
    session_json = json.dumps(session_data)
    email = next(
        (c["value"][:60] for c in cookies_list if c.get("name") == "idp_reguser"),
        "",
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET session_json = ?, email = ?, session_refresh_failed = 0 "
            "WHERE label = ?",
            (session_json, email, label),
        )
        await db.commit()

    session_file = SESSIONS_DIR / f"{label}.json"
    session_file.write_text(session_json, encoding="utf-8")

    asyncio.create_task(_fetch_and_store_points(label))
    await broadcast({"type": "accounts_update"})
    return {"label": label, "updated": True}


class RenameRequest(BaseModel):
    new_label: str


@app.put("/api/accounts/{label}/rename")
async def rename_account(label: str, req: RenameRequest):
    new_label = req.new_label.strip()
    if not new_label:
        raise HTTPException(400, "New label is required")
    if new_label == label:
        return {"label": new_label}

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT label FROM accounts WHERE label = ?", (label,)) as cur:
            if not await cur.fetchone():
                raise HTTPException(404, f"Account '{label}' not found")
        try:
            await db.execute("UPDATE accounts SET label = ? WHERE label = ?", (new_label, label))
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(409, f"Account '{new_label}' already exists")

    old_file = SESSIONS_DIR / f"{label}.json"
    new_file = SESSIONS_DIR / f"{new_label}.json"
    if old_file.exists():
        old_file.rename(new_file)

    await broadcast({"type": "accounts_update"})
    return {"label": new_label}


async def _fetch_and_store_points(label: str) -> None:
    await asyncio.sleep(1)
    try:
        points = await refresh_account_points(label)
        log.info(f"[{label}] Initial loyalty points: {points}")
        await broadcast({"type": "accounts_update"})
    except Exception as exc:
        log.warning(f"[{label}] Initial points fetch failed: {exc}")


@app.delete("/api/accounts/{label}")
async def delete_account(label: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT label FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, f"Account '{label}' not found")
        await db.execute("DELETE FROM accounts WHERE label = ?", (label,))
        await db.execute("DELETE FROM run_logs WHERE label = ?", (label,))
        await db.commit()

    session_file = SESSIONS_DIR / f"{label}.json"
    if session_file.exists():
        session_file.unlink()

    await broadcast({"type": "accounts_update"})
    return {"deleted": label}


@app.put("/api/accounts/{label}/schedule")
async def set_schedule(label: str, req: ScheduleRequest):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT label FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, f"Account '{label}' not found")

        if req.days is None:
            schedule_json = "null"
        else:
            schedule = {"days": req.days, "hour": req.hour, "minute": req.minute}
            schedule_json = json.dumps(schedule)

        await db.execute(
            "UPDATE accounts SET schedule_json = ? WHERE label = ?",
            (schedule_json, label),
        )
        await db.commit()

    await broadcast({"type": "accounts_update"})
    return {"label": label, "schedule": json.loads(schedule_json)}


@app.delete("/api/accounts/{label}/schedule")
async def clear_schedule(label: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT label FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, f"Account '{label}' not found")
        await db.execute(
            "UPDATE accounts SET schedule_json = 'null' WHERE label = ?", (label,)
        )
        await db.commit()

    await broadcast({"type": "accounts_update"})
    return {"label": label, "schedule": None}


@app.post("/api/accounts/{label}/run")
async def trigger_run(label: str, req: RunRequest = RunRequest()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT label FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, f"Account '{label}' not found")

    if label in running_accounts:
        raise HTTPException(409, f"Account '{label}' is already running")

    asyncio.create_task(run_account_tasks(label, req.tasks))
    return {"label": label, "triggered": True}


@app.post("/api/accounts/{label}/refresh-session")
async def trigger_session_refresh(label: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT label FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, f"Account '{label}' not found")

    asyncio.create_task(_do_session_refresh(label))
    return {"label": label, "triggered": True}


async def _do_session_refresh(label: str) -> None:
    await broadcast({"type": "session_refresh_started", "label": label})
    ok = await refresh_session_via_browser(label)
    await broadcast({"type": "accounts_update"})
    if not ok:
        log.warning(f"[{label}] Manual session refresh failed")


# ---------------------------------------------------------------------------
# Browser login — opens a headed Chrome window, user completes email-code
# passwordless login, Playwright captures the full session (incl. Auth0 cookies)
# ---------------------------------------------------------------------------

LOGIN_TIMEOUT_SECS = 300  # 5 minutes for user to complete the email flow

browser_login_running: set[str] = set()


async def capture_session_via_browser_login(label: str) -> bool:
    """
    Launch a real Chrome process (not Playwright-launched) with remote debugging,
    then connect Playwright via CDP to observe the session without triggering
    Akamai bot detection. The user completes the passwordless login manually.
    """
    import subprocess
    import shutil
    import tempfile
    import os

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT label FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()
    if not row:
        return False

    # Find the Chrome executable (platform-aware)
    if IS_LINUX:
        chrome_candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
    else:
        username = os.environ.get("USERNAME", "")
        chrome_candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            rf"C:\Users\{username}\AppData\Local\Google\Chrome\Application\chrome.exe",
        ]
    chrome_exe = next((p for p in chrome_candidates if os.path.exists(p)), None)
    if not chrome_exe:
        log.error(f"[{label}] Could not find Chrome executable — install Google Chrome")
        return False

    debug_port = 9223
    user_data_dir = tempfile.mkdtemp(prefix="ikea_chrome_login_")
    proc = None

    try:
        from playwright.async_api import async_playwright

        # On Linux, Xvfb + x11vnc were already started by _do_browser_login.
        # Just point Chrome at the virtual display.
        chrome_env = None
        if IS_LINUX:
            chrome_env = os.environ.copy()
            chrome_env["DISPLAY"] = VNC_DISPLAY

        log.info(f"[{label}] Browser login: launching Chrome (no automation flags)")
        chrome_args = [
            chrome_exe,
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "https://www.ikea.com/pl/pl/profile/login/",
        ]
        if IS_LINUX:
            chrome_args += ["--no-sandbox", "--disable-gpu", "--window-size=1280,800", "--start-maximized"]
        proc = subprocess.Popen(chrome_args, env=chrome_env)

        # Give Chrome a moment to start its DevTools endpoint
        await asyncio.sleep(4)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(
                f"http://localhost:{debug_port}"
            )
            # Use the existing context/page that Chrome already opened
            contexts = browser.contexts
            context = contexts[0] if contexts else await browser.new_context()
            pages = context.pages
            page = pages[0] if pages else await context.new_page()

            log.info(f"[{label}] Browser login: waiting up to {LOGIN_TIMEOUT_SECS}s for user to complete login")

            await page.wait_for_url(
                lambda url: (
                    "ikea.com/pl/pl/" in url
                    and "profile/login" not in url
                    and "accounts.ikea.com" not in url
                ),
                timeout=LOGIN_TIMEOUT_SECS * 1000,
            )

            # Let Auth0 finish writing its session cookies
            await asyncio.sleep(3)

            fresh_state = await context.storage_state()
            await browser.close()

        fresh_json = json.dumps(fresh_state)
        session_file = SESSIONS_DIR / f"{label}.json"
        session_file.write_text(fresh_json, encoding="utf-8")

        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE accounts SET session_json = ?, session_refreshed_at = ?, "
                "session_refresh_failed = 0 WHERE label = ?",
                (fresh_json, now, label),
            )
            await db.commit()

        log.info(f"[{label}] Browser login session captured successfully")
        return True

    except Exception as exc:
        log.error(f"[{label}] Browser login failed: {exc}")
        return False

    finally:
        if proc and proc.poll() is None:
            proc.terminate()
        shutil.rmtree(user_data_dir, ignore_errors=True)


@app.post("/api/accounts/{label}/browser-login")
async def trigger_browser_login(label: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT label FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, f"Account '{label}' not found")

    if label in browser_login_running:
        raise HTTPException(409, "Browser login already in progress for this account")

    asyncio.create_task(_do_browser_login(label))
    return {"label": label, "triggered": True}


async def _start_vnc_display() -> tuple:
    """Start Xvfb + x11vnc. Returns (xvfb_proc, x11vnc_proc)."""
    import subprocess as _sp
    xvfb = _sp.Popen(
        ["Xvfb", VNC_DISPLAY, "-screen", "0", "1280x800x24", "-ac"],
        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
    )
    _vnc_procs.append(xvfb)
    await asyncio.sleep(1)
    x11vnc = _sp.Popen(
        ["x11vnc", "-display", VNC_DISPLAY, "-rfbport", str(VNC_PORT),
         "-localhost", "-nopw", "-forever", "-quiet", "-shared"],
        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
    )
    _vnc_procs.append(x11vnc)
    await asyncio.sleep(1)
    return xvfb, x11vnc


async def _stop_vnc_display(xvfb_proc, x11vnc_proc) -> None:
    for p in [x11vnc_proc, xvfb_proc]:
        if p:
            if p.poll() is None:
                p.terminate()
            if p in _vnc_procs:
                _vnc_procs.remove(p)


async def _do_browser_login(label: str) -> None:
    browser_login_running.add(label)
    xvfb_proc = x11vnc_proc = None
    try:
        if IS_LINUX:
            log.info(f"[{label}] Starting VNC display for browser login")
            xvfb_proc, x11vnc_proc = await _start_vnc_display()
        await broadcast({"type": "browser_login_started", "label": label, "vnc": IS_LINUX})
        ok = await capture_session_via_browser_login(label)
        if ok:
            await broadcast({"type": "browser_login_done", "label": label, "success": True})
        else:
            await broadcast({"type": "browser_login_done", "label": label, "success": False})
        await broadcast({"type": "accounts_update"})
    finally:
        browser_login_running.discard(label)
        if IS_LINUX:
            await _stop_vnc_display(xvfb_proc, x11vnc_proc)


@app.post("/api/accounts/{label}/refresh")
async def refresh_points(label: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT label FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, f"Account '{label}' not found")

    points = await refresh_account_points(label)
    await broadcast({"type": "accounts_update"})
    return {"label": label, "loyalty_points": points}


@app.get("/api/accounts/{label}/points-history")
async def get_points_history(label: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT ts, points, delta FROM points_log WHERE label = ? ORDER BY id DESC LIMIT 100",
            (label,),
        ) as cur:
            rows = await cur.fetchall()
    return [{"ts": r["ts"], "points": r["points"], "delta": r["delta"]} for r in rows]


@app.get("/api/accounts/{label}/reward-history")
async def get_reward_history(label: str):
    """Return stored IKEA reward transaction history for this account."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT event_id, event_type, value, datetime, description
               FROM reward_history WHERE label = ? ORDER BY datetime DESC LIMIT 500""",
            (label,),
        ) as cur:
            rows = await cur.fetchall()
    return [
        {
            "id":          r["event_id"],
            "type":        r["event_type"],
            "value":       r["value"],
            "datetime":    r["datetime"],
            "description": r["description"],
        }
        for r in rows
    ]


@app.get("/api/accounts/{label}/vouchers")
async def get_vouchers(label: str):
    """Return cached vouchers for this account."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT vouchers_json, vouchers_updated_at, vouchers_error FROM accounts WHERE label = ?", (label,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, f"Account '{label}' not found")
    return {
        "vouchers": json.loads(row["vouchers_json"] or "[]"),
        "updated_at": row["vouchers_updated_at"],
        "vouchers_error": row["vouchers_error"],
    }


@app.post("/api/accounts/{label}/vouchers/refresh")
async def refresh_vouchers(label: str):
    """Trigger a fresh voucher fetch from ikeafamily.eu (runs in background)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT label FROM accounts WHERE label = ?", (label,)) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, f"Account '{label}' not found")

    async def _run():
        vouchers = await fetch_account_vouchers(label)
        # Re-read error state from DB (set inside fetch_account_vouchers)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT vouchers_error FROM accounts WHERE label = ?", (label,)
            ) as cur:
                err_row = await cur.fetchone()
        vouchers_error = err_row["vouchers_error"] if err_row else None
        await broadcast({"type": "vouchers_update", "label": label, "vouchers": vouchers, "vouchers_error": vouchers_error})

    asyncio.create_task(_run())
    return {"label": label, "triggered": True}


@app.get("/api/accounts/{label}/logs")
async def get_logs(label: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, label, started_at, finished_at, status, errors
               FROM run_logs WHERE label = ? ORDER BY id DESC LIMIT 50""",
            (label,),
        ) as cur:
            rows = await cur.fetchall()

    return [
        {
            "id": r["id"],
            "label": r["label"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "status": r["status"],
            "errors": json.loads(r["errors"] or "[]"),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# WebSocket endpoints
# ---------------------------------------------------------------------------

@app.websocket("/ws/vnc")
async def vnc_ws_proxy(ws: WebSocket):
    """Proxy WebSocket → raw TCP to the local x11vnc VNC server (port 5900).
    noVNC connects here and we forward bytes verbatim both ways."""
    if ws.cookies.get(AUTH_COOKIE) not in _valid_sessions:
        await ws.close(code=1008)
        return
    sec_proto = ws.headers.get("sec-websocket-protocol", "")
    protos = [p.strip() for p in sec_proto.split(",")] if sec_proto else []
    subprotocol = "binary" if "binary" in protos else (protos[0] if protos else None)
    await ws.accept(subprotocol=subprotocol)

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", VNC_PORT)
    except Exception as exc:
        log.warning(f"VNC proxy: cannot connect to x11vnc: {exc}")
        await ws.close(1011)
        return

    async def ws_to_tcp():
        try:
            while True:
                data = await ws.receive_bytes()
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def tcp_to_ws():
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send_bytes(data)
        except Exception:
            pass

    t1 = asyncio.create_task(ws_to_tcp())
    t2 = asyncio.create_task(tcp_to_ws())
    done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if ws.cookies.get(AUTH_COOKIE) not in _valid_sessions:
        await ws.close(code=1008)
        return
    await ws.accept()
    ws_connections.append(ws)
    log.info(f"WebSocket connected — total: {len(ws_connections)}")
    try:
        while True:
            # Keep connection alive; client messages are ignored
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if ws in ws_connections:
            ws_connections.remove(ws)
        log.info(f"WebSocket disconnected — total: {len(ws_connections)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webapp:app", host="0.0.0.0", port=8000, reload=False)
