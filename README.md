# PR Auto-Updater — Setup Guide

## What it does
Runs once a day and:
1. Scrapes every athlete's PRs from **MileSplit** (using your coach's premium login) and **Athletic.net** (public).
2. Picks the fastest time for each event across both sources.
3. Compares against last-known PRs — skips athletes with no changes.
4. Opens a headless browser, logs into **ARMS (Teamworks)**, and writes updated times into the correct field for each athlete's profile.

---

## Installation

### 1. Python 3.11+
```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install playwright requests beautifulsoup4 lxml schedule python-dotenv
playwright install chromium
```

### 2. Create a `.env` file (same directory as the script)
```
MILESPLIT_EMAIL=coach@university.edu
MILESPLIT_PASSWORD=your_milesplit_password
ARMS_EMAIL=coach@university.edu
ARMS_PASSWORD=your_arms_password
```

### 3. Fill out `athletes.csv`
| Column | Description |
|--------|-------------|
| `name` | Full name (First Last) |
| `school` | High school name (used for disambiguation) |
| `state` | Two-letter abbreviation (NC, TX, etc.) |
| `milesplit_id` | Leave blank on first run; auto-filled |
| `athleticnet_id` | Leave blank on first run; auto-filled |

### 4. Edit `event_field_map.json` to match your ARMS field labels exactly
Open an athlete's profile in ARMS, click the PR/Athletic Info tab, and note the exact label text of each field (e.g. "800 Meter PR"). Copy those strings into the JSON file.

---

## Running

### One-time test
```bash
python pr_auto_updater.py
```
The script runs once immediately, then stays alive and runs again at 06:00 every day.

### Headless vs. visible browser
Change `HEADLESS = True` to `HEADLESS = False` in the script to watch the browser while debugging.

### Run in the background (Linux/macOS)
```bash
nohup python pr_auto_updater.py &> updater.log &
```

### Run as a scheduled task (Windows Task Scheduler)
Create a task that runs `python pr_auto_updater.py` at 06:00 daily.
Or use the built-in `schedule` loop — just leave the process running.

---

## How ARMS updating works

ARMS (Teamworks Compliance + Recruiting) has **no public API for writing recruit data**. The script uses **Playwright** — a headless browser automation library — to:

1. Log into `my.armssoftware.com`
2. Search for each athlete by name
3. Open their profile and click to the "Athletic Information" or "Questionnaire" tab
4. Find each PR input field by matching its label text to your `event_field_map.json`
5. Fill in the new time (only if it's faster than the current value)
6. Click Save

This is exactly what a coach does manually — the script just does it automatically.

> **Note:** If Teamworks grants your institution API access (ask your Customer Success Manager), you can replace the Playwright section with direct REST calls to `PATCH /v1/recruits/{id}/custom_fields` for a more stable integration.

---

## Files produced / used

| File | Purpose |
|------|---------|
| `athletes.csv` | Your roster — edit to add/remove recruits |
| `event_field_map.json` | Maps event names to ARMS field labels |
| `last_known_prs.json` | Auto-generated; tracks last seen PRs to avoid redundant updates |
| `pr_updater.log` | Daily log of what was found and changed |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| MileSplit login fails | Verify credentials in `.env`; check if MileSplit added CAPTCHA |
| Athlete not found on MileSplit | Pre-fill the `milesplit_id` column manually (find the number in the URL of their profile) |
| ARMS field not updating | Check that the label in `event_field_map.json` exactly matches the label in ARMS (case-sensitive) |
| Script crashes after many athletes | MileSplit may be rate-limiting; increase `REQUEST_DELAY` to 3–4 seconds |
