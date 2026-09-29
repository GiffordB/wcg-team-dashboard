# World Class Gymnastics - Optional Team Dashboard

A coach-facing dashboard for the gym owner to track every gymnast on the
optional team (USAG JO Levels 6-10, plus the Xcel Gold/Platinum/Diamond
tiers this gym runs alongside them): who's at what level, how she did at
her last meet, and how this season is trending for her - all pulled
automatically from mymeetscores.com, no typing scores in by hand.

This is a sibling project to [gymnastics-dashboard](https://github.com/GiffordB/gymnastics-dashboard)
(one family's personal season tracker for a single gymnast) - same
underlying approach, but built for the gym owner to see her whole optional
roster at once rather than one family's own daughter.

## What's in here

- `app.py` - the Flask website: a **Team Roster** page (every active
  gymnast, grouped by level), an **athlete detail page** per gymnast
  (current-season summary, AA/event trend charts, recent meets), and a
  **Manage Roster** page to add or remove gymnasts by hand.
- `scraper.py` - two jobs in one run: (1) bootstraps the roster from the
  team's mymeetscores.com page, auto-adding any optional-level gymnast
  with a meet in roughly the last season; (2) scrapes every *active*
  gymnast's own results page for her full meet history. Safe to re-run
  any time - it updates existing meets instead of duplicating them.
- `charts.py` - the line/bar charts, drawn as plain SVG (no chart library,
  no CDN, so the site keeps working offline once loaded).
- `gymdata.py` - the shared database schema and constants, including the
  team ID and the definition of "optional" used for the roster bootstrap.
- `templates/`, `static/style.css` - the pages and the World Class
  Gymnastics black/pink/pastel-blue look.

## Why the roster is part-automatic, part-manual

mymeetscores' team page only lists gymnasts who already have a recorded
score - so it can't know about a gymnast who's *joined* the team but
hasn't competed yet, and it never "removes" someone who's *left* (her old
meets stay on the site forever). So:

- **Automatic**: every scrape run re-checks the team page and adds any
  new optional-level gymnast with a recent meet. It only ever *adds* -
  it never removes or reactivates anyone, so a gymnast the coach has
  taken off the roster stays off even though her old meets still show up.
- **Manual** (Manage Roster page, passcode-protected): add someone who
  hasn't competed yet, or remove someone who's left despite still
  appearing on the team page.

One known data quirk: mymeetscores occasionally gives the same real
gymnast two different profile IDs - one for her normal all-around record,
another ending in "IES VT" (or similar) for a meet where she competed as
an event specialist. Nothing here tries to auto-merge those (too easy to
accidentally combine two different people who share a name) - just remove
the spurious duplicate on the Manage Roster page if one shows up.

## Running it locally

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Pull the whole team's current results (takes a minute or two - ~40+ gymnasts)
./venv/bin/python scraper.py --force

# Start the site
ADMIN_SECRET=choose-a-coach-passcode ./venv/bin/python app.py
```

Then open **http://127.0.0.1:8000**.

## Keeping scores fresh automatically

Same schedule and mechanism as the sibling single-gymnast dashboard:
**Friday, Saturday, and Sunday at 8:00am and 5:00pm America/New_York,
October through June** (the season, not the calendar year - some levels
have started competing as early as October).

`app.py` exposes `POST /internal/scrape?key=<SCRAPE_SECRET>`, which runs
the scraper inside the running website process, so it writes to the exact
database the site reads from - no second Render service, no shared-disk
problem, no paid plan needed.

1. Deploy this app (see below). Render will auto-generate a `SCRAPE_SECRET`
   env var - find the value under the service's **Environment** tab.
2. Sign up for a free scheduled-HTTP-request service (e.g.
   [cron-job.org](https://cron-job.org)).
3. Create a job that sends `POST` to:
   `https://<your-app>.onrender.com/internal/scrape?key=<SCRAPE_SECRET>`
4. Set the job's timezone to `America/New_York` and its Custom schedule to
   hours **8** and **17**, minute **0**, weekdays Friday/Saturday/Sunday,
   months October through June. (cron-job.org's per-job timezone is
   DST-aware, so this fires at the real local times without any UTC math -
   if your scheduler only supports raw UTC, use 12:00, 13:00, 21:00, and
   22:00 UTC instead, which cover 8am/5pm Eastern on both sides of the
   March DST switch; `scraper.py`'s own window check turns the "wrong side"
   firings into harmless no-ops either way.)
5. After first deploying, hit that URL once by hand with `&force=1` added
   to seed the database immediately instead of waiting for the next
   scheduled window.

With ~40+ gymnasts, one full scrape run makes that many individual
requests to mymeetscores (plus one for the team roster) - `scraper.py`
spaces them out by a second each rather than firing them all at once.

## Deploying to Render

This repo has its own `render.yaml` at the root, so it's a one-click
Blueprint deploy:
**https://render.com/deploy?repo=https://github.com/GiffordB/wcg-team-dashboard**

You'll be asked to set `ADMIN_SECRET` during setup (or add it afterward
under the service's Environment tab) - that's the passcode the Manage
Roster page asks for before adding or removing a gymnast. Pick something
the coach can remember and share only with people she trusts to edit the
roster; it's separate from `SCRAPE_SECRET`, which Render generates on its
own and which only the scheduled scraper needs to know.

**Persistent disk:** `render.yaml` provisions a small 1GB disk mounted at
`/var/data` and points `team.db` at it (`DB_PATH`), on Render's Starter
plan - disks aren't available on the free plan. This is what makes manual
roster edits (an add, a remove, a level override) actually stick: without
a persistent disk, Render wipes local files on every deploy and every
spin-down/wake cycle, and the app's self-healing re-seed only knows how to
rebuild the roster from mymeetscores' auto-detected data - it has no way
to recover a manually-added gymnast who hasn't competed yet, or a manual
removal or level correction. With the disk, `team.db` (and everything in
it) survives all of that.
