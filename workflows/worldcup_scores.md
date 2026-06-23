# World Cup 2026 Score Tracker

## Objective
Follow FIFA World Cup 2026 match schedules and live/final scores on a local web page.
All match times are shown in Turkish local time (TRT = UTC+3, fixed offset, no DST).

## Inputs
None. Run on demand at any time during the tournament.

## Steps

### 1. Start the web server
```
python tools/serve_scores.py
```
- Fetches match data from ESPN's public API on startup
- Serves the page at http://localhost:5000
- Keep this terminal open while you want the page available

### 2. Open the page
Navigate to **http://localhost:5000** in any browser.

- The page loads all group-stage matches (Jun 11–27) with times in TRT
- Use the filter buttons at the top to show: Tümü (all), Canlı (live), Bugün (today), or a specific group
- Click **Yenile** or press F5 to fetch the latest scores — data refreshes every 5 minutes maximum

### 3. Stop the server
Press `Ctrl+C` in the terminal when done.

## Standalone fetch (optional)
To download match data without starting the web server:
```
python tools/fetch_worldcup_scores.py
```
Saves to `.tmp/worldcup_scores.json` and prints today's matches to the terminal.

## Tools Used
- `tools/serve_scores.py` — Flask web server, serves the match page
- `tools/fetch_worldcup_scores.py` — standalone fetch, saves JSON to .tmp/

## Data Source
ESPN public API — no API key required, no Firecrawl credits used:
`https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard`

## Expected Output
Local web page at http://localhost:5000 showing:
- All group-stage matches grouped by date (TRT)
- Match times in TRT (UTC+3)
- Live scores with a pulsing "Canlı" badge
- Final scores for finished matches, "vs" for upcoming
- Filter by group (Group A–L), today, or live-only

## Edge Cases

- **ESPN API returns 0 events**: API may be temporarily down. Refresh in a few minutes.
  The cached data from the last successful fetch is served in the meantime.

- **Scores look stale**: The cache TTL is 5 minutes. Click Yenile to force a fresh fetch.
  If scores are still wrong, check https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard
  in a browser to confirm the raw API response.

- **Knockout stage matches (post-Jun 27)**: Update the date range in both tools from
  `20260719` to a later date if needed. The ESPN API structure is identical for all rounds;
  `altGameNote` will show "Round of 32", "Quarterfinal", "Semifinal", "Final" etc.

- **Port 5000 already in use**: Change the port in the last line of `tools/serve_scores.py`:
  `app.run(host="0.0.0.0", port=5001, debug=False)` and open http://localhost:5001.

## Notes
- TRT is UTC+3 with no DST. Turkey abolished DST in 2016; no timezone database is needed.
- The ESPN API `date` field is always UTC (ISO 8601 ending in "Z"). Times are converted
  by adding exactly 3 hours — no ambiguity.
- Run command (one-liner): `python tools/serve_scores.py`
