"""
Shared constants, database schema, and helper functions used by both
app.py (the dashboard website) and scraper.py (the score fetcher).
"""

import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# On Render, DB_PATH points at the mounted persistent disk (see render.yaml)
# so the roster and meet history survive deploys and spin-down/wake cycles.
# Local dev has no such disk, so it falls back to a file next to the code.
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "team.db"))

# World Class Gymnastics and Cheerleading (Florida) on mymeetscores.com.
TEAM_ID = "164893"
TEAM_URL = f"https://www.mymeetscores.com/team.pl?teamid={TEAM_ID}"


def gymnast_url(gymnast_id):
    return f"https://www.mymeetscores.com/gymnast.pl?gymnastid={gymnast_id}"


# "Optional" here means USAG JO Levels 6-10, plus the Xcel divisions this
# gym treats as part of its optional team (Gold, Platinum, Diamond) - not
# the lower Xcel tiers (Bronze/Silver) or compulsory JO levels 1-5.
OPTIONAL_LEVELS = {"6", "7", "8", "9", "10", "XG", "XP", "XD"}

# Rough coaching-hierarchy order for sorting/grouping a roster - most
# advanced first: JO levels descending (10 down to 6), then the Xcel tiers
# this gym runs alongside them, also most-advanced-first (Diamond, then
# Platinum, then Gold).
LEVEL_ORDER = ["10", "9", "8", "7", "6", "XD", "XP", "XG"]

EVENTS = ["vault", "bars", "beam", "floor"]
EVENT_LABELS = {"vault": "Vault", "bars": "Bars", "beam": "Beam", "floor": "Floor"}


def level_sort_key(level):
    try:
        return (0, LEVEL_ORDER.index(level))
    except ValueError:
        return (1, level or "")


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    """Create tables if they don't already exist. Safe to run every startup."""
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS gymnasts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            level TEXT,
            level_override TEXT,
            team TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            active INTEGER NOT NULL DEFAULT 1,
            added_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gymnast_id TEXT NOT NULL REFERENCES gymnasts(id),
            meet_id TEXT,
            meet_date TEXT NOT NULL,
            meet_name TEXT NOT NULL,
            meet_url TEXT,
            team TEXT,
            session TEXT,
            level TEXT NOT NULL,
            division TEXT,
            vault REAL, vault_place TEXT,
            bars REAL, bars_place TEXT,
            beam REAL, beam_place TEXT,
            floor REAL, floor_place TEXT,
            aa REAL, aa_place TEXT,
            scraped_at TEXT NOT NULL,
            UNIQUE(gymnast_id, meet_id)
        );

        CREATE TABLE IF NOT EXISTS scrape_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ran_at TEXT NOT NULL,
            status TEXT NOT NULL,
            gymnasts_scraped INTEGER,
            meets_found INTEGER,
            message TEXT
        );
        """
    )

    # Migration: level_override was added after the table already existed
    # in production - CREATE TABLE IF NOT EXISTS above is a no-op there, so
    # add the column by hand if it's missing.
    existing_columns = {row["name"] for row in db.execute("PRAGMA table_info(gymnasts)")}
    if "level_override" not in existing_columns:
        db.execute("ALTER TABLE gymnasts ADD COLUMN level_override TEXT")

    db.commit()
    db.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
