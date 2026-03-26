"""
=============================================================================
PR AUTO-UPDATER  |  MileSplit + Athletic.net  →  ARMS (Teamworks)
=============================================================================
Author : generated for track & field recruiting automation
Purpose: Runs once daily (via cron/Task Scheduler). For each athlete in a
         local roster CSV, scrapes their best marks from MileSplit (primary)
         and Athletic.net (fallback/supplement), then writes the updated PRs
         into ARMS via its web UI using Playwright browser automation.

─────────────────────────── ARCHITECTURE OVERVIEW ──────────────────────────

  WHY NOT THE MILESPLIT "API"?
  The old MileSplit developer API (api.milesplit.com) dates from ~2010 and
  its performance endpoints are undocumented / effectively dead. The only
  live data surface is the modern website, which serves athlete pages via
  server-rendered HTML and a few internal JSON XHR calls that we can
  intercept. We therefore treat MileSplit like a website, not an API.

  APPROACH FOR MILESPLIT (with a Premium account):
  1. Log in once with coach's credentials and reuse the session cookie for
     all subsequent requests — avoids repeated logins and mirrors normal
     human browsing.
  2. Search for each athlete by full name + state to get their athlete ID.
  3. Fetch the athlete's profile page and parse the "Personal Records" /
     "Season Bests" table.

  APPROACH FOR ATHLETIC.NET:
  Athletic.net has no public API for reading athlete PRs, but athlete
  profiles are publicly accessible HTML pages. We search by name and school,
  then scrape the "Personal Bests" section. This serves as a supplementary
  source (especially useful for states where athletic.net coverage is
  better than MileSplit).

  ARMS / TEAMWORKS — HOW PR FIELDS WORK:
  ARMS (now "Teamworks Compliance + Recruiting") stores recruits at
  my.armssoftware.com. Each recruit profile has a configurable "Questionnaire"
  / custom fields section. When a coach sets up a T&F recruiting pipeline,
  the standard fields include event-specific PR columns (e.g. "800m PR",
  "Mile PR", "5K XC PR", etc.). There is NO documented public API for
  writing data back.
  
  BEST STRATEGY — PLAYWRIGHT BROWSER AUTOMATION:
  We drive a real (headless) browser session logged into ARMS, navigate to
  each athlete's profile, find the correct input field by label, clear it,
  and type the new time. This is robust because:
    • No API key or undocumented endpoint needed.
    • Works regardless of ARMS version/layout changes (uses visible labels).
    • Completely mimics what a coach does manually — keeps it compliant.
  
  ALTERNATIVE (if your ARMS plan exposes the Teamworks API):
  Teamworks does offer a Partner API. If your institution has API access,
  you can replace the Playwright ARMS section with REST calls to
  PATCH /v1/recruits/{id}/custom_fields. Contact your Teamworks CSM.

─────────────────────────────── PREREQUISITES ───────────────────────────────

  pip install playwright requests beautifulsoup4 lxml schedule python-dotenv
  playwright install chromium

  .env file (same directory as this script):
    MILESPLIT_EMAIL=coach@university.edu
    MILESPLIT_PASSWORD=your_password
    ARMS_EMAIL=coach@university.edu
    ARMS_PASSWORD=your_arms_password

  athletes.csv (columns):
    name, school, state, milesplit_id (optional), athleticnet_id (optional)
    "Jane Doe","Lincoln High School","NC","1234567",""
    "John Smith","Reagan High School","TX","","56789"

  event_field_map.json  (maps event names to ARMS field labels):
    {
  "800 Time (mm:ss:00):"  : "800 Meter PR",
  "1500m Time (mm:ss:00)" : "1500 Meter PR",
  "1600m Time (mm:ss:00)"  : "1600 PR",
  "3000m Time (mm:ss:00)" : "3000 Meter PR",
  "3200m Time (mm:ss:00)" : "3200 Meter PR",
  "5000m XC Time (mm:ss:00)" : "XC 5000 Meter PR",
  "3 mile XC Time (mm:ss:00)" : "XC 3 mile PR",
  "100m Time (mm:ss:00)" : "100m PR",
  "200m Time (mm:ss:00)" : "200m PR",
  "400m Time (mm:ss:00)": "400 Meter PR"
}

═════════════════════════════════════════════════════════════════════════════
"""

import os
import re
import csv
import json
import time
import logging
import datetime
import schedule
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ──────────────────────────────────────────────────────────────────
load_dotenv()

BASE_DIR      = Path(__file__).parent
#ATHLETES_CSV  = BASE_DIR / "athletes.csv"
EVENT_MAP     = BASE_DIR / "event_field_map.json"
LOG_FILE      = BASE_DIR / "pr_updater.log"
STATE_FILE = BASE_DIR / "roster_cache.json"   # Replaces last_known_prs.json

MILESPLIT_EMAIL    = os.getenv("MILESPLIT_EMAIL")
MILESPLIT_PASSWORD = os.getenv("MILESPLIT_PASSWORD")
ARMS_EMAIL         = os.getenv("ARMS_EMAIL")
ARMS_PASSWORD      = os.getenv("ARMS_PASSWORD")

MILESPLIT_BASE  = "https://www.milesplit.com"
ATHLETICNET_BASE = "https://www.athletic.net"

REQUEST_DELAY = 1.5   # seconds between HTTP requests (polite scraping)
HEADLESS      = True  # set False to watch the browser while debugging

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("pr_updater")

# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def load_athletes() -> list[dict]:
    """Read the athlete roster CSV and return a list of dicts."""
    athletes = []
    with open(ATHLETES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["name"]             = row["name"].strip()
            row["school"]           = row["school"].strip()
            row["state"]            = row["state"].strip().upper()
            row["milesplit_id"]     = row.get("milesplit_id", "").strip()
            row["athleticnet_id"]   = row.get("athleticnet_id", "").strip()
            athletes.append(row)
    log.info(f"Loaded {len(athletes)} athletes from {ATHLETES_CSV}")
    return athletes


def load_event_map() -> dict:
    with open(EVENT_MAP, encoding="utf-8") as f:
        return json.load(f)


def load_known_prs() -> dict:
    """Load previously recorded PRs so we can skip unchanged athletes."""
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_known_prs(data: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def clean_time_string(t: str) -> str:
    """
    Strips common T&F annotations: c (converted), h (hand), w (wind-aided), 
    PR, SR, and wind readings in parentheses (e.g., '(2.1)').
    """
    # Remove anything in parentheses (like wind readings)
    t = re.sub(r"\(.*?\)", "", t)
    
    # Extract only the time format (digits, colons, decimals)
    # This automatically drops 'c', 'h', 'w', 'PR', etc.
    match = re.search(r"(\d{1,2}:\d{2}\.\d{1,2}|\d{1,3}\.\d{1,2}|\d{1,3})", t)
    if match:
        return match.group(1)
    
    return t.strip()

def time_to_seconds(t: str) -> float:
    """Convert a sanitized time string to float seconds."""
    t = clean_time_string(t)
    try:
        if ":" in t:
            parts = t.split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        return float(t)
    except ValueError:
        # If it's a completely invalid time (e.g., "DNF", "DQ", "NH"),
        # return infinity so it never counts as a "faster" PR.
        return float('inf')


def is_faster(new_time: str, old_time: str) -> bool:
    """Return True if new_time is strictly faster (lower) than old_time."""
    try:
        return time_to_seconds(new_time) < time_to_seconds(old_time)
    except (ValueError, TypeError):
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  MILESPLIT SCRAPER
#  We use requests + BeautifulSoup for reading (fast, lightweight).
#  Authentication is done once; the session cookie is reused.
# ─────────────────────────────────────────────────────────────────────────────

class MileSplitScraper:
    """
    Scrapes personal records from MileSplit for a given athlete.

    Login flow:
      POST https://www.milesplit.com/users/login  with _method=POST,
      email=..., password=...  → sets session cookies.

    Athlete search:
      GET https://www.milesplit.com/api/v1/athletes/search
          ?name=Jane+Doe&state=NC
      Returns JSON {data: [{id, firstName, lastName, teamName, ...}]}

    Athlete PR page:
      GET https://www.milesplit.com/athletes/{id}/performances
      Parse the HTML table with class="tablesaw" or similar.
    """

    LOGIN_URL  = f"{MILESPLIT_BASE}/users/login"
    SEARCH_URL = f"{MILESPLIT_BASE}/api/v1/athletes"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Referer": MILESPLIT_BASE,
        })
        self._logged_in = False

    def login(self):
        """Authenticate with MileSplit and store session cookie."""
        log.info("Logging into MileSplit…")
        # First GET the login page to capture any CSRF token
        resp = self.session.get(self.LOGIN_URL)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # MileSplit uses a hidden _token / CSRF field in the login form
        token_tag = soup.find("input", {"name": "_token"})
        csrf = token_tag["value"] if token_tag else ""

        payload = {
            "_method"  : "POST",
            "_token"   : csrf,
            "email"    : MILESPLIT_EMAIL,
            "password" : MILESPLIT_PASSWORD,
        }
        resp = self.session.post(self.LOGIN_URL, data=payload, allow_redirects=True)
        if "logout" in resp.text.lower() or "my account" in resp.text.lower():
            log.info("MileSplit login successful.")
            self._logged_in = True
        else:
            log.warning("MileSplit login may have failed — check credentials.")
        time.sleep(REQUEST_DELAY)

    def find_athlete_id(self, name: str, state: str, school: str) -> str | None:
        """
        Search MileSplit for an athlete. Returns the athlete ID string or None.
        Strategy: use the internal search endpoint, filter by state, then fuzzy-
        match on school name to pick the right athlete if there are duplicates.
        """
        if not self._logged_in:
            self.login()

        first, *rest = name.split()
        last = " ".join(rest) if rest else ""

        params = {
            "search[firstName]": first,
            "search[lastName]" : last,
            "search[state]"    : state,
            "page"             : 1,
            "per_page"         : 10,
        }
        try:
            resp = self.session.get(self.SEARCH_URL, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except Exception as e:
            log.warning(f"MileSplit search failed for {name}: {e}")
            time.sleep(REQUEST_DELAY)
            return None

        time.sleep(REQUEST_DELAY)

        for athlete in data:
            team = athlete.get("teamName", "")
            # Prefer exact school match; fall back to first result in state
            if school.lower() in team.lower() or team.lower() in school.lower():
                log.info(f"  Found MileSplit ID {athlete['id']} for {name} ({team})")
                return str(athlete["id"])

        # If no school match but exactly one result, use it
        if len(data) == 1:
            log.info(f"  Using sole result {data[0]['id']} for {name}")
            return str(data[0]["id"])

        log.warning(f"  Could not uniquely identify {name} on MileSplit (got {len(data)} results)")
        return None

    def get_prs(self, athlete_id: str) -> dict[str, str]:
        """
        Fetch the athlete's performance page and parse personal records.
        Returns {event_name: best_time_string}.

        The MileSplit performances page lists events in a table; we look for
        the "Personal Best" column. The HTML structure:
          <table class="athletePerformances ...">
            <tr><td class="event">800m</td> <td class="personalBest">2:03.12</td> ...
        Because this layout can change, we fall back to a regex sweep if
        the structured parse doesn't find expected columns.
        """
        url = f"{MILESPLIT_BASE}/athletes/{athlete_id}/performances"
        try:
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            log.warning(f"  Could not fetch performances for ID {athlete_id}: {e}")
            time.sleep(REQUEST_DELAY)
            return {}

        time.sleep(REQUEST_DELAY)
        return self._parse_performance_page(resp.text)

    def _parse_performance_page(self, html: str) -> dict[str, str]:
        """
        Parse the MileSplit performance page HTML.
        Returns {event: best_time}.
        """
        soup = BeautifulSoup(html, "lxml")
        prs = {}

        # ── Strategy 1: look for a table with "event" and "personal best" columns
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not headers:
                continue
            
            event_col = next((i for i, h in enumerate(headers) if "event" in h), None)
            pr_col    = next((i for i, h in enumerate(headers)
                              if "personal" in h or "best" in h or "pr" in h), None)
            
            if event_col is None or pr_col is None:
                continue

            for row in table.find_all("tr")[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) <= max(event_col, pr_col):
                    continue
                event = cells[event_col].get_text(strip=True)
                time_ = cells[pr_col].get_text(strip=True)
                # Replace this: if event and re.match(r"[\d:.]", time_):
                if event and re.search(r"\d", time_):  # Just ensure there's a number in the string
                    prs[event] = time_

        if prs:
            return prs

        # ── Strategy 2: regex sweep for common event/time patterns
        # The optional [a-zA-Z\s]* catches things like 'c', 'h', ' PR'
        pattern = re.compile(
            r"(100m?|200m?|400m?|800m?|1500m?|1600m?|Mile|3000m?|3200m?|3\s*Mile|5000m?|5K)"
            r".*?(\d{1,2}:\d{2}\.\d{1,2}[a-zA-Z\s]*|\d{1,3}\.\d{1,2}[a-zA-Z\s]*)",
            re.IGNORECASE | re.DOTALL,
        )
        for m in pattern.finditer(soup.get_text()):
            event = m.group(1).strip()
            t     = m.group(2).strip()
            if event not in prs:
                prs[event] = t

        return prs


# ─────────────────────────────────────────────────────────────────────────────
#  ATHLETIC.NET SCRAPER
# ─────────────────────────────────────────────────────────────────────────────

class AthleticNetScraper:
    """
    Scrapes personal records from Athletic.net.

    Athletic.net athlete URLs follow this pattern:
      https://www.athletic.net/TrackAndField/Athlete/{id}/
    Search is done via:
      https://www.athletic.net/Search.aspx?q=Jane+Doe+NC

    No authentication required for public profile data.
    """

    SEARCH_URL = f"{ATHLETICNET_BASE}/Search.aspx"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        })

    def find_athlete_id(self, name: str, state: str, school: str) -> str | None:
        """Search athletic.net and return athlete ID or None."""
        query = f"{name} {state}"
        try:
            resp = self.session.get(
                self.SEARCH_URL,
                params={"q": query},
                timeout=10,
            )
            resp.raise_for_status()
        except Exception as e:
            log.warning(f"  Athletic.net search failed for {name}: {e}")
            time.sleep(REQUEST_DELAY)
            return None

        time.sleep(REQUEST_DELAY)
        soup = BeautifulSoup(resp.text, "lxml")

        # Athletic.net search returns athlete cards; find the one matching school
        for card in soup.select(".athleteSearchResult, .search-result"):
            card_text = card.get_text(" ", strip=True)
            link = card.find("a", href=re.compile(r"/TrackAndField/Athlete/\d+"))
            if not link:
                link = card.find("a", href=re.compile(r"/CrossCountry/Athlete/\d+"))
            if link and (school.lower() in card_text.lower()
                         or state.lower() in card_text.lower()):
                m = re.search(r"/Athlete/(\d+)", link["href"])
                if m:
                    log.info(f"  Found Athletic.net ID {m.group(1)} for {name}")
                    return m.group(1)

        return None

    def get_prs(self, athlete_id: str, sport: str = "TrackAndField") -> dict[str, str]:
        """
        Fetch PR data from an athletic.net athlete page.
        sport: "TrackAndField" or "CrossCountry"
        """
        url = f"{ATHLETICNET_BASE}/{sport}/Athlete/{athlete_id}/"
        try:
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            log.warning(f"  Athletic.net fetch failed for ID {athlete_id}: {e}")
            time.sleep(REQUEST_DELAY)
            return {}

        time.sleep(REQUEST_DELAY)
        return self._parse_profile(resp.text)

    def _parse_profile(self, html: str) -> dict[str, str]:
        """Parse Athletic.net profile page for personal bests."""
        soup = BeautifulSoup(html, "lxml")
        prs = {}

        # Athletic.net uses a "prs" section with event + best time
        for section in soup.select(".prs, #prs, .personal-bests, [data-event]"):
            rows = section.find_all("tr") or section.find_all("li")
            for row in rows:
                cells = row.find_all(["td", "span"])
                if len(cells) >= 2:
                    event = cells[0].get_text(strip=True)
                    time_ = cells[1].get_text(strip=True)
                    # Replace this: if event and re.match(r"[\d:.]", time_):
                    if event and re.search(r"\d", time_): 
                        prs[event] = time_

        # Fallback: look for event/time pairs in any table on the page
        if not prs:
            for table in soup.find_all("table"):
                for row in table.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        event = cells[0].get_text(strip=True)
                        time_ = cells[1].get_text(strip=True)
                        # Fallback: look for event/time pairs in any table on the page
                        if re.match(r"(100|200|400|800|1500|1600|Mile|3000|3200|3\s*Mile|5[Kk]|5000)", event, re.I):
                            # Replace this: if re.match(r"[\d:.]", time_):
                        if re.search(r"\d", time_):
                            prs[event] = time_
        return prs


# ─────────────────────────────────────────────────────────────────────────────
#  PR MERGER
#  Combines MileSplit + Athletic.net results, picking the best (fastest) time
#  for each event. Normalizes event names to a canonical set.
# ─────────────────────────────────────────────────────────────────────────────

# Canonical event name → list of aliases found on various sites
EVENT_ALIASES = {
    "100m Time (mm:ss:00)": ["100", "100m", "100 Meter", "100 Meters", "100 Dash"],
    "200m Time (mm:ss:00)": ["200", "200m", "200 Meter", "200 Meters", "200 Dash"],
    "400m Time (mm:ss:00)": ["400", "400m", "400 Meter", "400 Meters", "400 Dash"],
    "800 Time (mm:ss:00):": ["800", "800m", "800 Meter", "800 Meters", "800 Run"],
    "1500m Time (mm:ss:00)": ["1500", "1500m", "1500 Meter", "1.5K"],
    "1600m Time (mm:ss:00)": ["1600", "1600m", "1600 Meter", "Mile", "1 Mile"],
    "3000m Time (mm:ss:00)": ["3000", "3000m", "3K", "3 Kilometer"],
    "3200m Time (mm:ss:00)": ["3200", "3200m", "2 Mile", "Two Mile"],
    "3 mile XC Time (mm:ss:00)": ["3 Mile", "3M", "3 Mile XC", "3 Miles", "XC 3 Mile"],
    "5000m XC Time (mm:ss:00)": ["5000", "5000m", "5K", "5K XC", "5000 XC", "XC 5K", "5K TF"]
}

def normalize_event(raw: str) -> str | None:
    """Map a raw event name from a website to a canonical name, or None."""
    raw = raw.strip()
    for canonical, aliases in EVENT_ALIASES.items():
        for alias in aliases:
            if alias.lower() == raw.lower():
                return canonical
    return None


def merge_prs(ms_prs: dict, an_prs: dict) -> dict:
    """
    Merge two {raw_event: time} dicts into one {canonical_event: best_time}.
    Picks the faster of the two times for each event.
    """
    merged = {}

    for raw, t in {**ms_prs, **an_prs}.items():
        canon = normalize_event(raw)
        if canon is None:
            continue
        if canon not in merged:
            merged[canon] = t
        elif is_faster(t, merged[canon]):
            merged[canon] = t

    return merged


# ─────────────────────────────────────────────────────────────────────────────
#  ARMS UPDATER  (Playwright browser automation)
#
#  ARMS is at my.armssoftware.com.  The recruiting module stores each recruit
#  as a profile. Custom fields (where PRs live) are typically under a section
#  like "Athletic Information" or "Track & Field PRs" depending on how your
#  coach has configured the questionnaire template.
#
#  We:
#    1. Log in once with Playwright and keep the browser context alive.
#    2. Search for the recruit by name.
#    3. Navigate to their profile → "Questionnaire" or "Athletic Info" tab.
#    4. For each event with a new PR, find the matching input by its field
#       label, clear it, and type the new value.
#    5. Save / submit.
# ─────────────────────────────────────────────────────────────────────────────

class ARMSUpdater:

    ARMS_URL   = "https://my.armssoftware.com/arms/login"
    SEARCH_URL = "https://my.armssoftware.com/arms/recruiting/recruits"

    def __init__(self, playwright_instance):
        self.pw      = playwright_instance
        self.browser = playwright_instance.chromium.launch(headless=HEADLESS)
        self.context = self.browser.new_context()
        self.page    = self.context.new_page()
        self._logged_in = False

    def login(self):
        log.info("Logging into ARMS…")
        self.page.goto(self.ARMS_URL)
        self.page.fill("input[name='email'], input[type='email']", ARMS_EMAIL)
        self.page.fill("input[name='password'], input[type='password']", ARMS_PASSWORD)
        self.page.click("button[type='submit'], input[type='submit']")
        # Wait for the dashboard to load
        try:
            self.page.wait_for_url("**/dashboard**", timeout=15000)
            log.info("ARMS login successful.")
            self._logged_in = True
        except PWTimeout:
            log.warning("ARMS login timed out; may not be logged in correctly.")

    def get_active_recruits(self) -> list[dict]:
        """
        Scrapes the active recruit roster directly from the ARMS dashboard.
        Returns a list of dicts: [{name, school, state}]
        """
        if not self._logged_in:
            self.login()

        log.info("Fetching recruit roster directly from ARMS...")
        
        # Go to the recruit view that shows the table of all active recruits
        self.page.goto(self.SEARCH_URL)
        self.page.wait_for_load_state("networkidle", timeout=15000)

        athletes = []

        # ⚠️ CRITICAL: These CSS selectors are placeholders! 
        # You MUST inspect the ARMS Recruits page to find the actual class names 
        # or data-attributes for the table rows and data cells.
        
        # Example: if ARMS uses <tr class="recruit-row">, this would be "tr.recruit-row"
        row_selector = "tr.recruit-row" 
        
        # Optional: Handle Pagination if ARMS splits recruits across multiple pages
        while True:
            rows = self.page.locator(row_selector)
            count = rows.count()
            
            for i in range(count):
                row = rows.nth(i)
                
                # Replace these selectors with the specific classes for each column
                try:
                    name = row.locator(".col-fullname").inner_text().strip()
                    school = row.locator(".col-highschool").inner_text().strip()
                    state = row.locator(".col-state").inner_text().strip()
                    
                    if name:
                        athletes.append({
                            "name": name,
                            "school": school,
                            "state": state
                        })
                except Exception as e:
                    log.debug(f"Skipped a row due to missing data/formatting: {e}")

            # Pagination logic: Check if there's a "Next Page" button
            next_btn = self.page.locator("button.next-page") # Placeholder selector
            if next_btn.is_visible() and next_btn.is_enabled():
                next_btn.click()
                self.page.wait_for_load_state("networkidle")
            else:
                break # Reached the last page

        log.info(f"Successfully pulled {len(athletes)} recruits from ARMS.")
        return athletes     

    def find_recruit(self, name: str) -> bool:
        """
        Navigate to the recruits list and open a specific athlete's profile.
        Returns True if found and page is now on their profile.
        """
        if not self._logged_in:
            self.login()

        # Go to the recruits list and use the search box
        self.page.goto(self.SEARCH_URL)
        try:
            self.page.wait_for_selector("input[placeholder*='Search'], input[placeholder*='search']",
                                        timeout=8000)
        except PWTimeout:
            log.warning(f"  Recruit search box not found for {name}")
            return False

        search_box = self.page.locator("input[placeholder*='Search'], input[placeholder*='search']").first
        search_box.fill(name)
        search_box.press("Enter")
        time.sleep(1.5)

        # Click the first result that matches the name
        try:
            # ARMS typically shows results as clickable rows or cards
            result = self.page.locator(
                f"text={name.split()[0]}"  # match first name to be safe
            ).first
            result.click()
            self.page.wait_for_load_state("networkidle", timeout=8000)
            log.info(f"  Opened ARMS profile for {name}")
            return True
        except Exception as e:
            log.warning(f"  Could not open ARMS profile for {name}: {e}")
            return False

    def update_prs(self, name: str, prs: dict[str, str], event_map: dict):
        """
        Given a dict of {canonical_event: time}, write each value into the
        corresponding ARMS field.

        event_map: {canonical_event: ARMS field label}
        e.g. {"800m": "800 Meter PR", "Mile": "Mile PR"}
        """
        if not self.find_recruit(name):
            log.warning(f"  Skipping ARMS update for {name} — profile not found.")
            return

        # Navigate to the tab that holds PR fields
        # Common tab names in ARMS: "Questionnaire", "Athletic Info", "Profile"
        for tab_label in ["Athletic Information", "Questionnaire", "Track & Field", "Profile"]:
            tab = self.page.locator(f"text={tab_label}").first
            if tab.is_visible():
                tab.click()
                time.sleep(0.8)
                break

        updated_count = 0
        for canonical_event, new_time in prs.items():
            arms_label = event_map.get(canonical_event)
            if not arms_label:
                log.debug(f"    No ARMS field mapping for event '{canonical_event}', skipping.")
                continue

            # Find the input field by its associated label
            try:
                # Try label[for] association first
                label = self.page.locator(f"label:has-text('{arms_label}')").first
                input_id = label.get_attribute("for")
                if input_id:
                    field = self.page.locator(f"#{input_id}")
                else:
                    # Fallback: sibling input next to the label
                    field = label.locator("xpath=following-sibling::input[1]")

                current_val = field.input_value()
                if current_val == new_time:
                    log.debug(f"    {arms_label}: already {new_time}, no update needed.")
                    continue

                # Only update if it's a new PR (faster time)
                if current_val and not is_faster(new_time, current_val):
                    log.debug(f"    {arms_label}: existing {current_val} >= new {new_time}, skipping.")
                    continue

                field.triple_click()  # select all
                field.fill(new_time)
                log.info(f"    Updated {arms_label}: {current_val or 'empty'} → {new_time}")
                updated_count += 1

            except Exception as e:
                log.warning(f"    Could not update '{arms_label}' for {name}: {e}")

        if updated_count > 0:
            # Click Save button
            try:
                save_btn = self.page.locator("button:has-text('Save'), input[value='Save']").first
                save_btn.click()
                self.page.wait_for_load_state("networkidle", timeout=6000)
                log.info(f"  Saved {updated_count} field(s) for {name}.")
            except Exception as e:
                log.warning(f"  Save button not found / failed for {name}: {e}")
        else:
            log.info(f"  No fields needed updating for {name}.")

    def close(self):
        self.browser.close()


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

def run_update():
    log.info("=" * 60)
    log.info(f"PR update run started  {datetime.datetime.now():%Y-%m-%d %H:%M}")
    log.info("=" * 60)

    event_map = load_event_map()
    
    # roster_cache structure: { "Jane Doe": {"ms_id": "123", "an_id": "456", "prs": {"1600m": "4:50.12"}} }
    roster_cache = load_known_prs() 

    # Phase 1: Get the current roster from ARMS
    with sync_playwright() as pw:
        arms = ARMSUpdater(pw)
        arms.login()
        arms_roster = arms.get_active_recruits()
        
        # If ARMS scraping fails, abort so we don't wipe our cache
        if not arms_roster:
            log.error("Failed to pull roster from ARMS. Aborting run.")
            arms.close()
            return

    # Phase 2: Scrape PR data
    ms_scraper = MileSplitScraper()
    ms_scraper.login()
    an_scraper = AthleticNetScraper()

    all_updates = {}  # Only stores athletes with new PRs that need to be sent back to ARMS

    for athlete in arms_roster:
        name   = athlete["name"]
        state  = athlete["state"]
        school = athlete["school"]
        log.info(f"Processing: {name} | {school} | {state}")

        # Initialize this athlete in the cache if they are new from ARMS
        if name not in roster_cache:
            roster_cache[name] = {"ms_id": "", "an_id": "", "prs": {}}

        cached_data = roster_cache[name]

        # ── MileSplit ────────────────────────────────────────────────────────
        ms_id = cached_data.get("ms_id") or ms_scraper.find_athlete_id(name, state, school)
        ms_prs = {}
        if ms_id:
            ms_prs = ms_scraper.get_prs(ms_id)
            cached_data["ms_id"] = ms_id  # Save to cache so we don't search next time
        else:
            log.warning(f"  No MileSplit ID found for {name}")

        # ── Athletic.net ─────────────────────────────────────────────────────
        an_id = cached_data.get("an_id") or an_scraper.find_athlete_id(name, state, school)
        an_prs = {}
        if an_id:
            an_prs = an_scraper.get_prs(an_id)
            cached_data["an_id"] = an_id
        else:
            log.info(f"  No Athletic.net ID found for {name}")

        # ── Merge and compare ────────────────────────────────────────────────
        combined = merge_prs(ms_prs, an_prs)
        log.info(f"  Combined PRs: {combined}")

        prev_prs = cached_data.get("prs", {})
        new_or_improved = {
            event: t for event, t in combined.items()
            if event not in prev_prs or is_faster(t, prev_prs[event])
        }

        if new_or_improved:
            log.info(f"  NEW / IMPROVED PRs for {name}: {new_or_improved}")
            all_updates[name] = new_or_improved
            
            # Update the cache with the new best times
            for event, t in combined.items():
                if event not in prev_prs or is_faster(t, prev_prs[event]):
                    cached_data["prs"][event] = t
        else:
            log.info(f"  No new PRs for {name}.")

    # Save IDs and PRs permanently to disk
    save_known_prs(roster_cache)

    # Phase 3: Write updates back into ARMS
    if not all_updates:
        log.info("No athletes with new PRs — skipping ARMS update.")
        log.info("Run complete.")
        return

    log.info(f"\nWriting updates to ARMS for {len(all_updates)} athlete(s)…")
    with sync_playwright() as pw:
        arms = ARMSUpdater(pw)
        arms.login()
        for name, updates in all_updates.items():
            arms.update_prs(name, updates, event_map)
        arms.close()

    log.info("Run complete.")


# ─────────────────────────────────────────────────────────────────────────────
#  SCHEDULER  —  run once daily at 06:00 (adjustable)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Uncomment to run immediately on launch (good for first test):
    run_update()

    # Schedule daily at 6 AM
    schedule.every().day.at("06:00").do(run_update)
    log.info("Scheduler started — will run daily at 06:00. Press Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)
