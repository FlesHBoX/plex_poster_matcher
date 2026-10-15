# Plex Poster Matcher (DarkMatte / djchrisallen)

Scans your Plex movie and TV libraries, checks TheposterDB (TPDb) for a
poster uploaded by specific users (default: DarkMatte, djchrisallen), and
applies it directly to the matching Plex item — no manual downloading or
file copying needed.

## How it works

Instead of scraping everything those two users have ever uploaded (tens
of thousands of posters, loaded via JavaScript on their profile pages —
would require a full headless browser), the script works in the other
direction:

1. Pull the list of movies/shows from your Plex library.
2. For each one, search TPDb by title to find its poster page.
3. Check that page's uploader list for DarkMatte / djchrisallen.
4. If found, grab the poster's direct (unauthenticated) CDN image URL
   and hand it to Plex's own `uploadPoster(url=...)` — your Plex server
   fetches and applies it itself.

This means it only ever makes as many requests as you have library
items, not as many as they have uploads.

## Setup

```bash
cd plex_poster_matcher
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Find your Plex token

1. Sign in to Plex Web App.
2. Open any item, click the `...` menu -> **Get Info** -> **View XML**.
3. In the URL that opens, copy the value after `X-Plex-Token=`.

(Or search "Finding an authentication token" on Plex's support site —
the exact menu wording shifts between versions.)

### Configure

Copy `.env.example` to `.env` in this folder and fill in your real values:

```
PLEX_URL=http://localhost:32400
PLEX_TOKEN=paste-your-token-here
```

`.env` is picked up automatically every time you run the script — no
more re-typing `export`/`set` in every new terminal window, and it's
listed in `.gitignore` so it's never committed if you push this project
to GitHub (only `.env.example`, which has no real secrets, is meant to
be committed).

Optional overrides go in the same file (see the commented-out lines in
`.env.example`): `TPDB_USERS`, `REQUEST_DELAY`, `OUTPUT_DIR`.

If you'd rather not use a file at all, plain environment variables still
work exactly as before and take priority over `.env`:

```bash
export PLEX_URL="http://localhost:32400"      # Windows: set PLEX_URL=...
export PLEX_TOKEN="paste-your-token-here"
```

## Run it

Always test first with a small slice and `--dry-run`:

```bash
python main.py --dry-run --limit 25
```

Check `output/matched.csv` and `output/unmatched.csv`, then run for real:

```bash
python main.py
```

If you need to stop partway through (Ctrl+C, laptop sleeps, network drops),
just re-run the exact same command — it checks `output/matched.csv` and
`output/unmatched.csv` and automatically skips anything already recorded
there, picking up close to where it left off. Use `--restart` if you
actually want to reprocess everything from scratch instead.

### Going from a dry run to the real thing

A `--dry-run` match gets recorded in `matched.csv` with `applied=False`.
When you then run for real (no `--dry-run`), those items aren't just
skipped — by default they get **re-verified through TPDb again** (so the
match is still fresh, e.g. in case something changed since your dry run).
If you'd rather trust the dry run's results as-is and skip re-hitting
TPDb entirely, add `--use-cache`:

```bash
python main.py --use-cache
```

This applies straight from each item's cached local poster file (or the
cached image URL, if the local file isn't there) and flips `applied` to
`True` — no new TPDb requests for anything already matched. Much faster
for a "I already reviewed the dry run, just apply it" pass over a large
library.

### Re-checking previously unmatched items

Rather than manually deleting `unmatched.csv` to force a recheck (e.g.
after a matcher fix, or once DarkMatte/djchrisallen upload something new),
pass `--reverify`:

```bash
python main.py --reverify
```

This re-runs the full TPDb search for anything currently recorded as
unmatched, while leaving anything already matched-and-applied completely
untouched. Combine with `--dry-run` to preview what would change before
committing to it.

### Adding matches yourself

For a title the scraper can't find (or gets wrong), don't fight the
matcher — add a row to `output/manual.csv` instead:

| title | year | type | source_url | local_file | note | applied |
|---|---|---|---|---|---|---|

Fill in **either** `source_url` **or** `local_file`, not both, and leave
`applied` blank on a new row:

- **`source_url`** — a specific poster's own TPDb page, e.g.
  `https://theposterdb.com/poster/123456` (the *individual poster* page —
  click through from a movie's listing page to get this URL, don't just
  paste the listing page itself). This works for **any** uploader, not
  just the ones in `TPDB_USERS` — handy for someone you found by browsing
  manually. A direct image URL works here too, if you already have one.
- **`local_file`** — path to an image file you made yourself (e.g. via
  postertools.org), for a fully custom poster with no TPDb source at all.

Run `main.py` as normal afterward (dry-run or real) — manual rows are
picked up automatically at the start of every run, get applied to Plex,
recorded into `matched.csv` (with your `note` as the uploader), and
`applied` flips to `True` so that row won't be reprocessed again. A
manual entry always overrides whatever the automated scraper found for
that title. `--restart` never touches `manual.csv` — it's your own
curated data, not something the script regenerates.

### Reviewing titles with multiple poster options

`find_target_posters()` always checks every uploader in `TPDB_USERS`, not
just the first one that matches — when more than one of your configured
uploaders has a poster for the same title, the script picks whichever
one is listed first in `TPDB_USERS` and quietly discards the rest. If
you'd rather see all the options and pick for yourself sometimes, there's
a browsable page for that.

**If your library is already fully processed** (this feature only
catalogs titles as they're freshly searched, so anything matched before
it existed was never recorded), backfill it once with a read-only pass —
this never touches Plex, `matched.csv`, or your downloaded posters, it
only re-runs the TPDb search/lookup step to see what else was out there:

```bash
python main.py --backfill-multi-options
```

This is resumable the same way everything else is (Ctrl+C-safe, just
re-run the same command) — progress is tracked in
`output/multi_options_checked.json`; delete that file if you ever want
to recheck everything from scratch. It still costs one TPDb lookup per
already-matched title, so expect it to take about as long as your
original full run did.

Going forward, a normal run keeps `multi_options.json` up to date on its
own — no backfill needed for anything processed from here on out.

```bash
python main.py                       # normal run — this also writes
                                      # output/multi_options.json for any
                                      # title with more than one option
python generate_overrides_page.py    # builds output/overrides.html
```

### Adding a new uploader without re-running everything

```bash
python main.py --new-user SomeUploader
```

This does two things: adds `SomeUploader` to `TPDB_USERS` in `.env`
(appended at the end — lowest priority, so it can never silently outrank
an uploader you already trust more, unless you manually reorder the list
afterward), then checks that one uploader against everything you've
already processed — but cheaply, by reusing each title's TPDb page you
already found instead of repeating the full search from scratch.

What that means for each title:
- **Already matched** — if `SomeUploader` also has a poster there, it's
  added to `multi_options.json` as an extra option (review it via
  `overrides.html`), never auto-applied over your existing pick.
- **Previously unmatched with no target-uploader poster** — if
  `SomeUploader` has one, that's a genuine new match, applied
  immediately.
- **Ambiguous or never found on TPDb at all** — skipped; there's no
  single reliable page to reuse for these. Run `--reverify` afterward to
  reconsider them (it'll include the new uploader automatically, since
  by then they're already in `TPDB_USERS`).

Open `output/overrides.html` in a browser (double-click it, no server
needed). Click a title to see every option with the uploader's name
underneath; the one the script picked is marked "default." Click a
different poster to override it — your picks are saved in the browser
(`localStorage`), so it's fine to close the page and come back later.

When you're done, click **Export overrides.csv**, then fold it into your
real `manual.csv`:

```bash
python merge_overrides.py            # merges output/overrides.csv into manual.csv
python main.py                       # applies your overrides
```

Re-run `generate_overrides_page.py` any time `multi_options.json`
changes (e.g. after a `--reverify` pass) to pick up newly-discovered
multi-option titles — your existing selections aren't lost, since
they live in the browser, not the generated file.

### Poster organization on disk

Shows each get their own subfolder — `output/posters/shows/Show Name (Year)/` —
containing the main poster plus any season posters found:

```
output/posters/shows/Ted Lasso (2020)/
    Ted Lasso (2020).jpg
    Season 1.jpg
    Season 2.jpg
    Specials.jpg
```

Season posters take one extra request per matched show: TPDb's aggregate
show listing page only ever shows one representative card per uploader,
never season-specific variants — those only exist within that uploader's
own "Set" page. So after picking the main poster, the script follows its
Set link and scans that page for entries titled with a "- Season N" or
"- Specials" suffix. A show's art stays visually consistent rather than
mixing styles, and a season Plex doesn't have yet (or numbers
differently) is silently skipped, not an error.

One real wrinkle worth knowing: a Set isn't guaranteed to be scoped to
just one show. Some uploaders use a Set as a broad themed collection
spanning many different titles — confirmed via a real example: one
Set turned out to be an uploader's entire "Stephen King Universe"
collection (The Stand, Castle Rock, Mr. Mercedes, Under the Dome, Haven,
The Dead Zone, The Shining, and more, all bundled with 11.22.63 in the
SAME Set). Each entry's image URL embeds which show it actually belongs
to, so the script filters to just the show being matched — but this bug
existed for a while before being caught, so if you ran `--backfill-seasons`
before this fix, some shows may have picked up a completely unrelated
show's season posters. Re-running `--backfill-seasons` re-applies correct
posters over any wrong ones — safe to just do that again.

Movies are organized by **Plex Collection** (the ones you've already
set up in Plex itself — this doesn't use anything from TPDb):

- No collection → flat `output/posters/movies/`
- Exactly one collection → automatically filed into
  `output/posters/movies/<Collection Name>/`
- Two or more collections (e.g. Into the Spider-Verse is in Marvel,
  Spider-Man, *and* Spider-Verse) → needs your call. A row appears in
  `output/collection_choices.csv` listing the options; fill in
  `chosen_collection` yourself, same idea as `manual.csv`. Leave it
  blank and the poster just stays in the flat folder until you decide.
  An invalid or since-removed collection name in that column is treated
  the same as blank — it won't crash, it just stays flat.

**If your library predates this feature:**

```bash
python migrate_show_folders.py --dry-run   # preview moving existing
python migrate_show_folders.py             # flat show posters into the
                                            # new per-show subfolders

python migrate_movie_collections.py --dry-run   # preview organizing
python migrate_movie_collections.py             # existing movie posters
                                                 # by Plex Collection —
                                                 # re-run after filling in
                                                 # collection_choices.csv

python main.py --backfill-seasons          # check already-matched shows
                                            # for season posters, reusing
                                            # their known TPDb page
```

`--backfill-seasons` is read-mostly for the main poster (it only applies
season posters, never touches or re-applies the main one) and skips
movies entirely. Safe to re-run if interrupted — re-applying an
already-applied season poster is harmless.

One assumption worth knowing: season matching relies on plexapi's
`Season.index` being the season number, with `0` for Specials. This
hasn't been verified against a live Plex server — if seasons aren't
landing where expected, that's the first thing to check.

Full library runs will take a while — with the default 1-second delay,
~2,000 titles is roughly an hour or two, more when titles need extra
lookups (subtitle fallback searches, resolving ties). Turn `REQUEST_DELAY`
down at your own risk; it's there to be a considerate scraper, not a
technical minimum.

## Reading the output

- **matched.csv** — every item where a poster from a target uploader was
  found and applied (or would be, under `--dry-run`). `local_file` points
  to a saved copy under `output/posters/movies/` or `output/posters/shows/`,
  named `Title (Year).jpg` — handy for eyeballing whether matches look
  right before trusting them at scale. Pass `--no-images` to skip saving
  these and just rely on the `image_url` links instead.
- **unmatched.csv** — everything skipped, with a `status`/`reason`:
  - `no_title_match` — TPDb doesn't seem to have this title at all.
  - `ambiguous` — multiple similarly-named TPDb entries; check `tpdb_url`
    manually and adjust the title-matching threshold in `matcher.py` if
    this happens a lot.
  - `no_target_poster` — the title exists on TPDb, just nobody in your
    target list has uploaded a poster for it.
  - `error` — network hiccup or unexpected page structure; safe to
    re-run, it'll just be retried from scratch (no state is cached).

## Known limitations / things worth knowing

- **This scrapes public HTML, not an official API** — TPDb doesn't offer
  one. If the site's markup changes, the CSS/regex selectors in
  `tpdb_client.py` may need updating. If you suddenly get zero matches
  across the board, that's the first thing to check (view page source on
  a search results page and a `/posters/<id>` page, compare to what the
  code expects).
- **Symbol-for-word title stunts aren't handled.** A small number of
  movies use a symbol in place of a word in their official title (e.g.
  "I ♥ Huckabees" instead of "I Heart Huckabees"). TPDb's search doesn't
  associate the symbol with the word, so these won't be found even
  though the matcher itself would score them fine if they showed up in
  the candidate list. Rare enough not to be worth a dedicated fallback —
  just search TPDb by hand for the handful of titles like this.
- **Generic one-word titles were a needle in a haystack — now handled,
  at a cost.** Titles like "RED" or "Mother!" collide with dozens of
  unrelated same-named entries on TPDb (other films, shorts, fan
  projects), spread across many pages of search results with no
  apparent sort order (confirmed: "RED" alone has 28 such pages). When
  the quick search finds nothing matching the target year at all, the
  script now pages through further automatically (up to 40 pages),
  stopping as soon as it finds one that does. This can take a while for
  a title that genuinely needs it (each page is a separate polite,
  rate-limited request) — it only ever triggers for titles that fail
  the cheap first pass, so it doesn't slow down the common case.
- **Season-level posters aren't covered yet.** DarkMatte in particular
  uploads a lot of per-season show posters (e.g. "Andor (2022) - Season
  2"), which this script doesn't currently match against Plex's season
  objects — it only sets the main show poster. Straightforward to add as
  a follow-up if you want it.
- **Collections aren't covered.** TPDb also has "Collection" posters
  (e.g. "Marvel Cinematic Universe") that don't map to a single Plex
  item; out of scope here.
- Be a reasonable citizen of someone else's server: the default 1-second
  delay between requests is intentional, and this is for personal-library
  use, not for bulk-mirroring TPDb's catalog.

### New seasons are picked up automatically

Every normal `python main.py` run ends with a season sync: for each
already-applied show, it compares the seasons Plex has against the season
posters already handled (images in the show's folder plus
`output/seasons_state.json`), and looks up only the missing ones on the
Set page of the uploader whose main poster was applied. Shows with nothing
missing cost one Plex call and no TPDb requests.

- A season the uploader hasn't made a poster for yet is retried every 14
  days, for up to 90 days after first looking.
- Specials (season 0) are only looked for once.
- Manual/custom posters are skipped (there's no uploader Set to check).
- Skip it with `--skip-season-sync`; `--dry-run` previews without recording.
