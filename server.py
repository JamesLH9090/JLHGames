#!/usr/bin/env python3
"""Local web UI for browsing games.db. Standard library only.

Usage: python3 server.py [--port 8765] [--db games.db]   then open http://localhost:8765

API (all GET, JSON):
  /api/games   ?q= &genre= &tag= &mode= (repeatable, all must match) &xtag= (repeatable, exclude)
               &store= &protondb= (repeatable, any may match) &year_min= &year_max=
               &min_review= &min_reviews= &installed=1 &unplayed=1
               &sort=title|year|review|metacritic|popularity|playtime|random &order=asc|desc
               &limit= &offset=
  /api/facets  same filters -> label counts among matching games
  /api/game/<id>
"""
import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
INDEX = HERE / "docs" / "index.html"

SORTS = {
    "title": "title COLLATE NOCASE",
    "year": "year",
    "review": "steam_review_pct",
    "metacritic": "metacritic",
    "popularity": "steam_reviews",
    "playtime": "playtime_hours",
    "random": "random()",
}
LABEL_FILTERS = {"genre": "genre", "tag": "tag", "mode": "mode"}  # query param -> game_labels.kind
CARD_COLUMNS = "id, title, year, stores, genres, tags, steam_review_pct, steam_reviews, metacritic, protondb, linux_native, installed, playtime_hours, image"


def build_where(params):
    """Translate query params into a parameterized WHERE clause."""
    where, args = [], []

    def one(name):
        v = params.get(name, [""])[0].strip()
        return v or None

    if q := one("q"):
        like = f"%{q}%"
        where.append("(title LIKE ? OR developers LIKE ? OR tags LIKE ? OR description LIKE ?)")
        args += [like] * 4
    for param, kind in LABEL_FILTERS.items():
        for label in params.get(param, []):
            where.append("id IN (SELECT game_id FROM game_labels WHERE kind = ? AND label = ?)")
            args += [kind, label]
    for label in params.get("xtag", []):
        where.append("id NOT IN (SELECT game_id FROM game_labels WHERE label = ?)")
        args.append(label)
    if stores := params.get("store"):
        where.append(f"id IN (SELECT game_id FROM game_labels WHERE kind = 'store' AND label IN ({','.join('?' * len(stores))}))")
        args += stores
    if tiers := params.get("protondb"):
        clauses = []
        if "native" in tiers:
            clauses.append("linux_native = 1")
        others = [t for t in tiers if t != "native"]
        if others:
            clauses.append(f"protondb IN ({','.join('?' * len(others))})")
            args += others
        where.append(f"({' OR '.join(clauses)})")
    for param, sql in (("year_min", "year >= ?"), ("year_max", "year <= ?"),
                       ("min_review", "steam_review_pct >= ?"), ("min_reviews", "steam_reviews >= ?")):
        if (v := one(param)) and v.isdigit():
            where.append(sql)
            args.append(int(v))
    if one("installed") == "1":
        where.append("installed = 1")
    if one("unplayed") == "1":
        where.append("(playtime_hours IS NULL OR playtime_hours = 0)")
    return (" WHERE " + " AND ".join(where)) if where else "", args


class Handler(BaseHTTPRequestHandler):
    db_path = None

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def db(self):
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        params = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                return self.send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/games":
                return self.send(200, self.games(params))
            if url.path == "/api/facets":
                return self.send(200, self.facets(params))
            if url.path.startswith("/api/game/") and url.path.rsplit("/", 1)[1].isdigit():
                return self.game(int(url.path.rsplit("/", 1)[1]))
            self.send(404, {"error": "not found"})
        except sqlite3.Error as e:
            self.send(500, {"error": str(e)})

    def games(self, params):
        where, args = build_where(params)
        sort = SORTS.get(params.get("sort", ["title"])[0], SORTS["title"])
        order = "DESC" if params.get("order", ["asc"])[0] == "desc" else "ASC"
        limit = min(int(params.get("limit", ["60"])[0] or 60), 500)
        offset = int(params.get("offset", ["0"])[0] or 0)
        # NULLs (unknown year/score) always sort last, whichever direction.
        order_by = sort if sort == "random()" else f"{sort} IS NULL, {sort} {order}, title COLLATE NOCASE"
        with self.db() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM games{where}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT {CARD_COLUMNS} FROM games{where} ORDER BY {order_by} LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
        return {"total": total, "games": [dict(r) for r in rows]}

    def facets(self, params):
        where, args = build_where(params)
        with self.db() as conn:
            ids = f"SELECT id FROM games{where}"
            labels = conn.execute(
                f"SELECT kind, label, COUNT(*) n FROM game_labels WHERE game_id IN ({ids}) "
                "GROUP BY kind, label ORDER BY n DESC, label", args
            ).fetchall()
            tiers = conn.execute(
                f"SELECT COALESCE(protondb, 'unknown') t, COUNT(*) n FROM games{where} GROUP BY t ORDER BY n DESC", args
            ).fetchall()
            native = conn.execute(f"SELECT COUNT(*) FROM games{where}{' AND' if where else ' WHERE'} linux_native = 1", args).fetchone()[0]
            years = conn.execute("SELECT MIN(year), MAX(year) FROM games").fetchone()
        out = {"genre": [], "tag": [], "mode": [], "store": []}
        for r in labels:
            out[r["kind"]].append([r["label"], r["n"]])
        out["protondb"] = [[r["t"], r["n"]] for r in tiers] + [["native", native]]
        out["years"] = list(years)
        return out

    def game(self, gid):
        with self.db() as conn:
            row = conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
        if row is None:
            return self.send(404, {"error": "not found"})
        self.send(200, dict(row))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to reach it from other devices on your LAN")
    ap.add_argument("--db", type=Path, default=HERE / "games.db")
    args = ap.parse_args()
    if not args.db.exists():
        raise SystemExit(f"{args.db} not found — run build_game_library.py first")
    Handler.db_path = args.db.resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Game library at http://localhost:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
