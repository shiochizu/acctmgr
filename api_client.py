"""
Direct API calls to IKEA's internal endpoints.
Bypasses browser automation for tasks that are simple API calls under the hood.

Confirmed endpoints (captured via live browser interception):
  - Favourites: GraphQL at favs.oneweb.ingka.com/graphql
  - Events:     REST at customer.prod.store-events.ingka.com/api/v1.0/events/

Auth: cookies from the saved session file (idp_reguser JWT + session cookies).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
import httpx

GRAPHQL_FAVS  = "https://favs.oneweb.ingka.com/graphql"
EVENTS_API    = "https://customer.prod.store-events.ingka.com/api/v1.0/events"
SESSIONS_DIR  = Path("sessions")


def load_cookies(label: str) -> dict[str, str]:
    """Load cookies from a saved session file as a flat name→value dict."""
    session_file = SESSIONS_DIR / f"{label}.json"
    state = json.loads(session_file.read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in state["cookies"]}


def auth_headers(label: str) -> dict[str, str]:
    """Return headers with Bearer token extracted from the idp_reguser JWT cookie."""
    cookies = load_cookies(label)
    jwt = cookies.get("idp_reguser", "")
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://www.ikea.com",
        "Referer": "https://www.ikea.com/pl/pl/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    return headers


def events_headers(label: str) -> dict[str, str]:
    """Return headers required by the store-events API (X-USER-AUTH)."""
    cookies = load_cookies(label)
    jwt = cookies.get("idp_reguser", "")
    return {
        "Content-Type": "application/json",
        "X-USER-AUTH": jwt,
        "Origin": "https://www.ikea.com",
        "Referer": "https://www.ikea.com/pl/pl/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }


# ---------------------------------------------------------------------------
# Favourites
# ---------------------------------------------------------------------------

async def create_favourites_list_via_page(page, list_name: str) -> dict:
    """
    Create a favourites list using Playwright's APIRequestContext.
    Sends through the browser's network stack (real TLS fingerprint, browser cookie jar)
    without CORS restrictions — bypasses Cloudflare bot detection.
    """
    import json as _json

    mutation = (
        "mutation CreateList($name: String!) {"
        "  list: createList(name: $name) { listId name quantity updated }"
        "}"
    )
    payload = {
        "query": mutation,
        "variables": {"name": list_name, "languageCode": "pl", "storeId": "205", "withStore": True},
    }

    all_cookies = await page.context.cookies()
    jwt = next((c["value"] for c in all_cookies if c["name"] == "idp_reguser"), "")

    response = await page.context.request.post(
        GRAPHQL_FAVS,
        data=_json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {jwt}",
            "Origin": "https://www.ikea.com",
            "Referer": "https://www.ikea.com/pl/pl/",
        },
    )

    body = await response.text()
    if not response.ok:
        raise RuntimeError(f"GraphQL HTTP {response.status}: {body[:400]}")

    data = _json.loads(body)
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]["list"]


def create_favourites_list(label: str, list_name: str) -> dict:
    """
    Create a favourites list via the IKEA GraphQL API.
    Returns: { listId, name, quantity, updated }
    """
    cookies = load_cookies(label)
    mutation = """
    mutation CreateList($name: String!) {
      list: createList(name: $name) {
        listId name quantity updated
      }
    }
    """
    payload = {
        "query": mutation,
        "variables": {"name": list_name, "languageCode": "pl", "storeId": "205", "withStore": True},
    }
    r = httpx.post(GRAPHQL_FAVS, json=payload, cookies=cookies,
                   headers=auth_headers(label), timeout=15)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]["list"]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def list_available_events(label: str) -> list:
    """
    Return event IDs for all PL events that have at least one non-closed, future timeslot.
    The listing endpoint uses countryCode=pl (lowercase) and field name 'id'.
    NOTE: No cookies — auth is entirely via X-USER-AUTH header; sending ikea.com cookies
    to this domain bloats headers and causes 431 Request Header Fields Too Large.
    """
    r = httpx.get(EVENTS_API, params={"countryCode": "pl"},
                  headers=events_headers(label), timeout=15)
    r.raise_for_status()
    now = datetime.now(timezone.utc).timestamp()
    result = []
    for event in r.json():
        event_id = event.get("id")
        if not event_id:
            continue
        slots = event.get("timeSlots", [])
        has_open = any(
            not s.get("registrationClosed") and s.get("utcEndDate", 0) > now
            for s in slots
        )
        if has_open:
            result.append(event_id)
    return result


def get_event(label: str, event_id: str) -> dict:
    """Fetch event details including all timeslots."""
    r = httpx.get(f"{EVENTS_API}/{event_id}",
                  headers=events_headers(label), timeout=15)
    r.raise_for_status()
    return r.json()


def get_customer_profile(label: str) -> dict:
    """Fetch the logged-in customer's profile (name, email, loyalty number)."""
    r = httpx.get(f"{EVENTS_API}/customer",
                  headers=events_headers(label), timeout=15)
    r.raise_for_status()
    return r.json()


def pick_timeslot(event: dict) -> dict:
    """Return the first available (not closed, has capacity) timeslot."""
    for slot in event.get("timeSlots", []):
        if slot.get("registrationClosed"):
            continue
        settings = slot.get("registrationSettings", {})
        capacity = settings.get("maxRegistrationCount", 0)
        taken    = slot.get("currentRegistrationCount", 0)
        if taken < capacity:
            return slot
    raise RuntimeError("No available timeslots found for this event.")


def register_for_event(label: str, event_id: str, timeslot_id: str) -> None:
    """Step 1 — register for a timeslot (state=REGISTERED)."""
    payload = {
        "contactMethods": [],
        "state": "REGISTERED",
        "guests": 0,
        "childCount": 0,
        "adultCount": 1,
        "pageLang": "pl",
    }
    r = httpx.put(
        f"{EVENTS_API}/{event_id}/timeslots/{timeslot_id}/registrations",
        json=payload,
        headers=events_headers(label), timeout=15,
    )
    # 409 Conflict = already registered — treat as success
    if r.status_code != 409:
        r.raise_for_status()


def confirm_event_attendance(label: str, event_id: str, timeslot_id: str) -> None:
    """Step 2 — confirm attendance (state=SELF_CHECKIN)."""
    profile = get_customer_profile(label)
    payload = {
        "userId":         profile["partyUid"],
        "adultCount":     1,
        "childCount":     0,
        "state":          "SELF_CHECKIN",
        "creationTime":   datetime.now(timezone.utc).isoformat(),
        "contactMethods": [],
        "firstName":      profile["firstName"],
        "lastName":       profile["lastName"],
        "emailAddress":   profile["emailAddress"],
        "phoneNumber":    profile.get("phoneNumber"),
        "loyalty":        profile.get("familyNumber"),
        "loyaltyPrograms": ["IKEA_FAMILY"],
        "companyName":    profile.get("companyName"),
        "contact": {
            "EMAIL": profile["emailAddress"],
            "SMS":   None,
        },
    }
    r = httpx.put(
        f"{EVENTS_API}/{event_id}/timeslots/{timeslot_id}/registrations",
        json=payload,
        headers=events_headers(label), timeout=15,
    )
    if r.status_code != 409:
        r.raise_for_status()


def _open_slots(event: dict) -> list:
    """Return timeslots that are open and not full. maxRegistrationCount=0 means unlimited."""
    now = datetime.now(timezone.utc).timestamp()
    result = []
    for s in event.get("timeSlots", []):
        if s.get("registrationClosed"):
            continue
        if s.get("utcEndDate", float("inf")) < now:
            continue
        max_count = s.get("registrationSettings", {}).get("maxRegistrationCount", 0)
        cur_count = s.get("currentRegistrationCount", 0)
        if max_count == 0 or cur_count < max_count:  # 0 = no cap
            result.append(s)
    return result


def join_and_confirm_event(label: str, event_id: str) -> dict:
    """
    Full flow: fetch event -> pick first available timeslot -> register -> confirm.
    Tries all available timeslots until one succeeds (handles duplicate-registration 500s).
    Returns: { event_id, timeslot_id, start, end }
    """
    event = get_event(label, event_id)
    slots = _open_slots(event)
    if not slots:
        raise RuntimeError("No available timeslots found for this event.")

    last_err = None
    for timeslot in slots:
        timeslot_id = timeslot["timeslotId"]
        try:
            register_for_event(label, event_id, timeslot_id)
            confirm_event_attendance(label, event_id, timeslot_id)
            return {
                "event_id":    event_id,
                "timeslot_id": timeslot_id,
                "start":       timeslot["startDate"],
                "end":         timeslot["endDate"],
            }
        except Exception as exc:
            last_err = exc
            continue

    raise RuntimeError(f"All timeslots failed. Last error: {last_err}")


def join_random_event(label: str) -> dict:
    """
    Pick a random available PL event and join it.
    Falls back to the next candidate if an event has no open slots or registration fails.
    """
    import random as _random
    event_ids = list_available_events(label)
    if not event_ids:
        raise RuntimeError("No available events found in PL catalog.")
    _random.shuffle(event_ids)
    last_err = None
    for event_id in event_ids:
        try:
            return join_and_confirm_event(label, event_id)
        except Exception as exc:
            last_err = exc
            continue
    raise RuntimeError(f"All {len(event_ids)} events failed. Last error: {last_err}")


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    cmd   = sys.argv[1] if len(sys.argv) > 1 else "help"
    label = sys.argv[2] if len(sys.argv) > 2 else "account-1"

    if cmd == "list":
        name = sys.argv[3] if len(sys.argv) > 3 else f"API Test {datetime.now().date()}"
        print(create_favourites_list(label, name))

    elif cmd == "event":
        event_id = sys.argv[3] if len(sys.argv) > 3 else "9f2791eb-df38-411f-9d90-d9913fc2a997"
        print(join_and_confirm_event(label, event_id))

    else:
        print("Usage:")
        print("  python api_client.py list   <account-label> [list-name]")
        print("  python api_client.py event  <account-label> <event-id>")
