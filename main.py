#!/usr/bin/env python3
"""
Car Wash Scout Agent
Scrapes BizBuySell and LoopNet for US car wash listings,
visits each listing's detail page for full data (phone, EBITDA, acres, etc.),
and adds any new listings to the Shullman Car Wash Scout site.
"""

import os
import re
import time
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

SITE_URL = os.environ["SITE_URL"].rstrip("/")
API_URL = f"{SITE_URL}/api/manual-records"


# ---------------------------------------------------------------------------
# Site API helpers
# ---------------------------------------------------------------------------

def get_existing_keys() -> set:
    # Render free tier sleeps — retry up to 3 times with a long timeout
    for attempt in range(1, 4):
        try:
            log.info(f"Fetching existing listings (attempt {attempt})...")
            resp = requests.get(API_URL, timeout=90)
            resp.raise_for_status()
            records = resp.json()
            if isinstance(records, dict):
                records = records.get("records", [])
            keys: set = set()
            for r in records:
                url = (r.get("research_url") or "").strip().lower()
                if url:
                    keys.add(url)
                else:
                    name = (r.get("name") or "").strip().lower()
                    state = (r.get("state") or "").strip().lower()
                    if name:
                        keys.add(f"{name}|{state}")
            return keys
        except Exception as e:
            log.warning(f"Attempt {attempt} failed: {e}")
            if attempt < 3:
                log.info("Waiting 30s for site to wake up...")
                time.sleep(30)
    raise RuntimeError("Site did not respond after 3 attempts")


def add_listing(listing: dict) -> dict:
    resp = requests.post(API_URL, json=listing, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def parse_state(text: str) -> str:
    m = re.search(r",\s*([A-Z]{2})\b", text)
    return m.group(1) if m else ""


def dedup_key(listing: dict) -> str:
    url = (listing.get("research_url") or "").strip().lower()
    if url:
        return url
    name = (listing.get("name") or "").strip().lower()
    state = (listing.get("state") or "").strip().lower()
    return f"{name}|{state}"


def maps_url(address: str) -> str:
    if not address:
        return ""
    return f"https://www.google.com/maps/search/{requests.utils.quote(address)}"


def first_text(page, *selectors) -> str:
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                t = el.inner_text().strip()
                if t:
                    return t
        except Exception:
            pass
    return ""


def regex_find(pattern: str, html: str, group: int = 1) -> str:
    m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    return m.group(group).strip() if m else ""


def new_browser(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    return browser, ctx.new_page()


# ---------------------------------------------------------------------------
# BizBuySell — Pass 1: collect listing stubs from search results
# ---------------------------------------------------------------------------

def collect_bizbuysell_stubs(page, existing_keys: set) -> list:
    """Return basic listing dicts (name, location, asking_price, url) from search pages."""
    stubs = []
    today = datetime.utcnow().strftime("%Y-%m-%d")

    for pg_num in range(1, 21):
        url = (
            "https://www.bizbuysell.com/car-washes-for-sale/"
            if pg_num == 1
            else f"https://www.bizbuysell.com/car-washes-for-sale/?pg={pg_num}"
        )
        log.info(f"BizBuySell search page {pg_num}: {url}")

        try:
            page.goto(url, timeout=60_000, wait_until="domcontentloaded")
            time.sleep(2)
        except PWTimeout:
            log.warning(f"  Page {pg_num} timed out — stopping")
            break

        body = page.content().lower()
        if "no results" in body or "0 businesses" in body or "no listings" in body:
            log.info("  No more results — stopping pagination")
            break

        cards = (
            page.query_selector_all("[data-listing-id]")
            or page.query_selector_all(".listing-card")
            or page.query_selector_all(".businessCard")
            or page.query_selector_all("ul.listings > li")
        )

        if not cards:
            log.info(f"  No cards on page {pg_num} — stopping")
            break

        log.info(f"  Found {len(cards)} cards")

        for card in cards:
            try:
                link_el = card.query_selector("a[href]")
                href = (link_el.get_attribute("href") if link_el else "") or ""
                if href and not href.startswith("http"):
                    href = f"https://www.bizbuysell.com{href}"

                name_el = (
                    card.query_selector("h2")
                    or card.query_selector("h3")
                    or card.query_selector(".businessName")
                    or card.query_selector("[class*='title']")
                )
                name = (name_el.inner_text().strip() if name_el else "") or "Car Wash"

                loc_el = (
                    card.query_selector(".location")
                    or card.query_selector("[class*='location']")
                    or card.query_selector("[class*='city']")
                )
                location = loc_el.inner_text().strip() if loc_el else ""

                price_el = (
                    card.query_selector("[class*='asking']")
                    or card.query_selector("[class*='price']")
                )
                asking_price = price_el.inner_text().strip() if price_el else ""

                rev_el = (
                    card.query_selector("[class*='revenue']")
                    or card.query_selector("[class*='sales']")
                    or card.query_selector("[class*='cash']")
                )
                sales_card = rev_el.inner_text().strip() if rev_el else ""

                stub = {
                    "name": name,
                    "market": location,
                    "state": parse_state(location),
                    "asking_price": asking_price,
                    "sales": sales_card,
                    "research_url": href,
                    "source": "BizBuySell",
                    "note": f"Auto-scraped from BizBuySell on {today}",
                }
                key = dedup_key(stub)
                if key and key not in existing_keys:
                    stubs.append(stub)
                    existing_keys.add(key)
                    log.info(f"  + {name} — {location}")
                else:
                    log.info(f"  - Already have: {name}")

            except Exception as e:
                log.warning(f"  Error parsing card: {e}")

        time.sleep(1)

    return stubs


# ---------------------------------------------------------------------------
# BizBuySell — Pass 2: enrich each stub by visiting its detail page
# ---------------------------------------------------------------------------

def enrich_bizbuysell(page, stub: dict) -> dict:
    """Visit the listing's detail page and pull phone, EBITDA, website, year, etc."""
    href = stub.get("research_url", "")
    if not href:
        return stub

    log.info(f"  Enriching: {href}")
    try:
        page.goto(href, timeout=45_000, wait_until="domcontentloaded")
        time.sleep(2)
    except PWTimeout:
        log.warning(f"  Detail page timed out: {href}")
        return stub

    html = page.content()
    listing = {**stub}

    # Year established
    year = first_text(page,
        "[data-testid='year-established'] .value",
        "[class*='yearEstablished'] .value",
    )
    if not year:
        year = regex_find(r'Year\s+Established[^0-9]*(\d{4})', html)
    if year:
        listing["year"] = year

    # Cash flow = EBITDA
    ebitda = first_text(page,
        "[data-testid='cash-flow'] .value",
        "[data-testid='cashFlow'] .value",
        "[class*='cashFlow'] .value",
        "[class*='cash-flow'] .value",
    )
    if not ebitda:
        ebitda = regex_find(r'Cash\s+Flow[^$\d]*(\$[\d,]+(?:\.\d+)?(?:\s*[KMkm])?)', html)
    if ebitda:
        listing["ebitda"] = ebitda

    # Gross revenue / sales (override card value if found)
    sales = first_text(page,
        "[data-testid='gross-revenue'] .value",
        "[data-testid='grossRevenue'] .value",
        "[class*='grossRevenue'] .value",
    )
    if not sales:
        sales = regex_find(r'Gross\s+Revenue[^$\d]*(\$[\d,]+(?:\.\d+)?(?:\s*[KMkm])?)', html)
    if sales:
        listing["sales"] = sales

    # Full address (more complete than card location)
    full_addr = first_text(page,
        "[itemprop='address']",
        ".businessLocation address",
        "[class*='businessAddress']",
    )
    if not full_addr:
        full_addr = regex_find(r'"streetAddress"\s*:\s*"([^"]+)"', html)
    if full_addr:
        listing["market"] = full_addr
        state = parse_state(full_addr)
        if state:
            listing["state"] = state

    # Phone number
    phone = first_text(page,
        "a[href^='tel:']",
        ".phone-number",
        "[class*='phone']",
        "[itemprop='telephone']",
    )
    if phone.startswith("tel:"):
        phone = phone[4:]
    if not phone:
        phone = regex_find(r'"telephone"\s*:\s*"([^"]+)"', html)
    if phone:
        listing["phone"] = re.sub(r'[^\d\-\(\)\+\s]', '', phone).strip()

    # Website
    website = ""
    for sel in ["a[class*='website']", "a[class*='Website']", ".businessWebsite a"]:
        el = page.query_selector(sel)
        if el:
            website = el.get_attribute("href") or ""
            break
    if not website:
        website = regex_find(
            r'href="(https?://(?!(?:www\.)?bizbuysell\.com)[^"]{6,})"[^>]*>(?:Visit Website|Website|Visit Site)',
            html,
        )
    if website:
        listing["website"] = website

    # Acres / lot size
    acres = regex_find(r'([\d,]+(?:\.\d+)?)\s*(?:acre|acres|AC)\b', html)
    if acres:
        listing["acres"] = acres.replace(",", "")

    # Description for note field (truncated)
    desc_el = page.query_selector(".businessDescription, .description, [class*='businessDesc']")
    if desc_el:
        desc = desc_el.inner_text().strip()
        if desc:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            listing["note"] = desc[:800] + f"\n\nAuto-scraped from BizBuySell on {today}"

    # Generate Google Maps URL from best address we have
    addr = listing.get("market", "")
    if addr:
        listing["maps_url"] = maps_url(addr)

    return listing


# ---------------------------------------------------------------------------
# LoopNet scraper (best-effort — card-level only due to bot protection)
# ---------------------------------------------------------------------------

def scrape_loopnet(page, existing_keys: set) -> list:
    new_listings = []
    today = datetime.utcnow().strftime("%Y-%m-%d")

    url = "https://www.loopnet.com/biz/car-washes-for-sale/"
    log.info(f"LoopNet: {url}")

    try:
        page.goto(url, timeout=60_000, wait_until="domcontentloaded")
        time.sleep(5)
    except PWTimeout:
        log.warning("LoopNet: timed out — skipping")
        return []

    body = page.content().lower()
    if "access denied" in body or "captcha" in body or "please verify" in body:
        log.warning("LoopNet: blocked by bot protection — skipping")
        return []

    cards = (
        page.query_selector_all(".placard")
        or page.query_selector_all("[class*='listing-card']")
        or page.query_selector_all("[class*='SearchResult']")
        or page.query_selector_all("article")
    )
    log.info(f"LoopNet: {len(cards)} cards found")

    for card in cards:
        try:
            link_el = card.query_selector("a[href]")
            href = (link_el.get_attribute("href") if link_el else "") or ""
            if href and not href.startswith("http"):
                href = f"https://www.loopnet.com{href}"

            name_el = (
                card.query_selector("h2")
                or card.query_selector("h3")
                or card.query_selector("[class*='title']")
                or card.query_selector("[class*='Title']")
            )
            name = (name_el.inner_text().strip() if name_el else "") or "Car Wash Property"

            loc_el = (
                card.query_selector("[class*='address']")
                or card.query_selector("[class*='Address']")
                or card.query_selector("[class*='location']")
            )
            location = loc_el.inner_text().strip() if loc_el else ""

            price_el = (
                card.query_selector("[class*='price']")
                or card.query_selector("[class*='Price']")
                or card.query_selector("[class*='asking']")
            )
            asking_price = price_el.inner_text().strip() if price_el else ""

            # Lot size / acres often visible on commercial real estate cards
            acres = ""
            size_el = card.query_selector("[class*='size'], [class*='Size'], [class*='area']")
            if size_el:
                size_text = size_el.inner_text()
                m = re.search(r'([\d.]+)\s*(?:acre|AC)', size_text, re.IGNORECASE)
                if m:
                    acres = m.group(1)

            state = parse_state(location)
            listing = {
                "name": name,
                "market": location,
                "state": state,
                "asking_price": asking_price,
                "research_url": href,
                "source": "LoopNet",
                "note": f"Auto-scraped from LoopNet on {today}",
                "maps_url": maps_url(location),
            }
            if acres:
                listing["acres"] = acres

            key = dedup_key(listing)
            if key and key not in existing_keys:
                new_listings.append(listing)
                existing_keys.add(key)
                log.info(f"  + {name} — {location}")
            else:
                log.info(f"  - Already have: {name}")

        except Exception as e:
            log.warning(f"  Error parsing LoopNet card: {e}")

    return new_listings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== Car Wash Scout Agent starting ===")

    log.info("Fetching existing listings from site...")
    try:
        existing_keys = get_existing_keys()
        log.info(f"Found {len(existing_keys)} existing listings to compare against")
    except Exception as e:
        log.error(f"Could not fetch existing listings: {e}")
        return

    all_new = []

    with sync_playwright() as p:
        browser, page = new_browser(p)

        # --- BizBuySell: two-pass ---
        log.info("--- BizBuySell: collecting new listings ---")
        stubs = collect_bizbuysell_stubs(page, existing_keys)
        log.info(f"BizBuySell: {len(stubs)} new listings to enrich")

        log.info("--- BizBuySell: enriching with detail page data ---")
        for stub in stubs:
            enriched = enrich_bizbuysell(page, stub)
            all_new.append(enriched)
            time.sleep(1.5)  # polite delay between detail page requests

        time.sleep(5)

        # --- LoopNet: card-level only ---
        log.info("--- LoopNet: collecting listings ---")
        ln = scrape_loopnet(page, existing_keys)
        log.info(f"LoopNet: {len(ln)} new listings found")
        all_new.extend(ln)

        browser.close()

    # --- Post to site ---
    log.info(f"--- Adding {len(all_new)} new listings to site ---")
    added, failed = 0, 0
    for listing in all_new:
        try:
            add_listing(listing)
            log.info(f"Added: {listing['name']} ({listing.get('market', '')})")
            added += 1
            time.sleep(0.3)
        except Exception as e:
            log.error(f"Failed to add '{listing['name']}': {e}")
            failed += 1

    log.info(f"=== Done. {added} added, {failed} failed ===")


if __name__ == "__main__":
    main()
