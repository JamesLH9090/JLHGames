#!/usr/bin/env python3
"""Build a searchable SQLite + CSV catalogue of your Steam, Epic and GOG games.

Owned-game lists come from Lutris (~/.local/share/lutris/pga.db) — re-sync your
accounts in Lutris to pick up new purchases. Metadata comes from keyless public
APIs: Steam store (genres, categories, release date, Metacritic), SteamSpy (user
tags, review counts), ProtonDB (Linux compatibility tier) and GOG (genres/tags
for GOG-owned games). Epic and GOG titles are matched to Steam by name.

All HTTP responses are cached in cache.db, so re-runs only fetch new games.

Usage: python3 build_game_library.py [--out DIR] [--workers N]
"""
import argparse
import csv
import difflib
import json
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

LUTRIS_DB = Path.home() / ".local/share/lutris/pga.db"
SERVICES = {"steam": "Steam", "egs": "Epic", "gog": "GOG"}
STORE_NAME_PAT = re.compile(r"\b(soundtrack|ost|artbook|art book|dedicated server|sdk|editor|benchmark|demo|ptr|playtest|test branch|public testing)\b|beta$", re.I)
EDITION_PAT = re.compile(
    r"\b(definitive|enhanced|complete|ultimate|deluxe|special|anniversary|gold|standard|digital deluxe|"
    r"game of the year|goty|directors cut|final cut|remastered|redux|re elected|hd)\b( edition)?$"
)
MODE_CATEGORIES = {
    "Single-player", "Multi-player", "Co-op", "Online Co-op", "Shared/Split Screen Co-op",
    "Shared/Split Screen", "PvP", "Online PvP", "Shared/Split Screen PvP", "LAN Co-op", "LAN PvP",
    "Cross-Platform Multiplayer", "MMO", "Remote Play Together",
}
MAX_TAGS = 20
USER_AGENT = "game-library-builder/1.0 (personal use)"

# Minimum seconds between requests per endpoint (Steam store allows ~200 per 5 minutes).
RATE_LIMITS = {"appdetails": 1.6, "storesearch": 1.1, "steamspy": 1.0, "protondb": 0.2, "gog": 0.3}


class Http:
    """Rate-limited, cached JSON fetcher. Thread-safe."""

    def __init__(self, cache_path):
        self.db = sqlite3.connect(cache_path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS http (url TEXT PRIMARY KEY, status INTEGER, body TEXT, fetched REAL)")
        self.db_lock = threading.Lock()
        self.locks = {k: threading.Lock() for k in RATE_LIMITS}
        self.last = {k: 0.0 for k in RATE_LIMITS}

    def get(self, bucket, url):
        with self.db_lock:
            row = self.db.execute("SELECT status, body FROM http WHERE url=?", (url,)).fetchone()
        if row:
            return json.loads(row[1]) if row[0] == 200 else None
        for attempt in range(6):
            with self.locks[bucket]:
                wait = self.last[bucket] + RATE_LIMITS[bucket] - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self.last[bucket] = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    status = resp.status
            except urllib.error.HTTPError as e:
                if e.code in (429, 403) or e.code >= 500:
                    backoff = 60 * (attempt + 1) if e.code in (429, 403) else 5
                    print(f"  [{bucket}] HTTP {e.code}, backing off {backoff}s", file=sys.stderr)
                    with self.locks[bucket]:
                        time.sleep(backoff)
                    continue
                body, status = "", e.code  # 404 etc.: cache as "no data"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                print(f"  [{bucket}] {e}, retrying", file=sys.stderr)
                time.sleep(5)
                continue
            if status == 200:
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data, status = None, 0
            else:
                data = None
            with self.db_lock:
                self.db.execute("INSERT OR REPLACE INTO http VALUES (?,?,?,?)", (url, status, body, time.time()))
                self.db.commit()
            return data
        return None


def normalize(title):
    t = title.lower().replace("&", " and ")
    t = re.sub(r"[™®©'’]", "", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def base_title(title):
    """Normalized title with edition suffixes stripped, for dedup/matching."""
    t = normalize(title)
    prev = None
    while prev != t:
        prev = t
        t = EDITION_PAT.sub("", t).strip()
    return t


def load_owned():
    """Return list of dicts: one per owned store entry, non-games filtered out."""
    db = sqlite3.connect(f"file:{LUTRIS_DB}?mode=ro", uri=True)
    installed = {}
    for service, sid, inst, playtime, lastplayed in db.execute(
        "SELECT service, service_id, installed, playtime, lastplayed FROM games WHERE service IS NOT NULL"
    ):
        installed[(service, sid)] = (bool(inst), playtime or 0, lastplayed or 0)

    owned, skipped = [], 0
    for service, appid, name, details in db.execute(
        f"SELECT service, appid, name, details FROM service_games WHERE service IN ({','.join('?' * len(SERVICES))})",
        list(SERVICES),
    ):
        d = json.loads(details or "{}")
        if service == "egs":
            cats = {c["path"] for c in d.get("categories", [])}
            if d.get("mainGameItem") or "addons" in cats or "games" not in cats:
                skipped += 1
                continue
        if service == "gog" and (d.get("isGame") is False or d.get("isMovie")):
            skipped += 1
            continue
        if STORE_NAME_PAT.search(name):
            skipped += 1
            continue
        inst, playtime, lastplayed = installed.get((service, appid), (False, 0, 0))
        if service == "steam":
            playtime = max(playtime, (d.get("playtime_forever") or 0) / 60)
        owned.append({
            "service": service, "id": appid, "name": name, "details": d,
            "installed": inst, "playtime": playtime, "lastplayed": lastplayed,
        })
    print(f"Lutris: {len(owned)} owned games, {skipped} DLC/soundtracks/tools skipped")
    return owned


def group_titles(owned):
    """Merge the same game owned on several stores into one entry."""
    groups = {}
    for o in owned:
        groups.setdefault(base_title(o["name"]) or normalize(o["name"]), []).append(o)
    return list(groups.values())


def search_queries(name):
    """Query variants, most specific first. Steam search returns nothing for e.g. 'Alba - A Wildlife Adventure'."""
    clean = re.sub(r"[™®©]", "", name)
    variants = [clean, re.sub(r"\s+[-–—|]\s+", " ", clean), base_title(name)]
    head = re.split(r"\s+[-–—|]\s+|:\s", clean)[0]
    if len(head) >= 4:
        variants.append(head)
    return list(dict.fromkeys(v for v in variants if v))


def score_match(name, candidate):
    if normalize(candidate) == normalize(name):
        return 1.0
    if base_title(candidate) == base_title(name):
        return 0.97
    if re.findall(r"\d+", base_title(candidate)) != re.findall(r"\d+", base_title(name)):
        return 0.0  # different sequel, e.g. 'The Outer Worlds' vs 'The Outer Worlds 2'
    return difflib.SequenceMatcher(None, base_title(name), base_title(candidate)).ratio()


def match_steam(http, name):
    for query in search_queries(name):
        q = urllib.parse.quote(query)
        data = http.get("storesearch", f"https://store.steampowered.com/api/storesearch/?term={q}&cc=us&l=en")
        items = [i for i in (data or {}).get("items", []) if i.get("type") == "app"]
        if not items:
            continue
        # Some editions are DLC on Steam (e.g. 'Ethan Carter Redux' is an upgrade); only accept real games.
        for it in sorted(items, key=lambda it: -score_match(name, it["name"])):
            if score_match(name, it["name"]) < 0.9:
                break
            if steam_type(http, it["id"]) in (None, "game"):
                return it["id"]
    return None


def steam_type(http, appid):
    d = http.get("appdetails", f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=us&l=en")
    info = (d or {}).get(str(appid), {})
    return info["data"].get("type") if info.get("success") else None


def year_from(text):
    m = re.search(r"(19|20)\d\d", text or "")
    return int(m.group()) if m else None


def enrich(http, group):
    stores = sorted({SERVICES[o["service"]] for o in group})
    primary = next((o for s in ("steam", "gog", "egs") for o in group if o["service"] == s))
    steam_id = next((int(o["id"]) for o in group if o["service"] == "steam"), None)
    gog_id = next((o["id"] for o in group if o["service"] == "gog"), None)
    epic_id = next((o["id"] for o in group if o["service"] == "egs"), None)
    if steam_id is None:
        steam_id = match_steam(http, primary["name"])

    row = {
        "title": primary["name"], "stores": ", ".join(stores), "year": None, "release_date": None,
        "genres": [], "tags": [], "modes": [], "metacritic": None, "steam_review_pct": None,
        "steam_reviews": None, "protondb": None, "linux_native": None, "developers": None,
        "description": None, "installed": any(o["installed"] for o in group),
        "playtime_hours": round(sum(o["playtime"] for o in group), 1),
        "steam_appid": steam_id, "gog_id": gog_id, "epic_id": epic_id, "metadata": [],
    }

    if steam_id:
        d = http.get("appdetails", f"https://store.steampowered.com/api/appdetails?appids={steam_id}&cc=us&l=en")
        info = (d or {}).get(str(steam_id), {})
        if info.get("success"):
            s = info["data"]
            if s.get("type") not in (None, "game"):
                return None  # owned on Steam but it's a DLC, demo, tool, etc.
            row["metadata"].append("steam")
            row["genres"] = [g["description"] for g in s.get("genres", [])]
            row["modes"] = [c["description"] for c in s.get("categories", []) if c["description"] in MODE_CATEGORIES]
            row["release_date"] = s.get("release_date", {}).get("date") or None
            row["year"] = year_from(row["release_date"])
            row["metacritic"] = (s.get("metacritic") or {}).get("score")
            row["linux_native"] = (s.get("platforms") or {}).get("linux")
            row["developers"] = ", ".join(s.get("developers", [])) or None
            row["description"] = s.get("short_description") or None

        spy = http.get("steamspy", f"https://steamspy.com/api.php?request=appdetails&appid={steam_id}")
        if spy and spy.get("name"):
            row["metadata"].append("steamspy")
            tags = spy.get("tags") or {}
            if isinstance(tags, dict):
                row["tags"] = [t.strip() for t, _ in sorted(tags.items(), key=lambda kv: -kv[1])][:MAX_TAGS]
            pos, neg = spy.get("positive") or 0, spy.get("negative") or 0
            if pos + neg:
                row["steam_review_pct"] = round(100 * pos / (pos + neg))
                row["steam_reviews"] = pos + neg

        pdb = http.get("protondb", f"https://www.protondb.com/api/v1/reports/summaries/{steam_id}.json")
        if pdb:
            row["protondb"] = pdb.get("tier")

    if gog_id and (not row["genres"] or not row["tags"] or not row["year"]):
        g = http.get("gog", f"https://api.gog.com/v2/games/{gog_id}?locale=en-US")
        emb = (g or {}).get("_embedded") or {}
        if emb:
            row["metadata"].append("gog")
            row["genres"] = row["genres"] or [t["name"] for t in emb.get("tags", [])]
            row["tags"] = row["tags"] or [p["name"] for p in emb.get("properties", [])][:MAX_TAGS]
            row["modes"] = row["modes"] or [f["name"] for f in emb.get("features", []) if "player" in f["name"].lower() or "co-op" in f["name"].lower()]
            rd = (emb.get("product") or {}).get("globalReleaseDate")
            row["release_date"] = row["release_date"] or (rd[:10] if rd else None)
            row["year"] = row["year"] or year_from(rd)
            row["developers"] = row["developers"] or ", ".join(d["name"] for d in emb.get("developers", [])) or None
            if row["linux_native"] is None:
                row["linux_native"] = any(os_["operatingSystem"]["name"] == "linux" for os_ in emb.get("supportedOperatingSystems", []))
        lutris_gog = next(o["details"] for o in group if o["service"] == "gog")
        if not row["genres"] and lutris_gog.get("category"):
            row["genres"] = [lutris_gog["category"]]
        if not row["year"]:
            row["year"] = year_from((lutris_gog.get("releaseDate") or {}).get("date"))

    if not row["developers"] and epic_id:
        row["developers"] = next(o["details"].get("developer") for o in group if o["service"] == "egs") or None
    row["image"] = cover_image(group, steam_id if "steam" in row["metadata"] else None)
    return row


def cover_image(group, steam_id):
    """Landscape cover URL: Steam header, else GOG tile, else Epic wide box art."""
    if steam_id:
        return f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{steam_id}/header.jpg"
    for o in group:
        d = o["details"]
        if o["service"] == "gog" and d.get("image"):
            return f"https:{d['image']}_392.jpg"
        if o["service"] == "egs":
            for kind in ("DieselGameBox", "OfferImageWide", "DieselStoreFrontWide"):
                url = next((k["url"] for k in d.get("keyImages", []) if k["type"] == kind), None)
                if url:
                    return f"{url}?h=270&w=480&resize=1"
    return None


SCHEMA = """
CREATE TABLE games (
    id INTEGER PRIMARY KEY, title TEXT, stores TEXT, year INTEGER, release_date TEXT,
    genres TEXT, tags TEXT, modes TEXT, metacritic INTEGER, steam_review_pct INTEGER,
    steam_reviews INTEGER, protondb TEXT, linux_native INTEGER, developers TEXT,
    installed INTEGER, playtime_hours REAL, description TEXT,
    steam_appid INTEGER, gog_id TEXT, epic_id TEXT, image TEXT, metadata TEXT
);
-- One row per (game, label) so you can filter with plain SQL joins.
CREATE TABLE game_labels (game_id INTEGER REFERENCES games(id), kind TEXT, label TEXT);
CREATE INDEX game_labels_label ON game_labels(label);
"""
COLUMNS = ["title", "stores", "year", "release_date", "genres", "tags", "modes", "metacritic",
           "steam_review_pct", "steam_reviews", "protondb", "linux_native", "developers",
           "installed", "playtime_hours", "description", "steam_appid", "gog_id", "epic_id", "image", "metadata"]
LIST_COLUMNS = ("genres", "tags", "modes", "metadata")


def write_outputs(rows, out_dir):
    rows.sort(key=lambda r: r["title"].lower())
    db_path, csv_path = out_dir / "games.db", out_dir / "games.csv"
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    for i, r in enumerate(rows, 1):
        vals = [", ".join(r[c]) if c in LIST_COLUMNS else r[c] for c in COLUMNS]
        db.execute(f"INSERT INTO games (id, {', '.join(COLUMNS)}) VALUES (?{', ?' * len(COLUMNS)})", [i, *vals])
        for kind in ("genres", "tags", "modes"):
            db.executemany("INSERT INTO game_labels VALUES (?,?,?)", [(i, kind[:-1], v) for v in dict.fromkeys(r[kind])])
        db.executemany("INSERT INTO game_labels VALUES (?,?,?)", [(i, "store", s) for s in r["stores"].split(", ")])
    db.commit()
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow(["; ".join(r[c]) if c in LIST_COLUMNS else ("" if r[c] is None else r[c]) for c in COLUMNS])
    return db_path, csv_path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, help="only process the first N titles (for testing)")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    groups = group_titles(load_owned())[: args.limit]
    print(f"{len(groups)} unique titles after merging duplicates across stores")
    http = Http(args.out / "cache.db")

    rows, dropped, start = [], 0, time.monotonic()
    with ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(enrich, http, g): g for g in groups}
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                row = fut.result()
            except Exception as e:  # never lose a whole run to one bad record
                g = futures[fut]
                print(f"  error on {g[0]['name']!r}: {e}", file=sys.stderr)
                continue
            if row is None:
                dropped += 1
            else:
                rows.append(row)
            if n % 25 == 0 or n == len(groups):
                print(f"  {n}/{len(groups)}  ({time.monotonic() - start:.0f}s elapsed)", flush=True)

    db_path, csv_path = write_outputs(rows, args.out)
    no_meta = sum(1 for r in rows if not r["metadata"])
    print(f"\nWrote {len(rows)} games ({dropped} non-games dropped, {no_meta} with no metadata found)")
    print(f"  {db_path}\n  {csv_path}")
    if args.out.resolve() == Path(__file__).resolve().parent:
        from publish import apply_visibility
        shown, hidden = apply_visibility(db_path)
        print(f"  docs/games.json: {shown} public, {hidden} hidden (see visibility.json)")


if __name__ == "__main__":
    main()
