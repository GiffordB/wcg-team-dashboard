"""
World Class Gymnastics - Optional Team Dashboard
--------------------------------------------------
A coach-facing view of every gymnast on the optional team (USAG JO Levels
6-10, plus the Xcel Gold/Platinum/Diamond tiers this gym runs alongside
them): who's at what level, how they did at their last meet, and how this
season is trending for each of them. All the meet data comes from
scraper.py, which pulls it from each gymnast's public mymeetscores.com
results page - this app just reads what's already in team.db and renders it.

The roster itself (who counts as "current") is a mix of automatic and
manual: scraper.py auto-adds anyone competing at an optional level who's
had a recent meet, but a gymnast who's joined without competing yet, or
who's left despite old meets still showing up in mymeetscores' history,
has to be added or removed by hand on the Manage Roster page.

Shape of this file:
  1. Setup & constants
  2. Small data-shaping helpers (turning rows of meet results into the
     numbers/series each page needs)
  3. Routes - one per page
"""

import os
from datetime import date, datetime

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import charts
import scraper
from gymdata import (
    EVENT_LABELS,
    EVENTS,
    get_db,
    init_db,
    level_sort_key,
)

app = Flask(__name__)

GYM_NAME = "World Class Gymnastics"
BRAND_INITIALS = "WCG"
SCRAPE_SECRET = os.environ.get("SCRAPE_SECRET")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")

EVENT_COLORS = {
    "vault": "#3f8fce",
    "bars": "#d9498f",
    "beam": "#b8790f",
    "floor": "#159873",
}
AA_COLOR = "#f5f5f7"

app.jinja_env.globals.update(
    gym_name=GYM_NAME,
    brand_initials=BRAND_INITIALS,
    event_labels=EVENT_LABELS,
    event_colors=EVENT_COLORS,
    events=EVENTS,
)


@app.template_filter("place_class")
def place_class(place):
    """'1T' -> 'gold', '2' -> 'silver', '3T' -> 'bronze', else ''."""
    if not place:
        return ""
    digits = "".join(ch for ch in place if ch.isdigit())
    if digits == "1":
        return "gold"
    if digits == "2":
        return "silver"
    if digits == "3":
        return "bronze"
    return ""


# ---------------------------------------------------------------------
# Data-shaping helpers
# ---------------------------------------------------------------------
def active_gymnasts(db):
    return db.execute(
        "SELECT * FROM gymnasts WHERE active = 1 ORDER BY name"
    ).fetchall()


def meets_for(db, gymnast_id):
    return db.execute(
        "SELECT * FROM meets WHERE gymnast_id = ? ORDER BY meet_date ASC, id ASC",
        (gymnast_id,),
    ).fetchall()


def season_bounds(today=None):
    """The gym season runs July 1 - June 30, not the calendar year."""
    today = today or date.today()
    if today.month >= 7:
        return date(today.year, 7, 1), date(today.year + 1, 6, 30)
    return date(today.year - 1, 7, 1), date(today.year, 6, 30)


def current_season_meets(meets):
    start, end = season_bounds()
    return [m for m in meets if start <= date.fromisoformat(m["meet_date"]) <= end]


def display_season_meets(meets):
    """The current season's meets if there are any yet; otherwise the most
    recently *completed* season's meets, so the dashboard shows the last
    real stretch of results instead of going blank the moment a season
    rolls over with nothing logged yet (true for the whole team right now -
    the 2026-27 season started July 1 with zero meets so far). Returns
    (meets, is_current_season)."""
    season_meets = current_season_meets(meets)
    if season_meets:
        return season_meets, True
    if not meets:
        return [], True
    last_date = date.fromisoformat(meets[-1]["meet_date"])
    start, end = season_bounds(last_date)
    return [m for m in meets if start <= date.fromisoformat(m["meet_date"]) <= end], False


def clean_event_score(value):
    """A 0.000 on an individual event means she scratched it - not a real
    score - so treat it as missing everywhere event scores get aggregated
    or charted. The raw meet tables still show the real 0.000 on record."""
    if value is None or value == 0:
        return None
    return value


def summary_stats(meets_list):
    aa_scores = [m["aa"] for m in meets_list if m["aa"] is not None]
    stats = {
        "meet_count": len(meets_list),
        "best_aa": max(aa_scores) if aa_scores else None,
        "avg_aa": round(sum(aa_scores) / len(aa_scores), 3) if aa_scores else None,
        "latest": meets_list[-1] if meets_list else None,
    }
    for event in EVENTS:
        vals = [clean_event_score(m[event]) for m in meets_list]
        vals = [v for v in vals if v is not None]
        stats[f"best_{event}"] = max(vals) if vals else None
    return stats


def chart_date_labels(meets_list):
    return [m["meet_date"][5:] for m in meets_list]


def aa_trend_chart(meets_list):
    categories = chart_date_labels(meets_list)
    series = [{"name": "All-Around", "color": AA_COLOR, "values": [m["aa"] for m in meets_list]}]
    return charts.line_chart(categories, series)


def event_trend_chart(meets_list):
    categories = chart_date_labels(meets_list)
    series = [
        {
            "name": EVENT_LABELS[event],
            "color": EVENT_COLORS[event],
            "values": [clean_event_score(m[event]) for m in meets_list],
            "slug": event,
        }
        for event in EVENTS
    ]
    return charts.line_chart(categories, series, y_min=0)


def team_roster_rows(db):
    """One row per active gymnast: her current level, latest meet, and her
    season stats (current season, or the last completed one if the new
    season hasn't started producing results yet), grouped by level in
    coaching order."""
    rows = []
    for gymnast in active_gymnasts(db):
        meets = meets_for(db, gymnast["id"])
        season_meets, _ = display_season_meets(meets)
        stats = summary_stats(season_meets)
        rows.append(
            {
                "id": gymnast["id"],
                "name": gymnast["name"],
                "level": gymnast["level"] or "?",
                "latest": meets[-1] if meets else None,
                "season_meet_count": stats["meet_count"],
                "season_best_aa": stats["best_aa"],
            }
        )

    grouped = {}
    for row in rows:
        grouped.setdefault(row["level"], []).append(row)
    for level_rows in grouped.values():
        level_rows.sort(key=lambda r: r["name"])

    return sorted(grouped.items(), key=lambda kv: level_sort_key(kv[0]))


def last_scrape_status(db):
    return db.execute("SELECT * FROM scrape_log ORDER BY id DESC LIMIT 1").fetchone()


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.route("/")
def team_roster():
    db = get_db()
    levels = team_roster_rows(db)
    total_active = sum(len(rows) for _, rows in levels)
    db.close()
    return render_template("roster.html", levels=levels, total_active=total_active)


@app.route("/athlete/<gymnast_id>")
def athlete(gymnast_id):
    db = get_db()
    gymnast = db.execute("SELECT * FROM gymnasts WHERE id = ?", (gymnast_id,)).fetchone()
    if not gymnast:
        db.close()
        abort(404)

    meets = meets_for(db, gymnast_id)
    season_meets, is_current_season = display_season_meets(meets)
    stats = summary_stats(season_meets)
    db.close()

    return render_template(
        "athlete.html",
        gymnast=gymnast,
        stats=stats,
        latest=stats["latest"],
        recent_meets=list(reversed(season_meets[-8:])),
        aa_chart=aa_trend_chart(season_meets) if season_meets else None,
        event_chart=event_trend_chart(season_meets) if season_meets else None,
        has_any_meets=bool(meets),
        is_current_season=is_current_season,
    )


@app.route("/roster")
def manage_roster():
    db = get_db()
    all_gymnasts = db.execute(
        "SELECT * FROM gymnasts ORDER BY active DESC, name"
    ).fetchall()
    db.close()
    return render_template(
        "manage_roster.html",
        gymnasts=all_gymnasts,
        error=request.args.get("error"),
        message=request.args.get("message"),
    )


def _check_admin_secret(form):
    if not ADMIN_SECRET:
        return "Roster changes are disabled until ADMIN_SECRET is configured on the server."
    if form.get("passcode") != ADMIN_SECRET:
        return "Wrong coach passcode."
    return None


@app.route("/roster/add", methods=["POST"])
def add_gymnast():
    error = _check_admin_secret(request.form)
    if error:
        return redirect(url_for("manage_roster", error=error))

    gymnast_id = request.form.get("gymnast_id", "").strip()
    name = request.form.get("name", "").strip()
    if not gymnast_id or not gymnast_id.isdigit():
        return redirect(url_for("manage_roster", error="Enter a numeric mymeetscores gymnast ID."))
    if not name:
        return redirect(url_for("manage_roster", error="Enter a name."))

    db = get_db()
    existing = db.execute("SELECT active FROM gymnasts WHERE id = ?", (gymnast_id,)).fetchone()
    if existing:
        db.execute("UPDATE gymnasts SET active = 1, name = ? WHERE id = ?", (name, gymnast_id))
        db.commit()
        db.close()
        return redirect(url_for("manage_roster", message=f"{name} is back on the roster."))

    db.execute(
        "INSERT INTO gymnasts (id, name, source, active, added_at) VALUES (?, ?, 'manual', 1, ?)",
        (gymnast_id, name, datetime.utcnow().isoformat(timespec="seconds")),
    )
    db.commit()
    db.close()

    try:
        scraper.scrape_gymnast(gymnast_id)
    except Exception:  # noqa: BLE001 - she's still added even if the first scrape fails
        pass

    return redirect(url_for("manage_roster", message=f"Added {name}."))


@app.route("/roster/<gymnast_id>/deactivate", methods=["POST"])
def deactivate_gymnast(gymnast_id):
    error = _check_admin_secret(request.form)
    if error:
        return redirect(url_for("manage_roster", error=error))

    db = get_db()
    db.execute("UPDATE gymnasts SET active = 0 WHERE id = ?", (gymnast_id,))
    db.commit()
    db.close()
    return redirect(url_for("manage_roster", message="Removed from the active roster."))


@app.route("/internal/scrape", methods=["POST"])
def trigger_scrape():
    """HTTP fallback for running the scraper, for hosts (like Render's free
    tier) where a real Cron Job service isn't available - see README.md."""
    if not SCRAPE_SECRET or request.args.get("key") != SCRAPE_SECRET:
        abort(403)
    force = request.args.get("force") == "1"
    ok = scraper.run(force=force) == 0
    return jsonify({"ok": ok})


@app.context_processor
def inject_sync_status():
    db = get_db()
    status = last_scrape_status(db)
    db.close()
    return {"sync_status": status}


def seed_if_empty():
    """On Render's free plan the local disk (including team.db) can be
    wiped by a redeploy or a spin-down/wake cycle, so every startup checks
    for that and re-seeds immediately - runs the full bootstrap + roster
    scrape - rather than waiting for the next scheduled sync or a manual
    /internal/scrape?force=1. Cheap and idempotent - a non-empty roster
    just skips this."""
    db = get_db()
    count = db.execute("SELECT COUNT(*) AS c FROM gymnasts").fetchone()["c"]
    db.close()
    if count == 0:
        scraper.run(force=True)


init_db()  # runs both for local `python app.py` and for gunicorn in production
seed_if_empty()

if __name__ == "__main__":
    app.run(debug=True, port=8000)
