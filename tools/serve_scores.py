#!/usr/bin/env python3
"""
Flask web server: World Cup 2026 match tracker with standings and squads.
Run once, then open http://localhost:5000 in any browser.
Cache logic: polls every 5 min during live matches, then waits until next match kickoff + 5 min.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, jsonify, Response, send_file
import requests

TRT = timezone(timedelta(hours=3), name="TRT")

ESPN_API_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
    "?dates=20260611-20260719&limit=200"
)
STANDINGS_API_URL = "https://site.api.espn.com/apis/v2/sports/soccer/fifa.world/standings"
ROSTER_API_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/teams/{team_id}/roster"

PHOTOS_DIR = Path(__file__).parent.parent / ".tmp" / "photos"

STATUS_MAP = {
    "STATUS_SCHEDULED":   "Planlandı",
    "STATUS_IN_PROGRESS": "Canlı",
    "STATUS_HALFTIME":    "Devre Arası",
    "STATUS_FULL_TIME":   "Bitti",
    "STATUS_FINAL":       "Bitti",
    "STATUS_FINAL_PEN":   "Bitti (Pen.)",
    "STATUS_END_PERIOD":  "Bitti",
}

_cache: dict = {"data": None, "refresh_at": None}
_standings_cache: dict = {"data": None, "refresh_at": None}
_roster_cache: dict = {}  # keyed by team_id string
_fifa_map_cache: dict = {"data": {}, "refresh_at": None}

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Match data
# ---------------------------------------------------------------------------

def _parse_event(event: dict) -> dict:
    comp = event["competitions"][0]
    status_obj = comp.get("status", {})
    status_type = status_obj.get("type", {})
    state = status_type.get("state", "pre")

    competitors = comp.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0] if competitors else {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1] if len(competitors) > 1 else {})

    home_score = int(home.get("score", 0)) if state != "pre" else None
    away_score = int(away.get("score", 0)) if state != "pre" else None

    # Penalty shootout scores (knockout matches decided on penalties only)
    home_pens = home.get("shootoutScore")
    away_pens = away.get("shootoutScore")

    utc_str = comp.get("date", "")
    utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    trt_dt = utc_dt.astimezone(TRT)

    alt_note = comp.get("altGameNote", "")
    group = alt_note.split(", ", 1)[1] if ", " in alt_note else (
        event.get("season", {}).get("slug", "").replace("-", " ").title()
    )

    status_name = status_type.get("name", "STATUS_SCHEDULED")
    status_display = STATUS_MAP.get(status_name, status_type.get("description", ""))
    live_clock = status_obj.get("displayClock", "") if state == "in" else ""

    venue = comp.get("venue", {})
    city = venue.get("address", {}).get("city", "")
    venue_str = f"{venue.get('fullName', '')}{', ' + city if city else ''}"

    home_team = home.get("team", {})
    away_team = away.get("team", {})

    return {
        "match_id":    event["id"],
        "group":       group,
        "home_team":   home_team.get("displayName", ""),
        "home_abbr":   home_team.get("abbreviation", ""),
        "home_flag":   home_team.get("logo", ""),
        "away_team":   away_team.get("displayName", ""),
        "away_abbr":   away_team.get("abbreviation", ""),
        "away_flag":   away_team.get("logo", ""),
        "home_score":  home_score,
        "away_score":  away_score,
        "home_pens":   home_pens,
        "away_pens":   away_pens,
        "home_winner": home.get("winner", False),
        "away_winner": away.get("winner", False),
        "date_trt":    trt_dt.strftime("%Y-%m-%d"),
        "time_trt":    trt_dt.strftime("%H:%M"),
        "date_label":  trt_dt.strftime("%d %b"),
        "day_label":   trt_dt.strftime("%A"),
        "status":      status_display,
        "status_raw":  status_name,
        "state":       state,
        "live_clock":  live_clock,
        "venue":       venue_str,
        "kickoff_utc": utc_dt.isoformat(),
        "fifa_url":    None,
    }


def _compute_next_refresh(matches: list[dict]) -> datetime:
    now = datetime.now(timezone.utc)
    if any(m["state"] == "in" for m in matches):
        return now + timedelta(minutes=5)
    upcoming = [m for m in matches if m["state"] == "pre"]
    if upcoming:
        earliest = min(upcoming, key=lambda m: m["kickoff_utc"])
        kickoff = datetime.fromisoformat(earliest["kickoff_utc"])
        return kickoff + timedelta(minutes=5)
    return now + timedelta(hours=24)


def get_matches() -> list[dict]:
    now = datetime.now(timezone.utc)
    if _cache["data"] is not None and _cache["refresh_at"] is not None:
        if now < _cache["refresh_at"]:
            kickoff_passed = any(
                m["state"] == "pre" and datetime.fromisoformat(m["kickoff_utc"]) <= now
                for m in _cache["data"]
            )
            if not kickoff_passed:
                return _cache["data"]

    try:
        resp = requests.get(ESPN_API_URL, timeout=20, headers={"User-Agent": "WorldCupTracker/1.0"})
        resp.raise_for_status()
        events = resp.json().get("events", [])
    except requests.exceptions.RequestException as e:
        print(f"[HATA] ESPN API: {e}")
        return _cache["data"] or []

    matches = sorted([_parse_event(e) for e in events], key=lambda m: m["date_trt"] + m["time_trt"])

    try:
        fifa_map = get_fifa_map()
        for m in matches:
            key = (m["kickoff_utc"][:10], m["home_abbr"], m["away_abbr"])
            m["fifa_url"] = fifa_map.get(key)
    except Exception as e:
        print(f"[HATA] FIFA URL eşleme: {e}")

    _cache["data"] = matches
    _cache["refresh_at"] = _compute_next_refresh(matches)
    _standings_cache["refresh_at"] = None  # always re-fetch standings after fresh match data

    refresh_trt = _cache["refresh_at"].astimezone(TRT).strftime("%H:%M TRT")
    print(f"[INFO] Maçlar güncellendi. Sonraki: {refresh_trt}")
    return matches


# ---------------------------------------------------------------------------
# Standings data
# ---------------------------------------------------------------------------

def _annotate_qualify_status(groups: list[dict], matches: list[dict]) -> list[dict]:
    """Set qualify_status on each entry from actual knockout-round match data."""
    knockout_abbrs: set[str] = set()
    for m in matches:
        if not m["group"].startswith("Group "):
            knockout_abbrs.add(m["home_abbr"])
            knockout_abbrs.add(m["away_abbr"])
    for group in groups:
        for e in group["entries"]:
            if e["abbr"] in knockout_abbrs:
                e["qualify_status"] = "qualified"
            elif int(e["gp"]) >= 3:
                e["qualify_status"] = "eliminated"
            else:
                e["qualify_status"] = ""
    return groups


def _compute_standings_refresh(matches: list[dict]) -> datetime:
    now = datetime.now(timezone.utc)
    refresh_points = []
    for m in matches:
        kickoff = datetime.fromisoformat(m["kickoff_utc"])
        refresh_points.append(kickoff)
        refresh_points.append(kickoff + timedelta(minutes=120))
    future = [p for p in refresh_points if p > now]
    return min(future) if future else now + timedelta(hours=24)


def get_standings(matches: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    if _standings_cache["data"] is not None and _standings_cache["refresh_at"] is not None:
        if now < _standings_cache["refresh_at"]:
            return _standings_cache["data"]

    try:
        resp = requests.get(STANDINGS_API_URL, timeout=20, headers={"User-Agent": "WorldCupTracker/1.0"})
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"[HATA] ESPN Standings API: {e}")
        return _standings_cache["data"] or []

    groups = []
    for group in data.get("children", []):
        entries = []
        for entry in group["standings"]["entries"]:
            team = entry["team"]
            stats = {s["name"]: s["displayValue"] for s in entry.get("stats", [])}
            logo = team["logos"][0]["href"] if team.get("logos") else ""
            note = entry.get("note", {})
            rank_val = stats.get("rank", "0") or "0"
            entries.append({
                "id":         team.get("id", ""),
                "rank":       int(float(rank_val)),
                "team":       team.get("displayName", ""),
                "abbr":       team.get("abbreviation", ""),
                "logo":       logo,
                "gp":         stats.get("gamesPlayed", "0"),
                "w":          stats.get("wins", "0"),
                "d":          stats.get("ties", "0"),
                "l":          stats.get("losses", "0"),
                "gf":         stats.get("pointsFor", "0"),
                "ga":         stats.get("pointsAgainst", "0"),
                "gd":         stats.get("pointDifferential", "0"),
                "pts":        stats.get("points", "0"),
                "qualify_status": "",
                "note_color":    note.get("color", ""),
                "note_desc":     note.get("description", ""),
            })
        entries.sort(key=lambda e: e["rank"])
        groups.append({"name": group["name"], "entries": entries})

    _standings_cache["data"] = groups
    _standings_cache["refresh_at"] = _compute_standings_refresh(matches)

    refresh_trt = _standings_cache["refresh_at"].astimezone(TRT).strftime("%H:%M TRT")
    print(f"[INFO] Puan durumu güncellendi. Sonraki: {refresh_trt}")
    return groups


# ---------------------------------------------------------------------------
# Roster data
# ---------------------------------------------------------------------------

def get_roster(team_id: str) -> list[dict]:
    if team_id in _roster_cache:
        return _roster_cache[team_id]

    try:
        url = ROSTER_API_URL.format(team_id=team_id)
        resp = requests.get(url, timeout=20, headers={"User-Agent": "WorldCupTracker/1.0"})
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"[HATA] ESPN Roster API ({team_id}): {e}")
        return []

    players = []
    for a in data.get("athletes", []):
        pos = a.get("position", {})
        jersey = a.get("jersey")
        players.append({
            "id":            a.get("id", ""),
            "name":          a.get("displayName", ""),
            "jersey":        int(jersey) if jersey else None,
            "position":      pos.get("displayName", ""),
            "position_abbr": pos.get("abbreviation", ""),
            "age":           a.get("age"),
        })

    _roster_cache[team_id] = players
    print(f"[INFO] Kadro yüklendi: team {team_id} ({len(players)} oyuncu)")
    return players


# ---------------------------------------------------------------------------
# FIFA match-centre URL map
# ---------------------------------------------------------------------------

FIFA_CALENDAR_URL = (
    "https://api.fifa.com/api/v3/calendar/matches"
    "?from=2026-06-11&to=2026-07-20&language=en&count=200"
    "&idCompetition=17&idSeason=285023"
)


def get_fifa_map() -> dict:
    """Returns dict: (utc_date_str, home_abbr, away_abbr) -> fifa_match_centre_url"""
    now = datetime.now(timezone.utc)
    if _fifa_map_cache["data"] and _fifa_map_cache["refresh_at"] and now < _fifa_map_cache["refresh_at"]:
        return _fifa_map_cache["data"]

    try:
        resp = requests.get(FIFA_CALENDAR_URL, timeout=20, headers={"User-Agent": "WorldCupTracker/1.0"})
        resp.raise_for_status()
        results = resp.json().get("Results", [])
    except Exception as e:
        print(f"[HATA] FIFA harita: {e}")
        return _fifa_map_cache["data"] or {}

    mapping = {}
    for m in results:
        h = m.get("Home") or {}
        a = m.get("Away") or {}
        date_str = (m.get("Date") or "")[:10]
        home_abbr = h.get("Abbreviation", "")
        away_abbr = a.get("Abbreviation", "")
        stage_id = m.get("IdStage", "")
        match_id = m.get("IdMatch", "")
        if date_str and home_abbr and away_abbr and match_id:
            url = f"https://www.fifa.com/en/match-centre/match/17/285023/{stage_id}/{match_id}"
            mapping[(date_str, home_abbr, away_abbr)] = url

    _fifa_map_cache["data"] = mapping
    _fifa_map_cache["refresh_at"] = now + timedelta(hours=6)
    print(f"[INFO] FIFA harita güncellendi: {len(mapping)} maç")
    return mapping


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FIFA Dünya Kupası 2026</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #fafaf9; color: #1c1917; min-height: 100vh; }

  /* Header */
  .header {
    background: #1c1917;
    padding: 20px 24px 16px;
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 12px;
    border-bottom: 3px solid #0d9488;
  }
  .header-title { display: flex; align-items: center; gap: 14px; }
  .header-title h1 { font-size: 1.5rem; font-weight: 700; color: #fff; }
  .header-title .year { color: #0d9488; }
  .trophy { font-size: 2rem; }
  .meta { font-size: 0.8rem; color: #a8a29e; }
  .meta strong { color: #e7e5e4; }
  .refresh-btn {
    background: #0d9488; color: #fff; border: none; padding: 8px 18px;
    border-radius: 20px; font-weight: 700; cursor: pointer; font-size: 0.85rem;
    transition: transform 0.1s, background 0.2s;
  }
  .refresh-btn:hover { background: #0f766e; transform: scale(1.04); }

  /* Filter bar */
  .filter-bar {
    background: #f5f5f4; padding: 12px 24px;
    display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
    border-bottom: 1px solid #e7e5e4;
    position: sticky; top: 0; z-index: 10;
  }
  .filter-label { font-size: 0.75rem; color: #78716c; text-transform: uppercase;
                  letter-spacing: 0.05em; margin-right: 4px; }
  .filter-btn {
    background: #fff; color: #57534e; border: 1px solid #d6d3d1;
    padding: 5px 12px; border-radius: 16px; cursor: pointer; font-size: 0.8rem;
    transition: all 0.15s;
  }
  .filter-btn:hover { background: #e7e5e4; color: #1c1917; }
  .filter-btn.active { background: #1c1917; color: #fff; border-color: #1c1917; }
  .filter-btn.live-btn.active { background: #dc2626; border-color: #dc2626; }
  .filter-btn.standings-btn.active { background: #0d9488; border-color: #0d9488; }
  .filter-btn.squads-btn.active { background: #0d9488; border-color: #0d9488; }
  .filter-divider { width: 1px; height: 22px; background: #d6d3d1; margin: 0 4px; flex-shrink: 0; }
  .search-wrapper { position: relative; margin-left: auto; }
  .search-input {
    background: #fff; color: #1c1917; border: 1px solid #d6d3d1;
    padding: 6px 14px 6px 34px; border-radius: 20px; font-size: 0.85rem;
    outline: none; width: 200px; transition: border-color 0.2s, width 0.2s;
  }
  .search-input:focus { border-color: #0d9488; width: 260px; }
  .search-input::placeholder { color: #a8a29e; }
  .search-icon { position: absolute; left: 10px; top: 50%; transform: translateY(-50%);
                 color: #a8a29e; font-size: 0.85rem; pointer-events: none; }

  /* Match cards */
  .content { padding: 0 16px 40px; max-width: 1100px; margin: 0 auto; }
  .day-section { margin-top: 24px; }
  .day-header {
    padding: 10px 16px; background: #f5f5f4;
    border-left: 4px solid #0d9488; border-radius: 4px;
    font-size: 0.9rem; font-weight: 600; color: #57534e;
    margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.04em;
  }
  .match-card {
    background: #fffbeb; border-radius: 10px; margin-bottom: 6px;
    border: 1px solid #e7e5e4; overflow: hidden; transition: border-color 0.2s;
  }
  .match-card:hover { border-color: #d6d3d1; background: #fef3c7; }
  .match-card.live { border-color: #0d9488; background: #f0fdfa; }
  .match-card.finished { opacity: 0.92; }
  .match-inner {
    display: grid;
    grid-template-columns: 80px 1fr auto 1fr 110px;
    align-items: center; padding: 12px 16px; gap: 8px;
  }
  .match-time { text-align: center; }
  .match-time .time { font-size: 1.1rem; font-weight: 700; color: #1c1917; }
  .match-time .trt-label { font-size: 0.65rem; color: #78716c; }
  .match-time .group-tag {
    font-size: 0.68rem; color: #57534e; background: #e7e5e4;
    padding: 2px 6px; border-radius: 10px; margin-top: 3px; display: inline-block;
  }
  .team { display: flex; align-items: center; gap: 8px; }
  .team.home { justify-content: flex-end; text-align: right; }
  .team.away { justify-content: flex-start; text-align: left; }
  .team-name { font-size: 0.95rem; font-weight: 600; color: #1c1917; }
  .team-abbr { font-size: 0.75rem; color: #78716c; display: none; }
  .team-flag { width: 28px; height: 20px; object-fit: cover; border-radius: 2px; flex-shrink: 0; }
  .team-flag-placeholder { width: 28px; height: 20px; background: #d6d3d1; border-radius: 2px; flex-shrink: 0; }
  .winner .team-name { color: #0c0a09; font-weight: 800; }
  .qual-arrow { font-size: 1.3rem; font-weight: 900; line-height: 1; flex-shrink: 0; }
  .qual-arrow.up { color: #16a34a; }
  .qual-arrow.down { color: #dc2626; }
  .score-box { text-align: center; flex-shrink: 0; min-width: 80px; }
  .score-display {
    font-size: 1.5rem; font-weight: 800; color: #0d9488;
    background: #f5f5f4; border-radius: 8px;
    padding: 4px 14px; letter-spacing: 2px; display: inline-block;
  }
  .score-display.pending { color: #a8a29e; font-size: 1rem; letter-spacing: 0; }
  .live .score-display { background: #ccfbf1; color: #0f766e; }
  .pen-score { display: block; margin-top: 4px; font-size: 0.72rem; font-weight: 700;
    color: #0d9488; letter-spacing: 0.04em; }
  .match-status { text-align: center; }
  .status-badge {
    display: inline-block; font-size: 0.7rem; font-weight: 700;
    padding: 3px 10px; border-radius: 12px; text-transform: uppercase; letter-spacing: 0.05em;
  }
  .badge-scheduled { background: #f5f5f4; color: #78716c; }
  .badge-live { background: #dc2626; color: #fff; animation: pulse 1.5s infinite; }
  .badge-halftime { background: #fef3c7; color: #92400e; }
  .badge-finished { background: #e7e5e4; color: #57534e; }
  .live-clock { font-size: 0.8rem; color: #0d9488; margin-top: 3px; font-weight: 700; }
  .venue-text { font-size: 0.68rem; color: #a8a29e; margin-top: 2px; }
  .highlights-link {
    display: inline-block; margin-top: 5px;
    color: #0d9488; font-size: 0.7rem; font-weight: 700;
    text-decoration: none; letter-spacing: 0.03em;
  }
  .highlights-link:hover { color: #0f766e; text-decoration: underline; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
  .no-matches { padding: 48px; text-align: center; color: #78716c; font-size: 1rem; }

  /* Standings */
  #standingsView { display: none; padding: 16px 16px 40px; max-width: 1200px; margin: 0 auto; }
  .standings-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
  .group-card { background: #fff; border-radius: 10px; border: 1px solid #e7e5e4; overflow: hidden; }
  .group-card-header {
    background: #1c1917;
    padding: 10px 14px; font-size: 0.85rem; font-weight: 700;
    color: #0d9488; text-transform: uppercase; letter-spacing: 0.06em;
    border-bottom: 1px solid #e7e5e4;
  }
  .standings-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
  .standings-table thead th {
    padding: 6px 6px; text-align: center; color: #78716c;
    font-weight: 600; font-size: 0.7rem; text-transform: uppercase;
    border-bottom: 1px solid #e7e5e4; background: #f5f5f4;
  }
  .standings-table thead th.th-team { text-align: left; padding-left: 10px; }
  .standings-table tbody tr { border-bottom: 1px solid #f5f5f4; transition: background 0.15s; }
  .standings-table tbody tr:last-child { border-bottom: none; }
  .standings-table tbody tr:hover { background: #fef3c7; }
  .standings-table td { padding: 7px 6px; text-align: center; color: #1c1917; }
  .standings-table td.td-rank { width: 24px; font-size: 0.72rem; color: #78716c; font-weight: 600; padding-left: 8px; }
  .standings-table td.td-team { text-align: left; padding-left: 6px; }
  .standings-table td.td-pts { font-weight: 800; color: #0c0a09; }
  .standings-table td.td-gd { color: #0d9488; }
  .s-team-cell { display: flex; align-items: center; gap: 7px; }
  .s-flag { width: 22px; height: 16px; object-fit: cover; border-radius: 2px; flex-shrink: 0; }
  .s-flag-placeholder { width: 22px; height: 16px; background: #d6d3d1; border-radius: 2px; flex-shrink: 0; }
  .s-name { font-size: 0.82rem; font-weight: 600; color: #1c1917; white-space: nowrap; }
  .s-abbr { font-size: 0.72rem; color: #78716c; display: none; }
  .qualifies-row { border-left: 3px solid #16a34a !important; background: rgba(22, 163, 74, 0.05); }
  .qualifies-row td.td-rank { color: #16a34a; font-weight: 800; }
  .qualifies-row .s-name { color: #14532d; }
  .qualifier-end-row { border-bottom: 2px solid rgba(22, 163, 74, 0.25) !important; }
  .maybe-row { border-left: 3px solid #d97706 !important; background: rgba(217, 119, 6, 0.05); }
  .maybe-row td.td-rank { color: #d97706; font-weight: 800; }
  .eliminated-row { border-left: 3px solid #dc2626 !important; background: rgba(220, 38, 38, 0.03); }
  .eliminated-row td { color: #a8a29e !important; }
  .eliminated-row td.td-pts { color: #78716c !important; font-weight: 600; }
  .eliminated-row .s-name { color: #a8a29e !important; }
  .standings-legend { margin-top: 12px; display: flex; align-items: center; gap: 14px;
    font-size: 0.75rem; color: #78716c; padding: 0 4px; flex-wrap: wrap; }
  .legend-item { display: flex; align-items: center; gap: 6px; }
  .legend-bar { width: 3px; height: 14px; border-radius: 2px; flex-shrink: 0; }
  .legend-bar-green { background: #16a34a; }
  .legend-bar-amber { background: #d97706; }
  .legend-bar-red { background: #dc2626; }

  /* Squads */
  #squadsView { display: none; padding: 16px 16px 40px; max-width: 1200px; margin: 0 auto; }

  .squads-back {
    display: none; margin-bottom: 16px;
    background: #f5f5f4; color: #57534e; border: 1px solid #d6d3d1;
    padding: 7px 16px; border-radius: 20px; cursor: pointer; font-size: 0.85rem;
  }
  .squads-back:hover { background: #e7e5e4; color: #1c1917; }
  .squads-back.visible { display: inline-block; }

  .teams-group-section { margin-bottom: 24px; }
  .teams-group-label {
    font-size: 0.75rem; font-weight: 700; color: #78716c;
    text-transform: uppercase; letter-spacing: 0.06em;
    margin-bottom: 8px; padding-left: 2px;
  }
  .teams-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; margin-bottom: 4px; }
  .team-tile {
    background: #fff; border: 1px solid #e7e5e4; border-radius: 8px;
    padding: 10px 6px; text-align: center; cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
    display: flex; flex-direction: column; align-items: center; gap: 6px;
  }
  .team-tile:hover { border-color: #0d9488; background: #f0fdfa; }
  .team-tile.selected { border-color: #0d9488; background: #ccfbf1; }
  .team-tile-flag { width: 36px; height: 26px; object-fit: cover; border-radius: 3px; }
  .team-tile-flag-ph { width: 36px; height: 26px; background: #d6d3d1; border-radius: 3px; }
  .team-tile-name { font-size: 0.72rem; color: #1c1917; font-weight: 600; line-height: 1.2; }

  .squad-panel { display: none; }
  .squad-panel.visible { display: block; }
  .squad-team-header {
    display: flex; align-items: center; gap: 14px;
    padding: 16px 0 20px;
    border-bottom: 2px solid #e7e5e4; margin-bottom: 24px;
  }
  .squad-team-flag { width: 52px; height: 36px; object-fit: cover; border-radius: 4px; }
  .squad-team-name { font-size: 1.3rem; font-weight: 700; color: #1c1917; }
  .squad-team-group { font-size: 0.8rem; color: #0d9488; margin-top: 3px; }

  .position-section { margin-bottom: 28px; }
  .position-header {
    font-size: 0.8rem; font-weight: 700; color: #78716c;
    text-transform: uppercase; letter-spacing: 0.06em;
    border-left: 3px solid #d6d3d1; padding-left: 8px; margin-bottom: 12px;
  }
  .players-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 10px; }

  .player-card {
    background: #fff; border: 1px solid #e7e5e4; border-radius: 8px;
    overflow: hidden; text-align: center; transition: border-color 0.15s;
  }
  .player-card:hover { border-color: #d6d3d1; }
  .player-photo-wrap {
    background: #f5f5f4; height: 76px;
    display: flex; align-items: flex-end; justify-content: center; overflow: hidden;
  }
  .player-photo { width: 66px; height: 76px; object-fit: cover; object-position: top; display: block; }
  .player-photo-wrap.no-photo {
    align-items: center;
    background: #f5f5f4;
  }
  .player-silhouette {
    width: 40px; height: 56px; opacity: 0.15;
    background: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%231c1917'%3E%3Cpath d='M12 12c2.7 0 4.8-2.1 4.8-4.8S14.7 2.4 12 2.4 7.2 4.5 7.2 7.2 9.3 12 12 12zm0 2.4c-3.2 0-9.6 1.6-9.6 4.8v2.4h19.2v-2.4c0-3.2-6.4-4.8-9.6-4.8z'/%3E%3C/svg%3E") no-repeat center/contain;
  }
  .player-info { padding: 6px 4px 8px; }
  .player-jersey { font-size: 0.65rem; color: #78716c; font-weight: 700; margin-bottom: 2px; }
  .player-name { font-size: 0.75rem; font-weight: 700; color: #1c1917; line-height: 1.2; margin-bottom: 2px; }
  .player-age { font-size: 0.65rem; color: #a8a29e; }

  .squad-loading { text-align: center; padding: 48px; color: #78716c; font-size: 0.9rem; }
  .squad-error { text-align: center; padding: 48px; color: #dc2626; font-size: 0.9rem; }

  /* Lightbox */
  .lightbox-overlay {
    display: none; position: fixed; inset: 0; z-index: 1000;
    background: rgba(0,0,0,0.6); align-items: center; justify-content: center;
  }
  .lightbox-overlay.open { display: flex; }
  .lightbox-box {
    background: #fff; border-radius: 12px; overflow: hidden;
    max-width: 300px; width: 88%; position: relative;
    box-shadow: 0 20px 60px rgba(0,0,0,0.25);
    animation: lbIn 0.15s ease-out;
  }
  @keyframes lbIn { from { transform: scale(0.9); opacity: 0; } to { transform: scale(1); opacity: 1; } }
  .lightbox-img { width: 100%; display: block; object-fit: cover; object-position: top; max-height: 340px; }
  .lightbox-caption { padding: 12px 16px 14px; border-top: 1px solid #e7e5e4; }
  .lightbox-caption-name { font-size: 1rem; font-weight: 700; color: #1c1917; margin-bottom: 3px; }
  .lightbox-caption-meta { font-size: 0.8rem; color: #78716c; }
  .lightbox-close {
    position: absolute; top: 8px; right: 8px;
    background: rgba(0,0,0,0.45); color: #fff; border: none;
    border-radius: 50%; width: 28px; height: 28px; font-size: 1.1rem;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
  }
  .lightbox-close:hover { background: rgba(0,0,0,0.7); }
  .player-photo { cursor: pointer; }

  /* Theme toggle button */
  .theme-btn {
    background: rgba(255,255,255,0.12); color: #fff; border: 1px solid rgba(255,255,255,0.2);
    border-radius: 20px; padding: 6px 12px; font-size: 1rem; cursor: pointer;
    transition: background 0.2s; line-height: 1;
  }
  .theme-btn:hover { background: rgba(255,255,255,0.22); }

  /* ── Dark theme overrides (body.dark) ── */
  body.dark { background: #0a0e1a; color: #e8eaf6; }
  body.dark .header { background: linear-gradient(135deg, #1a237e 0%, #283593 50%, #1565c0 100%); border-bottom-color: #ffd700; }
  body.dark .header-title .year { color: #ffd700; }
  body.dark .meta { color: #90caf9; }
  body.dark .meta strong { color: #fff; }
  body.dark .refresh-btn { background: #ffd700; color: #000; }
  body.dark .refresh-btn:hover { background: #ffec6e; }
  body.dark .filter-bar { background: #111827; border-bottom-color: #1f2937; }
  body.dark .filter-btn { background: #1f2937; color: #9ca3af; border-color: #374151; }
  body.dark .filter-btn:hover { background: #374151; color: #e5e7eb; }
  body.dark .filter-btn.active { background: #1d4ed8; border-color: #1d4ed8; color: #fff; }
  body.dark .filter-btn.live-btn.active { background: #dc2626; border-color: #dc2626; }
  body.dark .filter-btn.standings-btn.active { background: #7c3aed; border-color: #7c3aed; }
  body.dark .filter-btn.squads-btn.active { background: #0f766e; border-color: #0f766e; }
  body.dark .filter-divider { background: #374151; }
  body.dark .search-input { background: #1f2937; color: #e5e7eb; border-color: #374151; }
  body.dark .search-input:focus { border-color: #3b82f6; }
  body.dark .search-input::placeholder { color: #6b7280; }
  body.dark .search-icon { color: #6b7280; }
  body.dark .day-header { background: #111827; border-left-color: #ffd700; color: #ffd700; }
  body.dark .match-card { background: #111827; border-color: #1f2937; }
  body.dark .match-card:hover { border-color: #374151; background: #111827; }
  body.dark .match-card.live { border-color: #fbbf24; background: #1c1a0a; }
  body.dark .match-time .time { color: #fff; }
  body.dark .match-time .trt-label { color: #6b7280; }
  body.dark .match-time .group-tag { color: #60a5fa; background: #1e3a5f; }
  body.dark .team-name { color: #e5e7eb; }
  body.dark .team-abbr { color: #9ca3af; }
  body.dark .team-flag-placeholder { background: #374151; }
  body.dark .winner .team-name { color: #fff; font-weight: 800; }
  body.dark .score-display { color: #fff; background: #1f2937; }
  body.dark .score-display.pending { color: #4b5563; }
  body.dark .live .score-display { background: #78350f; color: #fbbf24; }
  body.dark .pen-score { color: #fbbf24; }
  body.dark .badge-scheduled { background: #1f2937; color: #6b7280; }
  body.dark .badge-halftime { background: #92400e; color: #fcd34d; }
  body.dark .badge-finished { background: #14532d; color: #86efac; }
  body.dark .live-clock { color: #fbbf24; }
  body.dark .venue-text { color: #4b5563; }
  body.dark .highlights-link { color: #fbbf24; }
  body.dark .highlights-link:hover { color: #ffd700; }
  body.dark .no-matches { color: #6b7280; }
  body.dark .group-card { background: #111827; border-color: #1f2937; }
  body.dark .group-card-header { background: linear-gradient(90deg, #1e3a5f, #1f2937); color: #ffd700; border-bottom-color: #1f2937; }
  body.dark .standings-table thead th { color: #6b7280; border-bottom-color: #1f2937; background: #0f172a; }
  body.dark .standings-table tbody tr { border-bottom-color: #1a2030; }
  body.dark .standings-table tbody tr:hover { background: #1a2235; }
  body.dark .standings-table td { color: #d1d5db; }
  body.dark .standings-table td.td-rank { color: #6b7280; }
  body.dark .standings-table td.td-pts { color: #fff; }
  body.dark .standings-table td.td-gd { color: #93c5fd; }
  body.dark .s-flag-placeholder { background: #374151; }
  body.dark .s-name { color: #e5e7eb; }
  body.dark .s-abbr { color: #9ca3af; }
  body.dark .qualifies-row { border-left-color: #22c55e !important; background: rgba(34,197,94,0.07); }
  body.dark .qualifies-row td.td-rank { color: #22c55e; }
  body.dark .qualifies-row .s-name { color: #f0fdf4; }
  body.dark .eliminated-row td { color: #6b7280 !important; }
  body.dark .eliminated-row td.td-pts { color: #9ca3af !important; }
  body.dark .eliminated-row .s-name { color: #9ca3af !important; }
  body.dark .standings-legend { color: #6b7280; }
  body.dark .squads-back { background: #1f2937; color: #9ca3af; border-color: #374151; }
  body.dark .squads-back:hover { background: #374151; color: #e5e7eb; }
  body.dark .teams-group-label { color: #6b7280; }
  body.dark .team-tile { background: #111827; border-color: #1f2937; }
  body.dark .team-tile:hover { border-color: #3b82f6; background: #1a2235; }
  body.dark .team-tile.selected { border-color: #ffd700; background: #1c1a0a; }
  body.dark .team-tile-flag-ph { background: #374151; }
  body.dark .team-tile-name { color: #d1d5db; }
  body.dark .squad-team-header { border-bottom-color: #1f2937; }
  body.dark .squad-team-name { color: #fff; }
  body.dark .squad-team-group { color: #60a5fa; }
  body.dark .position-header { color: #6b7280; border-left-color: #374151; }
  body.dark .player-card { background: #111827; border-color: #1f2937; }
  body.dark .player-card:hover { border-color: #374151; }
  body.dark .player-photo-wrap { background: #1a2235; }
  body.dark .player-photo-wrap.no-photo { background: #1a2235; }
  body.dark .player-jersey { color: #6b7280; }
  body.dark .player-name { color: #e5e7eb; }
  body.dark .player-age { color: #6b7280; }
  body.dark .squad-loading { color: #6b7280; }
  body.dark .lightbox-box { background: #111827; box-shadow: 0 20px 60px rgba(0,0,0,0.8); }
  body.dark .lightbox-caption { border-top-color: #1f2937; }
  body.dark .lightbox-caption-name { color: #fff; }
  body.dark .lightbox-caption-meta { color: #9ca3af; }

  /* Responsive */
  @media (max-width: 1024px) {
    .standings-grid { grid-template-columns: repeat(2, 1fr); }
    .teams-grid { grid-template-columns: repeat(4, 1fr); }
  }
  @media (max-width: 640px) {
    .match-inner { grid-template-columns: 60px 1fr auto 1fr 80px; padding: 10px; gap: 6px; }
    .team-name { font-size: 0.78rem; }
    .team-abbr { display: block; }
    .team-name { display: none; }
    .score-display { font-size: 1.2rem; padding: 3px 10px; }
    .header-title h1 { font-size: 1.1rem; }
    .standings-grid { grid-template-columns: 1fr; }
    .s-name { display: none; }
    .s-abbr { display: block; }
    .teams-grid { grid-template-columns: repeat(3, 1fr); }
    .players-grid { grid-template-columns: repeat(3, 1fr); }
  }
</style>
</head>
<body>

<div class="header">
  <div class="header-title">
    <span class="trophy">&#127942;</span>
    <div>
      <h1>FIFA Dünya Kupası <span class="year">2026</span></h1>
      <div class="meta">Son güncelleme: <strong>__UPDATED__</strong></div>
    </div>
  </div>
  <div style="display:flex;gap:8px;align-items:center;">
    <button class="theme-btn" id="themeBtn" onclick="toggleTheme()" title="Tema değiştir">&#9790;</button>
    <button class="refresh-btn" onclick="location.reload()">&#8635; Yenile</button>
  </div>
</div>

<div class="filter-bar">
  <span class="filter-label">Filtre:</span>
  <button class="filter-btn active" onclick="filterMatches('all', this)">Tümü</button>
  <button class="filter-btn live-btn" onclick="filterMatches('live', this)">🔴 Canlı</button>
  <button class="filter-btn" onclick="filterMatches('today', this)">Bugün</button>
  __GROUP_BUTTONS__
  <button class="filter-btn" onclick="filterMatches('Round of 32', this)">Son 32</button>
  <button class="filter-btn" onclick="filterMatches('Round of 16', this)">Son 16</button>
  <button class="filter-btn" onclick="filterMatches('Quarterfinals', this)">Çeyrek Final</button>
  <button class="filter-btn" onclick="filterMatches('Semifinals', this)">Yarı Final</button>
  <button class="filter-btn" onclick="filterMatches('3rd-Place Match', this)">3.lük Maçı</button>
  <button class="filter-btn" onclick="filterMatches('Final', this)">Final</button>
  <div class="filter-divider"></div>
  <button class="filter-btn standings-btn" onclick="showStandings(this)">&#128202; Puan Durumu</button>
  <button class="filter-btn squads-btn" onclick="showSquads(this)">&#128101; Kadrolar</button>
  <div class="search-wrapper">
    <span class="search-icon">&#128269;</span>
    <input class="search-input" id="searchInput" type="text" placeholder="Ülke ara..." oninput="searchTeam(this.value)">
  </div>
</div>

<div class="content" id="matchView">
  __MATCH_CONTENT__
</div>

<div id="standingsView">
  <div class="standings-grid" id="standingsGrid"></div>
  <div class="standings-legend">
    <span class="legend-item"><span class="legend-bar legend-bar-green"></span><span>Son 32'ye yükseldi</span></span>
    <span class="legend-item"><span class="legend-bar legend-bar-red"></span><span>Turnuvadan elendi</span></span>
  </div>
</div>

<div id="squadsView">
  <button class="squads-back" id="squadsBackBtn" onclick="showTeamGrid()">&#8592; Tüm Takımlar</button>
  <div id="teamGrid"></div>
  <div class="squad-panel" id="squadPanel">
    <div class="squad-team-header">
      <img class="squad-team-flag" id="squadFlag" src="" alt="">
      <div>
        <div class="squad-team-name" id="squadName"></div>
        <div class="squad-team-group" id="squadGroupLabel"></div>
      </div>
    </div>
    <div id="squadContent"></div>
  </div>
</div>

<div class="lightbox-overlay" id="lightboxOverlay" onclick="closeLightbox()">
  <div class="lightbox-box" onclick="event.stopPropagation()">
    <button class="lightbox-close" onclick="closeLightbox()">&#215;</button>
    <img class="lightbox-img" id="lightboxImg" src="" alt="">
    <div class="lightbox-caption">
      <div class="lightbox-caption-name" id="lightboxName"></div>
      <div class="lightbox-caption-meta" id="lightboxMeta"></div>
    </div>
  </div>
</div>

<script>
  const TODAY = "__TODAY__";
  const TEAMS = __TEAMS_JSON__;
  let teamGridBuilt = false;

  // --- Lightbox ---

  function escAttr(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/"/g,'&quot;');
  }

  function openLightbox(img) {
    document.getElementById('lightboxImg').src = img.src;
    document.getElementById('lightboxName').textContent = img.dataset.name || img.alt;
    var jersey = img.dataset.jersey;
    var pos = img.dataset.position;
    var meta = (jersey ? '#' + jersey : '') + (jersey && pos ? ' — ' : '') + (pos || '');
    document.getElementById('lightboxMeta').textContent = meta;
    document.getElementById('lightboxOverlay').classList.add('open');
    document.body.style.overflow = 'hidden';
  }

  function closeLightbox() {
    document.getElementById('lightboxOverlay').classList.remove('open');
    document.getElementById('lightboxImg').src = '';
    document.body.style.overflow = '';
  }

  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeLightbox();
  });

  // --- View switching ---

  function hideAllViews() {
    document.getElementById('matchView').style.display = 'none';
    document.getElementById('standingsView').style.display = 'none';
    document.getElementById('squadsView').style.display = 'none';
    document.getElementById('searchInput').parentElement.style.display = 'none';
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  }

  function showMatchView() {
    hideAllViews();
    document.getElementById('matchView').style.display = '';
    document.getElementById('searchInput').parentElement.style.display = '';
  }

  function renderStandings(groups) {
    const grid = document.getElementById('standingsGrid');
    if (!groups || !groups.length) {
      grid.innerHTML = '<div style="padding:48px;text-align:center;color:#ef4444">Puan durumu yüklenemedi.</div>';
      return;
    }
    let html = '';
    groups.forEach(function(group) {
      let lastQIdx = -1;
      group.entries.forEach(function(e, i) { if (e.qualify_status === 'qualified') lastQIdx = i; });
      let rows = '';
      group.entries.forEach(function(e, i) {
        let classes = '';
        if (e.qualify_status === 'qualified') {
          classes = 'qualifies-row' + (i === lastQIdx ? ' qualifier-end-row' : '');
        } else if (e.qualify_status === 'eliminated') {
          classes = 'eliminated-row';
        }
        const flagHtml = e.logo
          ? '<img class="s-flag" src="' + escAttr(e.logo) + '" alt="' + escAttr(e.abbr) + '">'
          : '<div class="s-flag-placeholder"></div>';
        rows += '<tr class="' + classes + '">'
          + '<td class="td-rank">' + e.rank + '</td>'
          + '<td class="td-team"><div class="s-team-cell">' + flagHtml
          + '<span class="s-name">' + escAttr(e.team) + '</span>'
          + '<span class="s-abbr">' + escAttr(e.abbr) + '</span>'
          + '</div></td>'
          + '<td>' + e.gp + '</td><td>' + e.w + '</td><td>' + e.d + '</td><td>' + e.l + '</td>'
          + '<td>' + e.gf + '</td><td>' + e.ga + '</td>'
          + '<td class="td-gd">' + e.gd + '</td>'
          + '<td class="td-pts">' + e.pts + '</td>'
          + '</tr>';
      });
      html += '<div class="group-card">'
        + '<div class="group-card-header">' + escAttr(group.name) + '</div>'
        + '<table class="standings-table">'
        + '<thead><tr><th>#</th><th class="th-team">Takım</th>'
        + '<th>O</th><th>G</th><th>B</th><th>M</th>'
        + '<th>AG</th><th>YG</th><th>Av</th><th>P</th></tr></thead>'
        + '<tbody>' + rows + '</tbody></table></div>';
    });
    grid.innerHTML = html;
  }

  function showStandings(btn) {
    hideAllViews();
    document.getElementById('standingsView').style.display = 'block';
    btn.classList.add('active');
    window.scrollTo({ top: 0, behavior: 'instant' });
    document.getElementById('standingsGrid').innerHTML =
      '<div style="padding:48px;text-align:center;color:#6b7280">Yükleniyor...</div>';
    fetch('/api/standings')
      .then(r => r.json())
      .then(data => renderStandings(data))
      .catch(() => {
        document.getElementById('standingsGrid').innerHTML =
          '<div style="padding:48px;text-align:center;color:#ef4444">Puan durumu yüklenemedi.</div>';
      });
  }

  function showSquads(btn) {
    hideAllViews();
    document.getElementById('squadsView').style.display = 'block';
    btn.classList.add('active');
    window.scrollTo({ top: 0, behavior: 'instant' });
    if (!teamGridBuilt) { buildTeamGrid(); teamGridBuilt = true; }
    showTeamGrid();
  }

  // --- Match filtering ---

  function applyVisibility(cards, daySections) {
    daySections.forEach(section => {
      const visible = [...section.querySelectorAll('.match-card')].some(c => c.style.display !== 'none');
      section.style.display = visible ? '' : 'none';
    });
  }

  function filterMatches(filter, btn) {
    showMatchView();
    document.getElementById('searchInput').value = '';
    btn.classList.add('active');
    const cards = document.querySelectorAll('.match-card');
    const daySections = document.querySelectorAll('.day-section');
    cards.forEach(card => {
      let show = false;
      if (filter === 'all') show = true;
      else if (filter === 'live') show = card.dataset.state === 'in';
      else if (filter === 'today') show = card.dataset.date === TODAY;
      else show = card.dataset.group === filter;
      card.style.display = show ? '' : 'none';
    });
    applyVisibility(cards, daySections);
  }

  function searchTeam(query) {
    showMatchView();
    document.querySelector('.filter-btn').classList.add('active');
    const q = query.trim().toLowerCase();
    const cards = document.querySelectorAll('.match-card');
    const daySections = document.querySelectorAll('.day-section');
    if (q) {
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      cards.forEach(card => {
        card.style.display = (card.dataset.home.includes(q) || card.dataset.away.includes(q)) ? '' : 'none';
      });
    } else {
      cards.forEach(card => { card.style.display = ''; });
    }
    applyVisibility(cards, daySections);
  }

  document.addEventListener('DOMContentLoaded', function() {
    const todayCard = document.querySelector('.match-card[data-date="' + TODAY + '"]');
    if (todayCard) {
      const section = todayCard.closest('.day-section');
      if (section) {
        const y = section.getBoundingClientRect().top + window.scrollY - 60;
        window.scrollTo({ top: y, behavior: 'smooth' });
      }
    }
  });

  // --- Squads ---

  function buildTeamGrid() {
    const byGroup = {};
    TEAMS.forEach(t => {
      if (!byGroup[t.group]) byGroup[t.group] = [];
      byGroup[t.group].push(t);
    });

    let html = '';
    Object.keys(byGroup).sort().forEach(group => {
      html += '<div class="teams-group-section"><div class="teams-group-label">' + group + '</div><div class="teams-grid">';
      byGroup[group].forEach(team => {
        const flagHtml = team.logo
          ? '<img class="team-tile-flag" src="' + team.logo + '" alt="' + team.abbr + '">'
          : '<div class="team-tile-flag-ph"></div>';
        html += '<div class="team-tile" id="tile-' + team.id + '" onclick="selectTeamById(' + team.id + ')">'
          + flagHtml
          + '<div class="team-tile-name">' + team.name + '</div>'
          + '</div>';
      });
      html += '</div></div>';
    });

    document.getElementById('teamGrid').innerHTML = html;
  }

  function showTeamGrid() {
    document.getElementById('teamGrid').style.display = '';
    document.getElementById('squadPanel').classList.remove('visible');
    document.getElementById('squadsBackBtn').classList.remove('visible');
    document.querySelectorAll('.team-tile').forEach(t => t.classList.remove('selected'));
    window.scrollTo({ top: 0, behavior: 'instant' });
  }

  function handlePhotoError(img, pid) {
    img.style.display = 'none';
    var w = document.getElementById('pw-' + pid);
    if (w) { w.classList.add('no-photo'); w.innerHTML = '<div class="player-silhouette"></div>'; }
  }

  function selectTeamById(id) {
    const team = TEAMS.find(t => t.id === String(id));
    if (team) selectTeam(team);
  }

  function selectTeam(team) {
    document.querySelectorAll('.team-tile').forEach(t => t.classList.remove('selected'));
    const tile = document.getElementById('tile-' + team.id);
    if (tile) tile.classList.add('selected');

    document.getElementById('teamGrid').style.display = 'none';
    document.getElementById('squadPanel').classList.add('visible');
    document.getElementById('squadsBackBtn').classList.add('visible');

    document.getElementById('squadFlag').src = team.logo || '';
    document.getElementById('squadName').textContent = team.name;
    document.getElementById('squadGroupLabel').textContent = team.group;
    document.getElementById('squadContent').innerHTML = '<div class="squad-loading">Kadro yükleniyor...</div>';
    window.scrollTo({ top: 0, behavior: 'instant' });

    fetch('/api/roster/' + team.id)
      .then(r => r.json())
      .then(players => renderSquad(players))
      .catch(() => {
        document.getElementById('squadContent').innerHTML = '<div class="squad-error">Kadro yüklenemedi.</div>';
      });
  }

  function renderSquad(players) {
    const POS_TR = { 'G': 'Kaleciler', 'D': 'Defanslar', 'M': 'Orta Sahalar', 'F': 'Forvetler' };
    const POS_ORDER = ['G', 'D', 'M', 'F'];

    const byPos = {};
    players.forEach(p => {
      const pos = p.position_abbr || 'F';
      if (!byPos[pos]) byPos[pos] = [];
      byPos[pos].push(p);
    });

    let html = '';
    POS_ORDER.forEach(pos => {
      if (!byPos[pos] || !byPos[pos].length) return;
      const sorted = byPos[pos].slice().sort((a, b) => (a.jersey || 99) - (b.jersey || 99));
      html += '<div class="position-section"><div class="position-header">' + (POS_TR[pos] || pos) + '</div><div class="players-grid">';
      sorted.forEach(p => {
        const photoUrl = p.photo_url || 'https://a.espncdn.com/i/headshots/soccer/players/full/' + p.id + '.png';
        html += '<div class="player-card">'
          + '<div class="player-photo-wrap" id="pw-' + p.id + '">'
          + '<img class="player-photo" src="' + photoUrl + '" alt="' + escAttr(p.name) + '" '
          + 'data-name="' + escAttr(p.name) + '" '
          + 'data-jersey="' + (p.jersey !== null ? p.jersey : '') + '" '
          + 'data-position="' + escAttr(p.position) + '" '
          + 'onerror="handlePhotoError(this,' + p.id + ')" '
          + 'onclick="openLightbox(this)">'
          + '</div>'
          + '<div class="player-info">'
          + '<div class="player-jersey">#' + (p.jersey !== null ? p.jersey : '-') + '</div>'
          + '<div class="player-name">' + p.name + '</div>'
          + (p.age ? '<div class="player-age">' + p.age + ' yaş</div>' : '')
          + '</div></div>';
      });
      html += '</div></div>';
    });

    document.getElementById('squadContent').innerHTML = html || '<div class="squad-loading">Oyuncu bulunamadı.</div>';
  }

  // Theme toggle
  function toggleTheme() {
    var isDark = document.body.classList.toggle('dark');
    localStorage.setItem('wc_theme', isDark ? 'dark' : 'light');
    document.getElementById('themeBtn').textContent = isDark ? '☀' : '☾';
  }
  (function() {
    if (localStorage.getItem('wc_theme') === 'dark') {
      document.body.classList.add('dark');
      document.getElementById('themeBtn').textContent = '☀';
    }
  })();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTML builders
# ---------------------------------------------------------------------------

def _badge(state: str, status: str, live_clock: str, fifa_url: str = "") -> str:
    if state == "in":
        clock = f'<div class="live-clock">{live_clock}</div>' if live_clock else ""
        return f'<span class="status-badge badge-live">&#128308; Canlı</span>{clock}'
    if state == "post":
        hl = f'<br><a class="highlights-link" href="{fifa_url}" target="_blank" rel="noopener">&#9654; Özet</a>' if fifa_url else ""
        return f'<span class="status-badge badge-finished">Bitti</span>{hl}'
    if "Devre" in status:
        return '<span class="status-badge badge-halftime">Devre Arası</span>'
    return '<span class="status-badge badge-scheduled">Planlandı</span>'


def _score_html(m: dict) -> str:
    if m["home_score"] is None:
        return '<div class="score-box"><span class="score-display pending">&#8211;&nbsp;vs&nbsp;&#8211;</span></div>'
    pen_html = ""
    if m.get("home_pens") is not None and m.get("away_pens") is not None:
        pen_html = f'<span class="pen-score">Pen. {m["home_pens"]} &#8211; {m["away_pens"]}</span>'
    return (
        f'<div class="score-box">'
        f'<span class="score-display">{m["home_score"]} &#8211; {m["away_score"]}</span>'
        f'{pen_html}</div>'
    )


def _team_html(m: dict, side: str) -> str:
    name = m[f"{side}_team"]
    abbr = m[f"{side}_abbr"]
    flag = m.get(f"{side}_flag", "")
    is_winner = m["home_winner"] if side == "home" else m.get("away_winner", False)
    flag_html = (
        f'<img class="team-flag" src="{flag}" alt="{abbr}">' if flag
        else '<div class="team-flag-placeholder"></div>'
    )
    winner_class = " winner" if is_winner and m["state"] == "post" else ""

    arrow_html = ""
    if not m["group"].startswith("Group ") and m["state"] == "post":
        if is_winner:
            arrow_html = '<span class="qual-arrow up">&#9650;</span>'
        else:
            arrow_html = '<span class="qual-arrow down">&#9660;</span>'

    if side == "home":
        return (
            f'<div class="team home{winner_class}">'
            f'{arrow_html}'
            f'<span class="team-name">{name}</span>'
            f'<span class="team-abbr">{abbr}</span>'
            f'{flag_html}</div>'
        )
    return (
        f'<div class="team away{winner_class}">'
        f'{flag_html}'
        f'<span class="team-name">{name}</span>'
        f'<span class="team-abbr">{abbr}</span>'
        f'{arrow_html}'
        f'</div>'
    )


def _build_standings_html(groups: list[dict]) -> str:
    if not groups:
        return '<div class="no-matches">Puan durumu yüklenemedi.</div>'

    cards = []
    for group in groups:
        entries = group["entries"]
        last_q_idx = max((i for i, e in enumerate(entries) if e["qualify_status"] == "qualified"), default=-1)

        rows = []
        for i, e in enumerate(entries):
            qs = e["qualify_status"]
            classes = []
            if qs == "qualified":
                classes.append("qualifies-row")
                if i == last_q_idx:
                    classes.append("qualifier-end-row")
            elif qs == "maybe":
                classes.append("maybe-row")
            elif qs == "eliminated":
                classes.append("eliminated-row")
            flag_html = (
                f'<img class="s-flag" src="{e["logo"]}" alt="{e["abbr"]}">' if e["logo"]
                else '<div class="s-flag-placeholder"></div>'
            )
            rows.append(
                f'<tr class="{" ".join(classes)}">'
                f'<td class="td-rank">{e["rank"]}</td>'
                f'<td class="td-team"><div class="s-team-cell">{flag_html}'
                f'<span class="s-name">{e["team"]}</span>'
                f'<span class="s-abbr">{e["abbr"]}</span>'
                f'</div></td>'
                f'<td>{e["gp"]}</td><td>{e["w"]}</td><td>{e["d"]}</td><td>{e["l"]}</td>'
                f'<td>{e["gf"]}</td><td>{e["ga"]}</td>'
                f'<td class="td-gd">{e["gd"]}</td>'
                f'<td class="td-pts">{e["pts"]}</td>'
                f'</tr>'
            )
        cards.append(
            f'<div class="group-card">'
            f'<div class="group-card-header">{group["name"]}</div>'
            f'<table class="standings-table">'
            f'<thead><tr><th>#</th><th class="th-team">Takım</th>'
            f'<th>O</th><th>G</th><th>B</th><th>M</th>'
            f'<th>AG</th><th>YG</th><th>Av</th><th>P</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        )
    return "\n".join(cards)


def build_html(matches: list[dict], groups: list[dict]) -> str:
    now_trt = datetime.now(TRT)
    today_str = now_trt.strftime("%Y-%m-%d")
    updated_str = now_trt.strftime("%H:%M TRT")

    unique_groups = sorted({m["group"] for m in matches if m["group"] and m["group"].startswith("Group ")})
    group_buttons = "\n  ".join(
        f'<button class="filter-btn" onclick="filterMatches(\'{g}\', this)">{g}</button>'
        for g in unique_groups
    )

    # Teams JSON for the squads view (from standings data)
    teams_list = []
    seen_ids = set()
    for group in groups:
        for e in group["entries"]:
            if e["id"] and e["id"] not in seen_ids:
                seen_ids.add(e["id"])
                teams_list.append({
                    "id":    e["id"],
                    "name":  e["team"],
                    "abbr":  e["abbr"],
                    "logo":  e["logo"],
                    "group": group["name"],
                })
    teams_json = json.dumps(teams_list, ensure_ascii=False)

    # Match cards grouped by date
    by_date: dict[str, list] = {}
    for m in matches:
        by_date.setdefault(m["date_trt"], []).append(m)

    DAY_TR = {
        "Monday": "Pazartesi", "Tuesday": "Salı", "Wednesday": "Çarşamba",
        "Thursday": "Perşembe", "Friday": "Cuma", "Saturday": "Cumartesi", "Sunday": "Pazar",
    }
    MONTH_TR = {
        "Jan": "Oca", "Feb": "Şub", "Mar": "Mar", "Apr": "Nis",
        "May": "May", "Jun": "Haz", "Jul": "Tem", "Aug": "Ağu",
    }

    sections_html = []
    for date_key in sorted(by_date.keys()):
        day_matches = by_date[date_key]
        dt = datetime.strptime(date_key, "%Y-%m-%d")
        day_en = dt.strftime("%A")
        date_parts = dt.strftime("%d %b").split(" ")
        day_tr = DAY_TR.get(day_en, day_en)
        month_tr = MONTH_TR.get(date_parts[1], date_parts[1])
        today_marker = " — Bugün" if date_key == today_str else ""
        header = f"{day_tr}, {date_parts[0]} {month_tr}{today_marker}"

        cards_html = []
        for m in day_matches:
            state = m["state"]
            card_class = "match-card" + (" live" if state == "in" else " finished" if state == "post" else "")
            card = (
                f'<div class="{card_class}" data-date="{m["date_trt"]}" data-group="{m["group"]}"'
                f' data-state="{state}" data-home="{m["home_team"].lower()}" data-away="{m["away_team"].lower()}">'
                f'<div class="match-inner">'
                f'<div class="match-time"><div class="time">{m["time_trt"]}</div>'
                f'<div class="trt-label">TRT</div><div class="group-tag">{m["group"]}</div></div>'
                f'{_team_html(m, "home")}'
                f'{_score_html(m)}'
                f'{_team_html(m, "away")}'
                f'<div class="match-status">{_badge(state, m["status"], m["live_clock"], m.get("fifa_url") or "")}'
                f'<div class="venue-text">{m["venue"]}</div></div>'
                f'</div></div>'
            )
            cards_html.append(card)

        sections_html.append(
            f'<div class="day-section"><div class="day-header">{header}</div>'
            + "\n".join(cards_html) + "</div>"
        )

    match_content = "\n".join(sections_html) if sections_html else '<div class="no-matches">Maç bulunamadı.</div>'

    html = HTML_TEMPLATE
    html = html.replace("__UPDATED__", updated_str)
    html = html.replace("__TODAY__", today_str)
    html = html.replace("__GROUP_BUTTONS__", group_buttons)
    html = html.replace("__MATCH_CONTENT__", match_content)
    html = html.replace("__TEAMS_JSON__", teams_json)
    return html


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    matches = get_matches()
    groups = get_standings(matches)
    return Response(build_html(matches, groups), mimetype="text/html; charset=utf-8")


@app.route("/api/scores")
def api_scores():
    return jsonify(get_matches())


@app.route("/api/standings")
def api_standings():
    matches = get_matches()
    groups = get_standings(matches)
    return jsonify(_annotate_qualify_status(groups, matches))


@app.route("/player-photo/<player_id>")
def player_photo(player_id):
    if not player_id.replace("-", "").replace("_", "").isalnum():
        return "", 400
    path = PHOTOS_DIR / f"{player_id}.jpg"
    if path.exists():
        return send_file(path, mimetype="image/jpeg")
    return "", 404


@app.route("/api/roster/<team_id>")
def api_roster(team_id):
    players = get_roster(team_id)
    result = []
    for p in players:
        photo_url = f"/player-photo/{p['id']}" if (PHOTOS_DIR / f"{p['id']}.jpg").exists() else None
        result.append({**p, "photo_url": photo_url})
    return jsonify(result)


if __name__ == "__main__":
    print("=" * 50)
    print("  FIFA Dünya Kupası 2026 Takip Sistemi")
    print("  http://localhost:5000 adresini açın")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)
