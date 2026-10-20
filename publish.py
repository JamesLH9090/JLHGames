#!/usr/bin/env python3
"""Write the public docs/games.json from games.db, applying visibility.json.

visibility.json (kept out of git so the hidden list isn't public):
  hide_tags  games with any of these tags/genres are hidden...
  show       ...unless listed here (fixes wrongly tagged games)
  hide       always hidden

Usage: python3 publish.py          # regenerate docs/games.json
       python3 publish.py --push   # ...and commit + push it to GitHub Pages
"""
import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "games.db"
VISIBILITY = HERE / "visibility.json"
OUT = HERE / "docs" / "games.json"
# Install state, playtime and store account IDs stay private.
PRIVATE_COLUMNS = {"installed", "playtime_hours", "gog_id", "epic_id", "public", "hidden_reason"}
DEFAULT_VISIBILITY = {"hide_tags": ["Hentai", "NSFW"], "hide": [], "show": []}


def load_visibility():
    if not VISIBILITY.exists():
        save_visibility(DEFAULT_VISIBILITY)
    return {**DEFAULT_VISIBILITY, **json.loads(VISIBILITY.read_text())}


def save_visibility(v):
    for key in ("hide", "show"):
        v[key] = sorted(set(v.get(key, [])), key=str.lower)
    VISIBILITY.write_text(json.dumps(v, indent=2, ensure_ascii=False) + "\n")


def hidden_reason(title, labels, v):
    """Why a game is hidden from the public site, or None if it's public."""
    if title in v["hide"]:
        return "hidden manually"
    if title in v["show"]:
        return None
    hits = [t for t in v["hide_tags"] if t in labels]
    return f"tag: {', '.join(hits)}" if hits else None


def apply_visibility(db_path=DB, out_path=OUT):
    """Mark games.public in the DB and write the public JSON. Returns (shown, hidden)."""
    v = load_visibility()
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cols = {r[1] for r in db.execute("PRAGMA table_info(games)")}
    if "public" not in cols:
        db.execute("ALTER TABLE games ADD COLUMN public INTEGER DEFAULT 1")
        db.execute("ALTER TABLE games ADD COLUMN hidden_reason TEXT")
    labels = {}
    for gid, label in db.execute("SELECT game_id, label FROM game_labels WHERE kind IN ('tag', 'genre')"):
        labels.setdefault(gid, set()).add(label)

    public = []
    hidden = 0
    for row in db.execute("SELECT * FROM games ORDER BY id").fetchall():
        reason = hidden_reason(row["title"], labels.get(row["id"], set()), v)
        db.execute("UPDATE games SET public = ?, hidden_reason = ? WHERE id = ?", (reason is None, reason, row["id"]))
        if reason:
            hidden += 1
        else:
            public.append({k: row[k] for k in row.keys() if k not in PRIVATE_COLUMNS})
    db.commit()

    unknown = (set(v["hide"]) | set(v["show"])) - {r[0] for r in db.execute("SELECT title FROM games")}
    for title in sorted(unknown):
        print(f"  warning: visibility.json title not in library: {title!r}", file=sys.stderr)

    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps({"generated": time.strftime("%Y-%m-%d"), "games": public}, separators=(",", ":")))
    return len(public), hidden


def set_hidden(title, hide):
    """Hide or unhide one game, keeping visibility.json minimal."""
    v = load_visibility()
    v["hide"] = [t for t in v["hide"] if t != title]
    v["show"] = [t for t in v["show"] if t != title]
    db = sqlite3.connect(DB)
    labels = {r[0] for r in db.execute(
        "SELECT label FROM game_labels JOIN games ON games.id = game_id WHERE title = ? AND kind IN ('tag', 'genre')", (title,))}
    tag_hidden = hidden_reason(title, labels, v) is not None
    if hide and not tag_hidden:
        v["hide"].append(title)
    elif not hide and tag_hidden:
        v["show"].append(title)  # override the tag rule
    save_visibility(v)
    return apply_visibility()


def push():
    def git(*args):
        return subprocess.run(["git", *args], cwd=HERE, check=True, capture_output=True, text=True).stdout
    if not git("status", "--porcelain", "docs"):
        print("Nothing changed in docs/ — site already up to date.")
        return
    git("add", "docs")
    git("commit", "-m", "Update game library")
    git("push")
    print("Pushed — GitHub Pages updates in a minute or two.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--push", action="store_true", help="commit and push docs/ to GitHub")
    args = ap.parse_args()
    shown, hidden = apply_visibility()
    print(f"{OUT}: {shown} public games, {hidden} hidden")
    if args.push:
        push()


if __name__ == "__main__":
    main()
