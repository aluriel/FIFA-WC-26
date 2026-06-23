#!/usr/bin/env python3
"""
Fetch World Cup 2026 match data from ESPN's public API.
Converts all match times to Turkish time (UTC+3, TRT — fixed offset, no DST).
Saves structured JSON to .tmp/worldcup_scores.json.
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

ESPN_API_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
    "?dates=20260611-20260719&limit=200"
)

TRT = timezone(timedelta(hours=3), name="TRT")

OUTPUT_PATH = Path(__file__).parent.parent / ".tmp" / "worldcup_scores.json"

STATUS_MAP = {
    "STATUS_SCHEDULED":   "Planlandı",
    "STATUS_IN_PROGRESS": "Canlı",
    "STATUS_HALFTIME":    "Devre Arası",
    "STATUS_FULL_TIME":   "Bitti",
    "STATUS_FINAL":       "Bitti",
    "STATUS_END_PERIOD":  "Bitti",
}


def fetch_matches() -> list[dict]:
    try:
        resp = requests.get(
            ESPN_API_URL,
            timeout=20,
            headers={"User-Agent": "WorldCupTracker/1.0"},
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"[HATA] ESPN API isteği başarısız: {e}", file=sys.stderr)
        sys.exit(1)

    events = resp.json().get("events", [])
    if not events:
        print("[UYARI] ESPN API'den hiç maç dönmedi.", file=sys.stderr)

    return [_parse_event(e) for e in events]


def _parse_event(event: dict) -> dict:
    comp = event["competitions"][0]
    status_obj = comp.get("status", {})
    status_type = status_obj.get("type", {})
    state = status_type.get("state", "pre")  # "pre", "in", "post"

    competitors = comp.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0] if competitors else {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1] if len(competitors) > 1 else {})

    # Scores are None for unplayed matches
    home_score = int(home.get("score", 0)) if state != "pre" else None
    away_score = int(away.get("score", 0)) if state != "pre" else None

    # UTC → TRT
    utc_str = comp.get("date", "")
    utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    trt_dt = utc_dt.astimezone(TRT)

    # Group label from altGameNote: "FIFA World Cup, Group A" → "Group A"
    alt_note = comp.get("altGameNote", "")
    group = alt_note.split(", ", 1)[1] if ", " in alt_note else (
        event.get("season", {}).get("slug", "").replace("-", " ").title()
    )

    status_name = status_type.get("name", "STATUS_SCHEDULED")
    status_display = STATUS_MAP.get(status_name, status_type.get("description", "Bilinmiyor"))

    live_clock = status_obj.get("displayClock", "") if state == "in" else ""

    venue = comp.get("venue", {})
    city = venue.get("address", {}).get("city", "")
    venue_str = f"{venue.get('fullName', '')}{', ' + city if city else ''}"

    return {
        "match_id":       event["id"],
        "group":          group,
        "home_team":      home.get("team", {}).get("displayName", ""),
        "home_abbr":      home.get("team", {}).get("abbreviation", ""),
        "away_team":      away.get("team", {}).get("displayName", ""),
        "away_abbr":      away.get("team", {}).get("abbreviation", ""),
        "home_score":     home_score,
        "away_score":     away_score,
        "home_winner":    home.get("winner", False),
        "date_utc":       utc_dt.strftime("%Y-%m-%dT%H:%M"),
        "date_trt":       trt_dt.strftime("%Y-%m-%d"),
        "time_trt":       trt_dt.strftime("%H:%M"),
        "date_label":     trt_dt.strftime("%d %b %Y"),
        "day_label":      trt_dt.strftime("%A, %d %B %Y"),
        "status":         status_display,
        "status_raw":     status_name,
        "state":          state,
        "live_clock":     live_clock,
        "venue":          venue_str,
        "fetched_at":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main():
    print("Dünya Kupası maçları yükleniyor...")
    matches = fetch_matches()
    matches.sort(key=lambda m: m["date_utc"])

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(matches, indent=2, ensure_ascii=False), encoding="utf-8")

    total = len(matches)
    finished = sum(1 for m in matches if m["state"] == "post")
    live = sum(1 for m in matches if m["state"] == "in")
    scheduled = sum(1 for m in matches if m["state"] == "pre")

    print(f"  {total} maç yüklendi: {finished} bitti, {live} canlı, {scheduled} bekliyor")
    print(f"  Kaydedildi: {OUTPUT_PATH}")

    today_trt = datetime.now(TRT).strftime("%Y-%m-%d")
    today_matches = [m for m in matches if m["date_trt"] == today_trt]
    if today_matches:
        print(f"\n  Bugünkü maçlar (TRT):")
        for m in today_matches:
            if m["home_score"] is not None:
                score = f"{m['home_score']}-{m['away_score']}"
            else:
                score = "vs"
            print(f"    [{m['time_trt']} TRT] {m['home_team']} {score} {m['away_team']} ({m['status']}) — {m['group']}")


if __name__ == "__main__":
    main()
