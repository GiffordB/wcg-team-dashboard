"""
Fetches the World Class Gymnastics optional team's scores from
mymeetscores.com and saves them into team.db, so the coach's dashboard
always has fresh data without anyone typing scores in by hand.

Run it by hand any time:
    ./venv/bin/python scraper.py --force

Two things happen on every run:
  1. Roster bootstrap - fetch the team's roster page (one request) and
     auto-add any gymnast competing at an optional level (see
     gymdata.OPTIONAL_LEVELS) who's had a meet within the last ~15 months,
     if she isn't already in the `gymnasts` table. This only ever ADDS -
     it never removes or reactivates anyone, so a gymnast the coach has
     taken off the roster (see app.py's Manage Roster page) stays off even
     though her old meets still show up on the team page. A gymnast who's
     joined but hasn't competed yet won't appear on the team page at all
     (mymeetscores only lists gymnasts with at least one recorded score),
     so she has to be added by hand on that same page.
  2. Per-gymnast scrape - for every currently active gymnast, fetch her own
     results page (same page structure the original single-gymnast version
     of this dashboard used) and upsert her full meet history.

In production this runs on a schedule via the app's own `/internal/scrape`
route (see README.md), hit by a free external scheduler at both 12:00 and
13:00 UTC and both 21:00 and 22:00 UTC, Friday/Saturday/Sunday, October-June
- those four UTC times cover 8:00am and 5:00pm America/New_York across both
sides of the March daylight-saving switch, and every run checks the
*current* Eastern clock so only two of the four firings ever actually
scrape (the DST-safe window check below), no mid-season schedule edit
needed. The month/weekday/hour check happens here in code, not just in
however the external scheduler is configured, so a broader schedule just
costs a few no-op HTTP hits instead of scraping outside the real season.

Politeness: with ~40+ athletes this now adds up to more requests per run
than the original single-gymnast version, so each gymnast fetch is spaced
out by a short delay (see REQUEST_DELAY_SECONDS) rather than firing them
all at once. It identifies itself with a real contact address in the
User-Agent rather than pretending to be a browser or a search engine.
"""

import argparse
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from gymdata import OPTIONAL_LEVELS, TEAM_URL, get_db, gymnast_url, init_db

USER_AGENT = (
    "TeamGymnasticsDashboard/1.0 "
    "(coach roster tracker for one gym; non-commercial; contact: giffordb@mac.com)"
)

LOCAL_TZ = ZoneInfo("America/New_York")
SCRAPE_TIMES = [(8, 0), (17, 0)]  # 8:00am and 5:00pm local
SCRAPE_WINDOW_MINUTES = 20
SCRAPE_MONTHS = {10, 11, 12, 1, 2, 3, 4, 5, 6}  # October - June
SCRAPE_WEEKDAYS = {4, 5, 6}  # Mon=0 ... Fri=4, Sat=5, Sun=6

ROSTER_LOOKBACK_DAYS = 455  # ~15 months - covers a full prior season
REQUEST_DELAY_SECONDS = 1.0


def in_scrape_window(now=None):
    """True if it's currently a legitimate scrape time: October-June,
    Friday/Saturday/Sunday, and within 20 minutes of 8am or 5pm
    America/New_York (see module docstring)."""
    now = now or datetime.now(LOCAL_TZ)
    if now.month not in SCRAPE_MONTHS or now.weekday() not in SCRAPE_WEEKDAYS:
        return False
    for hour, minute in SCRAPE_TIMES:
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if abs((now - target).total_seconds()) <= SCRAPE_WINDOW_MINUTES * 60:
            return True
    return False


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_score_cell(text):
    """'9.225 14T' -> (9.225, '14T'). Missing score -> (None, None)."""
    text = text.strip()
    if not text:
        return None, None
    parts = text.split(None, 1)
    try:
        score = float(parts[0])
    except ValueError:
        return None, None
    place = parts[1].strip() if len(parts) > 1 else None
    return score, place or None


def find_table_by_title(soup, title):
    for table in soup.find_all("table"):
        title_row = table.find("tr", class_=lambda c: c and "GymTableTitle" in c)
        if title_row and title_row.get_text(strip=True) == title:
            return table
    return None


def parse_gymnast_header(soup):
    header_cell = soup.find("td", class_=lambda c: c and "GymHeader" in c)
    if not header_cell:
        return None, None
    team_link = header_cell.find("a")
    team = team_link.get_text(strip=True) if team_link else None
    name = None
    br = header_cell.find("br")
    if br and br.previous_sibling:
        name = str(br.previous_sibling).strip()
    return name, team


def parse_meets(soup, gymnast_id):
    table = find_table_by_title(soup, "Meet Scores")
    if not table:
        return []

    meets = []
    for row in table.find_all("tr", class_=lambda c: c and "GymTableCellTextSm" in c):
        # recursive=False guards against pages (like the team roster) where
        # missing </tr> tags nest every later row inside the first one.
        cells = row.find_all("td", recursive=False)
        if len(cells) != 11:
            continue
        (
            date_cell,
            meet_cell,
            team_cell,
            session_cell,
            level_cell,
            division_cell,
            vault_cell,
            bars_cell,
            beam_cell,
            floor_cell,
            aa_cell,
        ) = cells

        meet_link = meet_cell.find("a")
        meet_url = meet_link.get("href") if meet_link else None
        meet_id = None
        if meet_url:
            query = urllib.parse.urlparse(meet_url).query
            meet_id = urllib.parse.parse_qs(query).get("meetid", [None])[0]

        vault, vault_place = parse_score_cell(vault_cell.get_text())
        bars, bars_place = parse_score_cell(bars_cell.get_text())
        beam, beam_place = parse_score_cell(beam_cell.get_text())
        floor, floor_place = parse_score_cell(floor_cell.get_text())
        aa, aa_place = parse_score_cell(aa_cell.get_text())

        level = level_cell.get_text(strip=True)
        if not meet_id:
            # Fall back to a stable-ish synthetic key so re-scrapes still
            # de-dupe even on the rare row with no meet link.
            meet_id = f"{date_cell.get_text(strip=True)}::{meet_cell.get_text(strip=True)}::{level}"

        meets.append(
            {
                "gymnast_id": gymnast_id,
                "meet_id": meet_id,
                "meet_date": date_cell.get_text(strip=True),
                "meet_name": meet_cell.get_text(strip=True),
                "meet_url": meet_url,
                "team": team_cell.get_text(strip=True),
                "session": session_cell.get_text(strip=True),
                "level": level,
                "division": division_cell.get_text(strip=True),
                "vault": vault, "vault_place": vault_place,
                "bars": bars, "bars_place": bars_place,
                "beam": beam, "beam_place": beam_place,
                "floor": floor, "floor_place": floor_place,
                "aa": aa, "aa_place": aa_place,
            }
        )
    return meets


def parse_team_roster(soup):
    """Every gymnast mymeetscores has ever recorded a score for on this
    team, one row each: name, level, most recent meet date, and AA. This
    intentionally doesn't try to find "the roster table" by position -
    the page's malformed HTML (missing </tr> tags) makes later rows nest
    inside earlier ones, so a table-index lookup is fragile. Instead it
    scans every GymTableCellTextSm(2) row in the whole document and keeps
    only the ones shaped like a roster row (8 direct cells, first one a
    link to a gymnast page) - that shape doesn't occur anywhere else on
    the page regardless of how the nesting falls."""
    candidates = []
    for row in soup.find_all("tr", class_=lambda c: c and "GymTableCellTextSm" in c):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 8:
            continue
        name_cell = cells[0]
        link = name_cell.find("a", recursive=False)
        if not link or "gymnast.pl" not in (link.get("href") or ""):
            continue
        query = urllib.parse.urlparse(link["href"]).query
        gymnast_id = urllib.parse.parse_qs(query).get("gymnastid", [None])[0]
        if not gymnast_id:
            continue
        candidates.append(
            {
                "gymnast_id": gymnast_id,
                "name": name_cell.get_text(strip=True),
                "level": cells[1].get_text(strip=True),
                "latest_meet_date": cells[2].get_text(strip=True),
            }
        )
    return candidates


def bootstrap_roster():
    """Auto-add newly-appearing optional-level, recently-active gymnasts
    from the team roster page. Never removes or reactivates anyone -
    existing rows (active or not) are left exactly as they are."""
    html = fetch_html(TEAM_URL)
    soup = BeautifulSoup(html, "html.parser")
    candidates = parse_team_roster(soup)

    cutoff = (date.today() - timedelta(days=ROSTER_LOOKBACK_DAYS)).isoformat()
    now = datetime.utcnow().isoformat(timespec="seconds")

    db = get_db()
    added = 0
    for c in candidates:
        if c["level"] not in OPTIONAL_LEVELS or c["latest_meet_date"] < cutoff:
            continue
        exists = db.execute("SELECT 1 FROM gymnasts WHERE id = ?", (c["gymnast_id"],)).fetchone()
        if exists:
            continue
        db.execute(
            """
            INSERT INTO gymnasts (id, name, level, source, active, added_at)
            VALUES (?, ?, ?, 'auto', 1, ?)
            """,
            (c["gymnast_id"], c["name"], c["level"], now),
        )
        added += 1
    db.commit()
    db.close()
    return added


def scrape_gymnast(gymnast_id):
    """Fetch and save one gymnast's full meet history. Returns the number
    of meets found (not just newly-added - re-scraping the same meets is
    normal and expected)."""
    html = fetch_html(gymnast_url(gymnast_id))
    soup = BeautifulSoup(html, "html.parser")
    name, team = parse_gymnast_header(soup)
    meets = parse_meets(soup, gymnast_id)

    now = datetime.utcnow().isoformat(timespec="seconds")
    db = get_db()

    if name:
        # `meets` is in whatever order the page displayed (newest-first, in
        # practice) - not guaranteed, so pick the level by max date rather
        # than assuming a position.
        current_level = max(meets, key=lambda m: m["meet_date"])["level"] if meets else None
        db.execute(
            """
            UPDATE gymnasts SET name = ?, team = ?,
                level = COALESCE(?, level)
            WHERE id = ?
            """,
            (name, team, current_level, gymnast_id),
        )

    for meet in meets:
        db.execute(
            """
            INSERT INTO meets (
                gymnast_id, meet_id, meet_date, meet_name, meet_url, team,
                session, level, division,
                vault, vault_place, bars, bars_place,
                beam, beam_place, floor, floor_place, aa, aa_place,
                scraped_at
            ) VALUES (
                :gymnast_id, :meet_id, :meet_date, :meet_name, :meet_url, :team,
                :session, :level, :division,
                :vault, :vault_place, :bars, :bars_place,
                :beam, :beam_place, :floor, :floor_place, :aa, :aa_place,
                :scraped_at
            )
            ON CONFLICT(gymnast_id, meet_id) DO UPDATE SET
                vault = excluded.vault, vault_place = excluded.vault_place,
                bars = excluded.bars, bars_place = excluded.bars_place,
                beam = excluded.beam, beam_place = excluded.beam_place,
                floor = excluded.floor, floor_place = excluded.floor_place,
                aa = excluded.aa, aa_place = excluded.aa_place,
                scraped_at = excluded.scraped_at
            """,
            {**meet, "scraped_at": now},
        )

    db.commit()
    db.close()
    return len(meets)


def log_run(status, gymnasts_scraped=None, meets_found=None, message=None):
    db = get_db()
    db.execute(
        """
        INSERT INTO scrape_log (ran_at, status, gymnasts_scraped, meets_found, message)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            datetime.utcnow().isoformat(timespec="seconds"),
            status,
            gymnasts_scraped,
            meets_found,
            message,
        ),
    )
    db.commit()
    db.close()


def run(force=False):
    init_db()

    if not force and not in_scrape_window():
        print("Outside the Fri/Sat/Sun 8am/5pm America/New_York, Oct-June scrape window - skipping (use --force to override).")
        return 0

    try:
        added = bootstrap_roster()
    except Exception as exc:  # noqa: BLE001 - a bootstrap failure shouldn't block scraping the existing roster
        added = 0
        print(f"Roster bootstrap failed (continuing with existing roster): {exc}", file=sys.stderr)

    db = get_db()
    active = db.execute("SELECT id, name FROM gymnasts WHERE active = 1 ORDER BY name").fetchall()
    db.close()

    total_meets = 0
    errors = []
    for i, gymnast in enumerate(active):
        try:
            total_meets += scrape_gymnast(gymnast["id"])
        except Exception as exc:  # noqa: BLE001 - one bad page shouldn't stop the rest of the roster
            errors.append(f"{gymnast['name']} ({gymnast['id']}): {exc}")
        if i < len(active) - 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    status = "ok" if not errors else "partial"
    log_run(
        status,
        gymnasts_scraped=len(active),
        meets_found=total_meets,
        message="; ".join(errors) if errors else None,
    )
    print(
        f"Scraped {len(active)} active gymnasts ({added} newly added to roster), "
        f"{total_meets} meet-rows processed."
    )
    if errors:
        print(f"{len(errors)} gymnast(s) failed: {'; '.join(errors)}", file=sys.stderr)
    return 0 if not errors else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="Scrape now regardless of the 8am/5pm schedule window (for manual runs).",
    )
    args = parser.parse_args()
    sys.exit(run(force=args.force))
