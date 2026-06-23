#!/usr/bin/env python3
"""
One-time photo downloader for World Cup 2026 player photos.
Uses Wikipedia as the source (free, no API key required).
Saves to .tmp/photos/{espn_player_id}.jpg

Run once before the server, then refresh the Kadrolar page.
After new squads are announced, run again — already-cached photos are skipped.
"""

import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# Force UTF-8 output so international player names don't crash on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/soccer/fifa.world/standings"
ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/teams/{team_id}/roster"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKI_SEARCH = "https://en.wikipedia.org/w/api.php"

PHOTOS_DIR = Path(__file__).parent.parent / ".tmp" / "photos"
HEADERS = {"User-Agent": "WorldCupPhotoDownloader/1.0"}


def fetch_json(url: str) -> dict:
    resp = requests.get(url, timeout=15, headers=HEADERS)
    resp.raise_for_status()
    return resp.json()


def get_team_ids() -> list[tuple[str, str]]:
    data = fetch_json(STANDINGS_URL)
    teams, seen = [], set()
    for group in data.get("children", []):
        for entry in group.get("standings", {}).get("entries", []):
            team = entry.get("team", {})
            tid = team.get("id")
            name = team.get("displayName", "")
            if tid and tid not in seen:
                seen.add(tid)
                teams.append((tid, name))
    return teams


def get_players(team_id: str) -> list[dict]:
    url = ROSTER_URL.format(team_id=team_id)
    data = fetch_json(url)
    return [
        {"id": a.get("id", ""), "name": a.get("displayName", "")}
        for a in data.get("athletes", [])
        if a.get("id") and a.get("displayName")
    ]


def _wiki_request(url: str, params: dict | None = None) -> dict | None:
    """Single Wikipedia API call with retry on rate-limit or timeout."""
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=15, headers=HEADERS)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if r.ok:
                return r.json()
        except requests.Timeout:
            time.sleep(2 * (attempt + 1))
        except Exception:
            break
    return None


def download_wiki_photo(player_id: str, player_name: str) -> bool:
    """Try to download a Wikipedia thumbnail for the player. Returns True if saved."""
    save_path = PHOTOS_DIR / f"{player_id}.jpg"
    if save_path.exists():
        return True

    thumbnail_url = None

    # Strategy 1: direct page summary lookup
    slug = urllib.parse.quote(player_name.replace(" ", "_"))
    data = _wiki_request(WIKI_SUMMARY.format(title=slug))
    if data:
        thumbnail_url = data.get("thumbnail", {}).get("source")

    # Strategy 2: Wikipedia search with "footballer" qualifier
    if not thumbnail_url:
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": player_name + " footballer",
            "gsrlimit": 1,
            "prop": "pageimages",
            "format": "json",
            "pithumbsize": 250,
            "pilimit": 1,
        }
        data = _wiki_request(WIKI_SEARCH, params)
        if data:
            for page in data.get("query", {}).get("pages", {}).values():
                thumb = page.get("thumbnail", {}).get("source")
                if thumb:
                    thumbnail_url = thumb
                    break

    if not thumbnail_url:
        return False

    try:
        img_r = requests.get(thumbnail_url, timeout=20, headers=HEADERS)
        if img_r.ok and img_r.headers.get("Content-Type", "").startswith("image"):
            save_path.write_bytes(img_r.content)
            return True
    except Exception:
        pass

    return False


def main():
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

    print("FIFA Dunya Kupasi 2026 - Oyuncu Fotografi Indirme")
    print("=" * 54)

    print("Takımlar alınıyor...")
    teams = get_team_ids()
    print(f"{len(teams)} takım bulundu.\n")

    all_players = []
    for i, (team_id, team_name) in enumerate(teams, 1):
        try:
            players = get_players(team_id)
            all_players.extend(players)
            print(f"[{i:2}/{len(teams)}] {team_name}: {len(players)} oyuncu")
        except Exception as e:
            print(f"[{i:2}/{len(teams)}] {team_name}: HATA - {e}")

    already = sum(1 for p in all_players if (PHOTOS_DIR / f"{p['id']}.jpg").exists())
    to_download = [p for p in all_players if not (PHOTOS_DIR / f"{p['id']}.jpg").exists()]

    print(f"\nToplam {len(all_players)} oyuncu.")
    if already:
        print(f"  {already} fotograf zaten var - atlaniyor.")
    print(f"  {len(to_download)} fotograf indirilecek...\n")

    found = 0
    not_found = []

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(download_wiki_photo, p["id"], p["name"]): p
            for p in to_download
        }
        for i, future in enumerate(as_completed(futures), 1):
            player = futures[future]
            if future.result():
                found += 1
            else:
                not_found.append(player["name"])
            if i % 50 == 0 or i == len(futures):
                print(f"  {i}/{len(futures)} islendi... ({found} bulundu)")

    total = found + already
    print(f"\n{'='*54}")
    print(f"Tamamlandi: {total}/{len(all_players)} fotograf")
    if not_found:
        print(f"\nFotograf bulunamayan {len(not_found)} oyuncu:")
        for name in sorted(not_found):
            print(f"  - {name}")
    print(f"\nFotograflar: {PHOTOS_DIR}")
    print("Sunucuyu yeniden baslatin (ya da Kadrolar sayfasini acin).")


if __name__ == "__main__":
    main()
