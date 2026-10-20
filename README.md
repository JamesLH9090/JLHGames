# Game library catalogue

Live site: https://jameslh9090.github.io/JLHGames/

`games.db` (SQLite) and `games.csv` list every Steam, Epic and GOG game you own,
merged across stores, with genres, Steam user tags, play modes, release year,
Metacritic, Steam review %, ProtonDB tier, install state and playtime.

## Viewer page

The site is styled to match the JamesLH stream branding (gold crest, `docs/crest.png`) and is meant
for stream viewers: each game has a **Suggest** box that copies a chat message linking back to it
(`?game=<slug>` opens that game), with buttons for Twitch chat and YouTube live. The channel URLs and
message wording are constants at the top of the `<script>` in `docs/index.html`
(`TWITCH`, `YOUTUBE`, `suggestionMessage`) — change the message there if you add a bot command like `!suggest`.

## Hiding games from the public site

`visibility.json` (local only, git-ignored so the list itself isn't public) decides what viewers see:

- `hide_tags` — games with any of these Steam tags/genres are hidden (default: Hentai, NSFW)
- `show` — exceptions to the tag rule, for wrongly tagged games (e.g. LEGO Star Wars is tagged NSFW)
- `hide` — always hidden, for games the tags miss

Easiest way to edit it: run `python3 server.py`, open a game, click **Hide from public site** /
**Show on public site**. Tick *Hidden from public site* in the sidebar to review everything hidden.
Then publish with `python3 publish.py --push`.

## Refresh

1. In Lutris, re-sync your Steam / Epic / GOG sources (picks up new purchases).
2. `python3 build_game_library.py` — cached lookups make re-runs fast; only new games hit the network.
   This rewrites `games.db`, `games.csv` and `docs/games.json`.
   It also re-applies `visibility.json` to `docs/games.json`.
3. Publish: `python3 publish.py --push` — GitHub Pages redeploys in a minute or two.

## What gets published

`docs/` is the GitHub Pages site: `index.html` plus `games.json`. The JSON leaves out install
state, playtime and store account IDs. `games.db`, `games.csv` and `cache.db` stay local
(see `.gitignore`). The same `index.html` detects which mode it's in: with `games.json`
next to it, it filters in the browser; served by `server.py`, it uses the API and shows the
private fields.

Delete `cache.db` to force fresh metadata (full run takes ~25 min due to Steam rate limits).

## Web UI

```fish
python3 server.py          # then open http://localhost:8765
```

Search box, store / Linux-compatibility / year / review-score filters, genre / mode / tag
facets with live counts (tags can be included or excluded), sorting, and a 🎲 *Surprise me*
button that opens a random game matching the current filters. Filters live in the URL, so
bookmark a view you like. `--host 0.0.0.0` makes it reachable from other devices on your LAN.

## Other ways to browse

- GUI: `sudo pacman -S sqlitebrowser`, open `games.db`, use the Browse Data tab's per-column filters.
- Web UI: `pipx install datasette && datasette games.db` → faceted search in your browser.
- Spreadsheet: open `games.csv` (list columns are `;`-separated).

## Example queries

```sql
-- Highly rated roguelikes that run well on Linux
SELECT title, year, stores, steam_review_pct, protondb FROM games
WHERE id IN (SELECT game_id FROM game_labels WHERE label = 'Rogue-like')
  AND protondb IN ('platinum', 'gold') AND steam_review_pct >= 85
ORDER BY steam_review_pct DESC;

-- Couch co-op
SELECT title, year, modes FROM games WHERE modes LIKE '%Shared/Split Screen Co-op%';

-- Never played, story-rich, from the last 5 years
SELECT title, year, genres FROM games
WHERE playtime_hours = 0 AND year >= strftime('%Y', 'now') - 5
  AND id IN (SELECT game_id FROM game_labels WHERE label = 'Story Rich');

-- Games matching two tags at once
SELECT g.title FROM games g
JOIN game_labels a ON a.game_id = g.id AND a.label = 'Metroidvania'
JOIN game_labels b ON b.game_id = g.id AND b.label = 'Pixel Graphics';

-- Most common tags in your library
SELECT label, COUNT(*) FROM game_labels WHERE kind = 'tag' GROUP BY label ORDER BY 2 DESC LIMIT 40;
```

## Caveats

- `year` is the Steam store release date, so re-releases (e.g. *Beneath a Steel Sky*) show the re-release year.
- ~39 titles have no metadata — mostly Epic exclusives / free-to-play (Fortnite, Alan Wake 2, Genshin) not on Steam.
- Epic and GOG games are matched to Steam by name; matching is strict but not perfect.
- EA App and Ubisoft games in Lutris are not included; add `"ea_app"` / `"ubisoft"` to `SERVICES` to include them.
