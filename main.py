#!/usr/bin/env python3
"""
Car Wash Scout Agent - Email Edition
-------------------------------------
Reads BizBuySell "new listing" alert emails from a Gmail inbox and adds any
car wash listings that aren't already on the Shullman Car Wash Scout site.

Runs on a schedule (GitHub Actions). No web scraping, no browser - it just
reads the emails BizBuySell sends you and files them onto your site.
"""

import os
import re
import time
import email
import imaplib
import logging
from email.header import decode_header
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# --- Site config ---
SITE_URL = os.environ["SITE_URL"].rstrip("/")
API_URL = f"{SITE_URL}/api/manual-records"
AUTH = (os.environ.get("SCOUT_USER", "shullman"), os.environ.get("SCOUT_PASS", ""))

# --- Gmail config ---
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
# Sender to look for (substring match). BizBuySell alerts come from bizbuysell.com
BIZBUYSELL_FROM = os.environ.get("BIZBUYSELL_FROM", "bizbuysell")
# How many days of emails to scan each run (2 = small overlap so nothing slips)
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))

# Subjects that are account/welcome emails, NOT listing alerts - skip these
SKIP_SUBJECTS = (
    "thank you", "registering", "register", "welcome", "verify", "confirm your",
    "password", "receipt", "reset your", "your account", "saved search created",
    "saved your search",
)

# Anchor text that is navigation, not a business listing
NAV_WORDS = {
    "unsubscribe", "view all", "see all", "manage", "bizbuysell", "privacy",
    "contact us", "help", "sign in", "log in", "log out", "view listing",
    "see details", "view details", "email settings", "update preferences",
    "manage alerts", "saved searches", "see more", "view more", "more listings",
    "facebook", "twitter", "linkedin", "instagram", "app store", "google play",
    "terms", "advertise", "sell a business", "browse", "franchises",
    "business summary", "view complete details", "view details", "request additional",
    "request information", "view photo", "view listing details",
}


# ---------------------------------------------------------------------------
# Site API
# ---------------------------------------------------------------------------

def get_existing_keys() -> set:
    """Return name|state dedup keys for every listing already on the site."""
    for attempt in range(1, 4):
        try:
            log.info(f"Fetching existing site listings (attempt {attempt})...")
            resp = requests.get(API_URL, auth=AUTH, timeout=90)
            resp.raise_for_status()
            records = resp.json()
            if isinstance(records, dict):
                records = records.get("records", [])
            keys = set()
            for r in records:
                keys.add(listing_key(r.get("name", ""), r.get("state", "")))
                lid = extract_listing_id(r.get("research_url", ""))
                if lid:
                    keys.add(f"bbs-{lid}")
            return keys
        except Exception as e:
            log.warning(f"Attempt {attempt} failed: {e}")
            if attempt < 3:
                log.info("Waiting 30s for site to wake up...")
                time.sleep(30)
    raise RuntimeError("Site did not respond after 3 attempts")


def add_listing(listing: dict) -> dict:
    # Render free tier can throw transient 502s - retry on server errors
    for attempt in range(1, 4):
        try:
            resp = requests.post(API_URL, json=listing, auth=AUTH, timeout=45)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} server error (site waking up)")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt < 3:
                log.warning(f"  POST attempt {attempt} failed ({e}); retrying in 20s...")
                time.sleep(20)
            else:
                raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def listing_key(name: str, state: str) -> str:
    """A stable dedup key based on the business name + state."""
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    s = (state or "").strip().lower()
    return f"{n}|{s}"


def parse_state(location: str) -> str:
    m = re.search(r",\s*([A-Z]{2})\b", location or "")
    return m.group(1) if m else ""


def is_listing_url(href: str) -> bool:
    """True only for real BizBuySell listing pages, not nav/footer links."""
    h = (href or "").lower()
    if "bizbuysell" not in h:
        return False
    # Alert emails link listings as /listings/Profile/?q=<id>
    if "/listings/profile" in h:
        return True
    if "business-opportunity" in h:
        return True
    if re.search(r"[?&]q=\d+", h):
        return True
    if re.search(r"/\d{6,}", h):
        return True
    return False


def extract_listing_id(href: str) -> str:
    """Pull the stable BizBuySell listing id out of a link."""
    m = re.search(r"[?&]q=(\d+)", href or "")
    if m:
        return m.group(1)
    m = re.search(r"/(\d{6,})", href or "")
    return m.group(1) if m else ""


def listing_url(listing_id: str) -> str:
    return f"https://www.bizbuysell.com/listings/Profile/?q={listing_id}"


def maps_url(location: str) -> str:
    if not location:
        return ""
    return "https://www.google.com/maps/search/" + requests.utils.quote(location)


def dedup_key_for(listing: dict) -> str:
    """Prefer the stable listing id; fall back to name|state."""
    lid = extract_listing_id(listing.get("research_url", ""))
    if lid:
        return f"bbs-{lid}"
    return listing_key(listing.get("name", ""), listing.get("state", ""))


def looks_like_location(text: str) -> bool:
    return bool(re.match(r"^[A-Z][A-Za-z .'\-]+,\s*[A-Z]{2}\b", (text or "").strip()))


def extract_phone(text: str) -> str:
    m = re.search(r"Phone:\s*([\d\-\(\)\+.\s]{7,})", text or "")
    if not m:
        return ""
    digits = re.sub(r"\D", "", m.group(1))
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return m.group(1).strip()


def extract_price(text: str) -> str:
    # Prefer an explicit "Asking" price, else the first dollar amount
    m = re.search(r"Asking(?:\s*Price)?[:\s]*(\$[\d,]+(?:\.\d+)?\s*[KkMm]?)", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"\$[\d,]+(?:\.\d+)?\s*[KkMm]?", text)
    return m.group(0).strip() if m else ""


def extract_location(text: str) -> str:
    m = re.search(r"([A-Z][A-Za-z .'\-]+,\s*[A-Z]{2})\b", text or "")
    return m.group(1).strip() if m else ""


def decode_str(raw) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    out = ""
    for txt, enc in parts:
        if isinstance(txt, bytes):
            out += txt.decode(enc or "utf-8", errors="replace")
        else:
            out += txt
    return out


def get_html_body(msg) -> str:
    """Pull the best body (prefer HTML) out of an email message."""
    if msg.is_multipart():
        html, text = None, None
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                content = payload.decode(charset, errors="replace")
            except (LookupError, TypeError):
                content = payload.decode("utf-8", errors="replace")
            if ctype == "text/html" and html is None:
                html = content
            elif ctype == "text/plain" and text is None:
                text = content
        return html or text or ""
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Gmail reading
# ---------------------------------------------------------------------------

def fetch_alert_emails() -> list:
    """Return the HTML bodies of recent BizBuySell alert emails."""
    log.info(f"Connecting to Gmail as {GMAIL_USER}...")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    mail.select("INBOX")

    since = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    criteria = f'(SINCE "{since}" FROM "{BIZBUYSELL_FROM}")'
    log.info(f"Searching emails: {criteria}")
    status, data = mail.search(None, criteria)

    bodies = []
    if status == "OK" and data and data[0]:
        ids = data[0].split()
        log.info(f"Found {len(ids)} BizBuySell email(s) in the last {LOOKBACK_DAYS} days")
        for eid in ids:
            st, msg_data = mail.fetch(eid, "(RFC822)")
            if st != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = decode_str(msg.get("Subject"))
            if any(s in subject.lower() for s in SKIP_SUBJECTS):
                log.info(f"  Skipping non-listing email: {subject}")
                continue
            log.info(f"  Reading email: {subject}")
            bodies.append(get_html_body(msg))
    else:
        log.warning("No BizBuySell emails found in the search window.")

    mail.logout()
    return bodies


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_listings(html: str, today: str) -> list:
    """Extract car wash listings from a BizBuySell alert email.

    Handles both formats: single-listing emails and multi-listing digests.
    Each listing is keyed by its stable id (the ?q= value in the link).
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) Collect listings by stable id, in document order.
    order = []        # listing ids, first-seen order
    names = {}        # id -> best business-name anchor
    locations = {}    # id -> location anchor ("City, ST ...")
    for a in soup.find_all("a", href=True):
        if not is_listing_url(a["href"]):
            continue
        lid = extract_listing_id(a["href"])
        if not lid:
            continue
        if lid not in order:
            order.append(lid)
        text = " ".join(a.get_text(" ", strip=True).split())
        low = text.lower()
        if len(text) < 6 or any(w in low for w in NAV_WORDS):
            continue
        if looks_like_location(text):
            locations.setdefault(lid, text)
            continue
        # business-name candidate: keep the longest
        if lid not in names or len(text) > len(names[lid]):
            names[lid] = text

    # 2) Slice the email text into per-listing segments by name position.
    full = " ".join(soup.get_text(" ", strip=True).split())
    cut = re.search(r"This email is being sent|To unsubscribe|CoStar Group", full)
    if cut:
        full = full[: cut.start()]
    positions = []
    for lid in order:
        nm = names.get(lid)
        idx = full.find(nm) if nm else -1
        positions.append((idx if idx >= 0 else 10 ** 9, lid))
    positions.sort()
    segment = {}
    for i, (pos, lid) in enumerate(positions):
        if pos >= 10 ** 9:
            segment[lid] = ""
            continue
        nxt = positions[i + 1][0] if i + 1 < len(positions) else len(full)
        end = nxt if nxt < 10 ** 9 else len(full)
        segment[lid] = full[pos:end]

    # 3) Build records - car washes only.
    listings = []
    for lid in order:
        name = names.get(lid)
        if not name:
            continue
        if "wash" not in name.lower():
            log.info(f"  Skipping (not a car wash): {name}")
            continue
        seg = segment.get(lid) or name
        location = locations.get(lid, "") or extract_location(seg)
        location = re.sub(r"\s*\([^)]*\).*$", "", location).strip().rstrip(":").strip()
        state = parse_state(location)
        if not state:
            # Digest emails label each listing's state as "Location: XX"
            m = re.search(r"Location:\s*([A-Z]{2})\b", seg)
            if m:
                state = m.group(1)
        # Trim trailing franchise ads / boilerplate from the note text
        summary = re.split(r"Location:\s*[A-Z]{2}\b|The following franchises", seg)[0].strip()
        note = ((summary or name)[:600].strip() + f"\n\nAuto-imported from BizBuySell email alert on {today}").strip()
        listings.append({
            "name": name,
            "market": location,
            "state": state,
            "asking_price": extract_price(seg),
            "phone": extract_phone(seg),
            "research_url": listing_url(lid),
            "maps_url": maps_url(location) if location else "",
            "source": "BizBuySell Email Alert",
            "note": note,
        })

    if not listings:
        log.warning("No car wash listings parsed from this email.")
        log.warning(f"  Email text snippet: {full[:800]}")

    return listings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== Car Wash Scout Agent (Email Edition) starting ===")
    today = datetime.utcnow().strftime("%Y-%m-%d")

    existing = get_existing_keys()
    log.info(f"{len(existing)} listings already on the site")

    bodies = fetch_alert_emails()
    if not bodies:
        log.info("Nothing to do - no alert emails. Done.")
        return

    # Parse every email, collect unique new listings
    new_listings = []
    seen = set(existing)
    for html in bodies:
        for listing in parse_listings(html, today):
            key = dedup_key_for(listing)
            if key in seen:
                log.info(f"  - Already have: {listing['name']}")
                continue
            seen.add(key)
            new_listings.append(listing)
            log.info(f"  + New: {listing['name']} - {listing.get('market', '')} {listing.get('asking_price', '')}")

    log.info(f"--- Adding {len(new_listings)} new listing(s) to the site ---")
    added, failed = 0, 0
    for listing in new_listings:
        try:
            add_listing(listing)
            log.info(f"Added: {listing['name']}")
            added += 1
        except Exception as e:
            log.error(f"Failed to add '{listing['name']}': {e}")
            failed += 1

    log.info(f"=== Done. {added} added, {failed} failed ===")


if __name__ == "__main__":
    main()
