"""
Match and apply posters from specific TheposterDB uploaders to your Plex library.

Usage:
    export PLEX_URL="http://localhost:32400"
    export PLEX_TOKEN="your-token"
    python main.py                 # applies confident matches, writes reports
    python main.py --dry-run       # does everything except actually touching Plex
    python main.py --limit 25      # only process the first 25 library items (testing)
    python main.py --restart       # ignore any previous progress, start clean
    python main.py --use-cache     # for items already matched but not yet
                                    # applied (e.g. from an earlier --dry-run),
                                    # apply directly from the cached local
                                    # image / URL instead of re-searching TPDb
    python main.py --reverify      # also re-check items previously recorded
                                    # as unmatched (e.g. after a code fix or
                                    # a new TPDb upload) — anything already
                                    # matched AND applied is still skipped
    python main.py --backfill-multi-options
                                    # read-only pass over already-matched
                                    # items purely to populate
                                    # multi_options.json (see below) —
                                    # never touches Plex, matched.csv, or
                                    # downloaded images
    python main.py --new-user NAME # add NAME to TPDB_USERS (saved to
                                    # .env) and quickly check it against
                                    # everything already processed, reusing
                                    # each title's known TPDb page instead
                                    # of a full re-search

Output (rewritten after every change, not just at the end, so progress is
never lost to an interruption):
    output/matched.csv     - posters found and applied (or would-be, if --dry-run)
    output/unmatched.csv   - everything skipped, with a reason
    output/manual.csv      - your own hand-entered matches (see below);
                             created empty with a header on first run if
                             it doesn't exist yet

Manual entries: for a title the scraper can't find or gets wrong, add a
row to output/manual.csv yourself instead of fighting the matcher. Columns:
    title, year, type, source_url, local_file, note, applied
Fill in EITHER source_url OR local_file (not both):
  - source_url: a specific poster's own TPDb page (theposterdb.com/poster/NNNN
    — the individual poster, not the movie's listing page), from ANY
    uploader, not just the ones in TPDB_USERS. The direct image URL gets
    pulled from that page automatically. A direct image URL also works
    here if you already have one.
  - local_file: path to an image file you made yourself (e.g. via
    postertools.org), for a fully custom poster with no TPDb source at all.
Leave "applied" blank/empty for a new row — the script sets it to True
once it's actually pushed to Plex, so it won't reprocess that row again.
A manual entry always overrides whatever the automated scraper found for
that title, applying instead of it.

Checkpointing: an item already recorded in matched.csv or unmatched.csv is
handled based on its recorded state, not just skipped outright:
  - already applied for real                    -> skipped, nothing to do
  - recorded unmatched                           -> skipped, unless
                                                     --reverify is passed (or
                                                     unmatched.csv is deleted),
                                                     in which case it's
                                                     rechecked from scratch
  - matched but not yet applied (a --dry-run
    result, about to become a real run) and
    this run IS a --dry-run                      -> skipped, already known
  - matched but not yet applied, this run is
    real, and --use-cache is set                 -> applied straight from
                                                     the cached image, no
                                                     TPDb request at all
  - matched but not yet applied, this run is
    real, --use-cache NOT set                    -> re-runs the full TPDb
                                                     search/match, so the
                                                     match still gets
                                                     freshly verified
Pass --restart to ignore all of the above and reprocess everything.
"""
import argparse
import csv
import datetime
import json
import os
import shutil
import sys

import config
import image_store
import matcher
import plex_client
from tpdb_client import TPDbClient, strip_trailing_year

MATCHED_FIELDS = ["title", "year", "type", "uploader", "tpdb_url", "image_url", "local_file", "applied"]
UNMATCHED_FIELDS = ["title", "year", "type", "status", "reason", "tpdb_url"]
MANUAL_FIELDS = ["title", "year", "type", "source_url", "local_file", "note", "applied"]
COLLECTION_CHOICE_FIELDS = ["title", "year", "type", "available_collections", "chosen_collection"]


def process_item(tpdb, plex_lib_item):
    title = plex_lib_item["title"]
    year = plex_lib_item["year"]
    category = "movies" if plex_lib_item["type"] == "movie" else "shows"

    # Some titles bake a disambiguation year straight into the title text
    # itself (e.g. "The Continental (2023)") — that's noise for both the
    # search query and the matcher, since we already have the real year
    # separately from Plex.
    search_title = strip_trailing_year(title)

    try:
        candidates = tpdb.search_with_fallback(search_title, category)
    except Exception as exc:  # network hiccup, unexpected HTML, etc.
        return {"status": "error", "reason": f"search failed: {exc}"}

    # If nothing on the quick pass even shares the target year, this might
    # be a case like "RED" (2010) or "Mother!" (2017) — a generic title
    # buried behind dozens of unrelated same-named entries on TPDb, spread
    # across many pages with no useful sort order. Page through further
    # rather than giving up immediately; this only fires for titles that
    # actually need it, so it doesn't add cost to the common case.
    if year and not any(c.get("year") == year for c in candidates):
        try:
            deep_candidates, _pages_fetched = tpdb.search_deep_until_year(search_title, category, year)
        except Exception as exc:
            return {"status": "error", "reason": f"deep search failed: {exc}"}
        seen = {c["url"] for c in candidates}
        candidates += [c for c in deep_candidates if c["url"] not in seen]

    match, ambiguous, tied = matcher.best_match(search_title, year, candidates)

    if match is None:
        return {"status": "no_title_match", "reason": "no confident title match on TPDb"}

    if ambiguous:
        # Multiple candidates tied on title/year. Before giving up, check
        # which of them actually has a MAIN poster from a target uploader
        # (season-specific entries don't count for this) — TPDb sometimes
        # carries a duplicate or empty entry for the same title and year
        # (e.g. "Ad Astra" has a real 2019 entry plus a content-free 2018
        # duplicate). That's not a REAL ambiguity if only one of the tied
        # candidates has any relevant content; it only counts as one when
        # two or more tied candidates each have their own target-uploader
        # main poster.
        resolved = []
        for cand in tied:
            try:
                cand_found = tpdb.find_target_posters(cand["url"], config.TPDB_USERS)
            except Exception:
                cand_found = []
            if any(f["season_number"] is None for f in cand_found):
                resolved.append((cand, cand_found))

        if len(resolved) == 0:
            return {
                "status": "no_target_poster",
                "reason": "multiple title/year matches, none with a target-uploader poster",
                "tpdb_url": tied[0]["url"],
            }
        if len(resolved) > 1:
            return {
                "status": "ambiguous",
                "reason": "multiple distinct entries share this title/year, each with a target-uploader poster",
                "tpdb_url": match["url"],
            }
        match, found = resolved[0]
    else:
        try:
            found = tpdb.find_target_posters(match["url"], config.TPDB_USERS)
        except Exception as exc:
            return {"status": "error", "reason": f"poster listing failed: {exc}", "tpdb_url": match["url"]}

        if not any(f["season_number"] is None for f in found):
            return {
                "status": "no_target_poster",
                "reason": "matched on TPDb but no poster from target uploader(s)",
                "tpdb_url": match["url"],
            }

    # Only entries WITHOUT a season suffix are candidates for the main
    # poster. In practice the aggregate listing page never seems to carry
    # a season suffix at all (confirmed: Ted Lasso's page has 24 cards,
    # every one plainly "Ted Lasso (2020)") — but this filter stays as a
    # defensive safeguard in case some uploader's entry ever does, so a
    # "Season 3" card could never be mistakenly applied as the main poster.
    main_posters = [f for f in found if f["season_number"] is None]

    # Prefer whichever target uploader was listed first in config, in case
    # a title has posters from both.
    priority = {u.lower(): i for i, u in enumerate(config.TPDB_USERS)}
    main_posters.sort(key=lambda f: priority.get(f["uploader"].lower(), 999))
    chosen = main_posters[0]

    # Season-specific posters are NOT visible on the aggregate listing
    # page itself — confirmed via real data, it only ever shows one
    # representative card per uploader. They only exist within that
    # uploader's own "Set" page, so getting them costs one extra request
    # (only for shows, and only when the chosen poster actually has a
    # set_url at all).
    season_posters = []
    if plex_lib_item["type"] == "show" and chosen.get("set_url"):
        try:
            season_posters = tpdb.find_season_posters(chosen["set_url"], expected_show_url=match["url"])
        except Exception as exc:
            print(f"  (warning: couldn't check for season posters: {exc})")

    return {
        "status": "matched",
        "uploader": chosen["uploader"],
        # match["url"] is the title's poster-listing page — confirmed
        # working. We don't have a verified direct-permalink URL scheme
        # for an individual poster (data-poster-id doesn't necessarily
        # correspond to one), so we point here instead of guessing.
        "tpdb_url": match["url"],
        "image_url": chosen["image_url"],
        # Every target-uploader MAIN poster found for this title, priority-
        # order winner first — used to catalog cases where more than one
        # option existed (see multi_options.json / overrides.html).
        "options": main_posters,
        "season_posters": season_posters,
    }


def _item_key(title, year, item_type):
    """Identity used for checkpointing. year is normalized to match how
    it round-trips through CSV (None -> "")."""
    return (title, str(year) if year else "", item_type)


def _load_report(path):
    """Read a report CSV (if it exists) into a dict keyed by
    (title, year, type). Returns {} if the file doesn't exist yet."""
    rows = {}
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows[(row["title"], row["year"], row["type"])] = row
    return rows


def _rewrite_report(path, fieldnames, rows_by_key):
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows_by_key.values():
                writer.writerow(row)
    except PermissionError:
        sys.exit(
            f"\nCouldn't write to {path} — Windows says permission denied, which "
            "almost always means the file is open in another program (Excel, "
            "Notepad, etc. take an exclusive lock on CSVs).\n"
            "Close it there and re-run the exact same command — nothing has been "
            "lost, this will pick back up from here."
        )


def _load_multi_options(path):
    """Returns a dict keyed by (title, year, type) -> record, or {} if the
    file doesn't exist yet."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    return {(r["title"], str(r["year"]), r["type"]): r for r in records}


def _rewrite_multi_options(path, records_by_key):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(records_by_key.values()), f, indent=2)
    except PermissionError:
        sys.exit(
            f"\nCouldn't write to {path} — likely open in another program. "
            "Close it and re-run the exact same command."
        )


def _load_checked_keys(path):
    """Keys already visited by a --backfill-multi-options pass, so an
    interrupted backfill can resume instead of re-checking everything."""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {tuple(k) for k in json.load(f)}


def _save_checked_keys(path, checked_keys):
    with open(path, "w", encoding="utf-8") as f:
        json.dump([list(k) for k in checked_keys], f)


def backfill_multi_options(tpdb, matched_by_key, library_by_key, multi_options_path,
                            multi_options_by_key, checked_path, checked_keys):
    """
    Re-runs TPDb discovery (search + poster listing) for every already-
    matched item, purely to populate multi_options.json — does NOT touch
    Plex, matched.csv, or downloaded images, since those are already
    correct. For a library that was fully processed before this feature
    existed, without paying the cost of a full --restart (which would
    also redundantly re-apply everything to Plex and re-download every
    poster).

    Resumable via checked_path: to force rechecking everything from
    scratch, delete output/multi_options_checked.json — this function has
    no --restart of its own.
    """
    remaining = [k for k in matched_by_key if k not in checked_keys]
    already_done = len(matched_by_key) - len(remaining)
    if already_done:
        print(f"{already_done} item(s) already checked in a previous backfill run, skipping those.")
    print(f"Checking {len(remaining)} already-matched item(s) for additional poster options...")

    cataloged = 0
    for i, key in enumerate(remaining, 1):
        lib_item = library_by_key.get(key)
        title, year, item_type = key
        label = f"{title} ({year})" if year else title

        if lib_item is None:
            print(f"[{i}/{len(remaining)}] {label} ... no longer in Plex library, skipping")
            checked_keys.add(key)
            _save_checked_keys(checked_path, checked_keys)
            continue

        print(f"[{i}/{len(remaining)}] {label} ...", end=" ", flush=True)
        result = process_item(tpdb, lib_item)

        if result["status"] == "matched":
            options = result.get("options", [])
            if len(options) > 1:
                multi_options_by_key[key] = {
                    "title": title,
                    "year": year,
                    "type": item_type,
                    "tpdb_url": result["tpdb_url"],
                    # Reflect what's ACTUALLY applied in Plex right now
                    # (from matched.csv), not whatever this fresh
                    # re-search happens to prefer — TPDb's own data or
                    # ordering could have shifted slightly since the
                    # original run.
                    "chosen_image_url": matched_by_key[key]["image_url"],
                    "options": [
                        {"uploader": o["uploader"], "poster_id": o["poster_id"], "image_url": o["image_url"]}
                        for o in options
                    ],
                }
                _rewrite_multi_options(multi_options_path, multi_options_by_key)
                print(f"[{len(options)} options cataloged]")
                cataloged += 1
            else:
                print("(only one option, nothing to catalog)")
        else:
            print(f"(couldn't re-verify — {result['status']} — leaving existing match alone)")

        checked_keys.add(key)
        _save_checked_keys(checked_path, checked_keys)

    print(f"\nDone. {cataloged} title(s) newly cataloged with multiple options.")


def backfill_seasons(tpdb, matched_by_key, library_by_key, no_images, dry_run):
    """
    Read-mostly pass over already-matched SHOWS (movies are skipped —
    there's no such thing as a movie season), reusing each show's known
    TPDb page to check for season-specific posters from the SAME
    uploader already applied as the main poster, and applying/downloading
    any found. For a library that was fully processed before season
    support existed.

    Not fully checkpointed like the other backfills (no separate
    "already checked" file) — it's expected to be quick to re-run in
    full, since it's naturally limited to however many shows you have,
    not your whole library. Safe to just re-run if interrupted; anything
    already applied gets the same poster re-applied, which is harmless.
    """
    show_keys = [k for k, r in matched_by_key.items() if r["type"] == "show"]
    print(f"Checking {len(show_keys)} already-matched show(s) for season posters...")

    total_seasons = 0
    shows_with_seasons = 0
    for i, key in enumerate(show_keys, 1):
        row = matched_by_key[key]
        title, year = row["title"], row["year"]
        label = f"{title} ({year})" if year else title
        tpdb_url = row.get("tpdb_url", "")
        lib_item = library_by_key.get(key)

        if not tpdb_url or lib_item is None:
            continue

        print(f"[{i}/{len(show_keys)}] {label} ...", end=" ", flush=True)
        try:
            found = tpdb.find_target_posters(tpdb_url, [row["uploader"]])
        except Exception as exc:
            print(f"error: {exc}")
            continue

        # Season posters aren't on this aggregate page at all (see
        # find_season_posters' docstring) — find the main entry so we can
        # get its set_url, then fetch that Set page separately.
        main_entry = next((f for f in found if f["season_number"] is None), None)
        if not main_entry or not main_entry.get("set_url"):
            print("no Set found for this uploader")
            continue

        try:
            season_posters = tpdb.find_season_posters(main_entry["set_url"], expected_show_url=tpdb_url)
        except Exception as exc:
            print(f"error checking Set page: {exc}")
            continue

        if not season_posters:
            print("no season posters in this Set")
            continue

        applied = apply_season_posters(lib_item, season_posters, title, year, tpdb.session, no_images, dry_run)
        print(f"[+{len(applied)} season poster(s)]")
        if applied:
            shows_with_seasons += 1
        total_seasons += len(applied)

    print(f"\nDone. {total_seasons} season poster(s) applied across {shows_with_seasons} show(s).")


SEASON_RETRY_DAYS = 14   # how often to re-look for a season poster the uploader hasn't made yet
SEASON_GIVE_UP_DAYS = 90  # ...and how long after first looking to stop trying


def _season_state_key(key):
    return "|".join(str(part) for part in key)


def _load_season_state(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_season_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def _record_seasons_applied(state, key, season_numbers):
    entry = state.setdefault(_season_state_key(key), {"applied": [], "tried": {}})
    entry["applied"] = sorted(set(entry["applied"]) | set(season_numbers))
    for n in season_numbers:
        entry["tried"].pop(str(n), None)


def _local_season_numbers(title, year):
    """Season numbers that already have a saved image in the show's folder
    (from main runs, --backfill-seasons, or migrations)."""
    show_dir = image_store.poster_dir(config.OUTPUT_DIR, title, year, "show")
    if not os.path.isdir(show_dir):
        return set()
    names = os.listdir(show_dir)
    found = set()
    for fname in names:
        if fname.startswith("Specials."):
            found.add(0)
        elif fname.startswith("Season "):
            num = fname[len("Season "):].split(".", 1)[0]
            if num.isdigit():
                found.add(int(num))
    return found


def sync_new_seasons(tpdb, matched_by_key, library_by_key, state_path, state, no_images, dry_run):
    """
    Part of every normal run: for each already-applied show whose main
    poster came from one of the configured uploaders, compare the seasons
    Plex has now against the season posters already handled, and look up
    ONLY the missing ones on that uploader's Set page.

    A season counts as handled if it has a saved image in the show's
    folder, or was applied in an earlier run (seasons_state.json). A season
    the uploader hasn't made a poster for yet is retried every
    SEASON_RETRY_DAYS for SEASON_GIVE_UP_DAYS after we first looked, so a
    new season's poster gets picked up once it's uploaded. Specials
    (season 0) are only looked for once, since many uploaders skip it.
    Shows with nothing missing cost one quick Plex call and no TPDb requests.
    """
    allowed = {u.lower() for u in config.TPDB_USERS}
    today = datetime.date.today()
    checked = 0
    total_applied = 0

    for key, row in matched_by_key.items():
        if row["type"] != "show" or row["applied"] != "True":
            continue
        if row["uploader"].lower() not in allowed or not row.get("tpdb_url"):
            continue  # manual / custom poster, or an uploader no longer in the list
        lib_item = library_by_key.get(key)
        if lib_item is None:
            continue

        title, year = row["title"], row["year"]
        try:
            plex_seasons = {s.index for s in lib_item["item"].seasons()}
        except Exception:
            continue

        entry = state.get(_season_state_key(key), {"applied": [], "tried": {}})
        handled = set(entry["applied"]) | _local_season_numbers(title, year)
        wanted = set()
        for n in plex_seasons - handled:
            tried = entry["tried"].get(str(n))
            if tried:
                if n == 0:
                    continue
                first = datetime.date.fromisoformat(tried["first"])
                last = datetime.date.fromisoformat(tried["last"])
                if (today - first).days > SEASON_GIVE_UP_DAYS or (today - last).days < SEASON_RETRY_DAYS:
                    continue
            wanted.add(n)
        if not wanted:
            continue

        label = f"{title} ({year})" if year else title
        print(f"[seasons] {label}: looking for season(s) {', '.join(str(n) for n in sorted(wanted))} ...", end=" ", flush=True)
        checked += 1
        try:
            found = tpdb.find_target_posters(row["tpdb_url"], [row["uploader"]])
            main_entry = next((f for f in found if f["season_number"] is None), None)
            if not main_entry or not main_entry.get("set_url"):
                print("no Set found")
                continue
            season_posters = tpdb.find_season_posters(main_entry["set_url"], expected_show_url=row["tpdb_url"])
        except Exception as exc:
            print(f"error: {exc}")
            continue  # leave state alone so it retries next run

        todo = [sp for sp in season_posters if sp["season_number"] in wanted]
        applied = apply_season_posters(lib_item, todo, title, year, tpdb.session, no_images, dry_run) if todo else []
        applied_numbers = {sp["season_number"] for sp in applied}
        total_applied += len(applied_numbers)
        print(f"[+{len(applied_numbers)} season poster(s)]" if applied_numbers else "none available yet")

        if dry_run:
            continue
        _record_seasons_applied(state, key, applied_numbers)
        entry = state[_season_state_key(key)]
        for n in wanted - applied_numbers:
            prev = entry["tried"].get(str(n), {"first": today.isoformat()})
            entry["tried"][str(n)] = {"first": prev["first"], "last": today.isoformat()}
        _save_season_state(state_path, state)

    if checked:
        print(f"Season sync: checked {checked} show(s), applied {total_applied} new season poster(s).")


ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def add_user_to_env(new_username, env_path=ENV_PATH):
    """
    Adds new_username to the TPDB_USERS line in .env — appended at the
    end, i.e. lowest priority, so it never silently outranks an existing
    match unless TPDB_USERS is manually reordered afterward. Creates the
    line (and the file itself) if neither already exists, preserving
    every other line untouched. Returns the full updated user list.
    """
    current_users = list(config.TPDB_USERS)  # whatever's currently in effect
    lines = []
    found = False

    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("TPDB_USERS=") and not stripped.startswith("#"):
            found = True
            existing = [u.strip() for u in stripped.split("=", 1)[1].split(",") if u.strip()]
            if new_username.lower() not in [u.lower() for u in existing]:
                existing.append(new_username)
            new_lines.append(f"TPDB_USERS={','.join(existing)}\n")
            current_users = existing
        else:
            new_lines.append(line)

    if not found:
        if new_username.lower() not in [u.lower() for u in current_users]:
            current_users = current_users + [new_username]
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(f"TPDB_USERS={','.join(current_users)}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    return current_users


def process_new_user(tpdb, new_username, matched_by_key, unmatched_by_key, multi_options_by_key,
                      library_by_key, matched_path, unmatched_path, multi_options_path,
                      no_images, dry_run):
    """
    Fast path for adding an uploader after the library's already been
    processed: rather than re-running the full search pipeline (the most
    expensive and failure-prone part) for every title, reuse the TPDb
    listing URL already recorded and just check THAT ONE URL for the new
    user's poster.

    - Already-matched titles: a hit gets cataloged as an additional
      option (multi_options.json), never auto-applied over the existing
      pick — a newly added user is presumed lower priority unless
      TPDB_USERS is manually reordered.
    - Previously "no_target_poster" titles: a hit here is a genuine new
      match (nothing was covering this title before), so it's applied
      immediately, same as a fresh discovery.
    - "ambiguous" and "no_title_match" titles have no single reliable URL
      to reuse and are skipped — run --reverify afterward to reconsider
      those (it'll include the new user automatically, since by then
      they're in TPDB_USERS).

    Only checks MAIN posters, not season-specific ones — season posters
    from a newly added user aren't picked up here; a normal run or
    --backfill-seasons covers those.
    """
    checked = 0
    new_matches = 0
    new_options = 0
    skipped_no_url = 0

    matched_keys = list(matched_by_key.keys())
    print(f"Checking {len(matched_keys)} already-matched title(s) for a poster from {new_username}...")
    for i, key in enumerate(matched_keys, 1):
        row = matched_by_key[key]
        tpdb_url = row.get("tpdb_url", "")
        title, year = row["title"], row["year"]
        label = f"{title} ({year})" if year else title
        if not tpdb_url:
            skipped_no_url += 1
            continue

        print(f"[{i}/{len(matched_keys)}] {label} ...", end=" ", flush=True)
        try:
            found = tpdb.find_target_posters(tpdb_url, [new_username])
        except Exception as exc:
            print(f"error checking: {exc}")
            continue
        checked += 1
        main_found = [f for f in found if f["season_number"] is None]
        if not main_found:
            print("no poster from this user")
            continue

        new_opts = [{"uploader": f["uploader"], "poster_id": f["poster_id"], "image_url": f["image_url"]} for f in main_found]
        existing = multi_options_by_key.get(key)
        if existing:
            existing_ids = {o["poster_id"] for o in existing["options"]}
            added = [o for o in new_opts if o["poster_id"] not in existing_ids]
            if added:
                existing["options"].extend(added)
                multi_options_by_key[key] = existing
        else:
            multi_options_by_key[key] = {
                "title": title, "year": year, "type": row["type"],
                "tpdb_url": tpdb_url,
                "chosen_image_url": row["image_url"],
                "options": [{"uploader": row["uploader"], "poster_id": "", "image_url": row["image_url"]}] + new_opts,
            }
        _rewrite_multi_options(multi_options_path, multi_options_by_key)
        new_options += 1
        print(f"found a {new_username} option")

    no_target_items = [(k, r) for k, r in unmatched_by_key.items() if r.get("status") == "no_target_poster"]
    print(f"\nChecking {len(no_target_items)} previously-unmatched title(s) (no target poster) "
          f"for a poster from {new_username}...")
    for i, (key, row) in enumerate(no_target_items, 1):
        tpdb_url = row.get("tpdb_url", "")
        title, year, item_type = row["title"], row["year"], row["type"]
        label = f"{title} ({year})" if year else title
        if not tpdb_url:
            skipped_no_url += 1
            continue
        lib_item = library_by_key.get(key)
        if lib_item is None:
            continue

        print(f"[{i}/{len(no_target_items)}] {label} ...", end=" ", flush=True)
        try:
            found = tpdb.find_target_posters(tpdb_url, [new_username])
        except Exception as exc:
            print(f"error checking: {exc}")
            continue
        checked += 1
        main_found = [f for f in found if f["season_number"] is None]
        if not main_found:
            print("still nothing")
            continue

        chosen = main_found[0]
        uploader, poster_id, image_url = chosen["uploader"], chosen["poster_id"], chosen["image_url"]
        plex_client.apply_poster(lib_item["item"], image_url=image_url, dry_run=dry_run)
        saved_path = ""
        if not no_images:
            saved_path = _save_poster_locally(title, year, item_type, image_url, tpdb.session)
        matched_by_key[key] = {
            "title": title, "year": year, "type": item_type,
            "uploader": uploader, "tpdb_url": tpdb_url, "image_url": image_url,
            "local_file": saved_path, "applied": not dry_run,
        }
        del unmatched_by_key[key]
        _rewrite_report(matched_path, MATCHED_FIELDS, matched_by_key)
        _rewrite_report(unmatched_path, UNMATCHED_FIELDS, unmatched_by_key)
        new_matches += 1
        print(f"NEW MATCH ({uploader})")

    print(f"\nDone. Checked {checked} title(s) with a known TPDb page.")
    print(f"{new_matches} new match(es), {new_options} title(s) gained an additional option.")
    if skipped_no_url:
        print(f"({skipped_no_url} title(s) skipped — no TPDb page on record; "
              "run --reverify to reconsider those, it'll include the new user automatically.)")


def apply_season_posters(lib_item, season_posters, title, year, tpdb_session, no_images, dry_run):
    """
    Applies season-specific posters (the season_number-tagged entries
    find_target_posters returns) to the corresponding Plex season
    objects, and saves them locally alongside the main poster in that
    show's own subfolder (image_store.poster_dir).

    Assumes plexapi's Season.index gives the season number, with 0 for
    Specials — this hasn't been verified against a live server, so if
    seasons aren't landing correctly, that assumption is the first thing
    to check.

    Returns the list of season_posters actually applied (for reporting) —
    anything whose season_number has no matching Plex season is silently
    skipped, not an error.
    """
    if not season_posters:
        return []

    try:
        plex_seasons = {s.index: s for s in lib_item["item"].seasons()}
    except Exception as exc:
        print(f"  (warning: couldn't list seasons in Plex: {exc})")
        return []

    applied = []
    for sp in season_posters:
        plex_season = plex_seasons.get(sp["season_number"])
        if plex_season is None:
            continue  # Plex doesn't have this season (or numbers it differently)

        plex_client.apply_poster(plex_season, image_url=sp["image_url"], dry_run=dry_run)

        if not no_images:
            show_dir = image_store.poster_dir(config.OUTPUT_DIR, title, year, "show")
            os.makedirs(show_dir, exist_ok=True)
            ext = image_store.ext_from_url(sp["image_url"])
            dest_path = os.path.join(show_dir, image_store.season_filename(sp["season_number"], ext))
            try:
                image_store.download_poster(tpdb_session, sp["image_url"], dest_path)
            except Exception as exc:
                print(f"  (warning: couldn't save season poster locally: {exc})")

        applied.append(sp)

    return applied


def _save_poster_locally(title, year, item_type, image_url, session, collection_name=None):
    """Download a matched poster to its standard location (see
    image_store.poster_dir), return the saved path (or "" on failure —
    caller treats that as no local copy)."""
    ext = image_store.ext_from_url(image_url)
    filename = image_store.safe_filename(title, year, ext)
    dest_dir = image_store.poster_dir(config.OUTPUT_DIR, title, year, item_type, collection_name)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    try:
        image_store.download_poster(session, image_url, dest_path)
        return dest_path
    except Exception as exc:
        print(f"  (warning: couldn't save local copy: {exc})")
        return ""


def _copy_poster_locally(title, year, item_type, src_path, collection_name=None):
    """Copy a user-supplied local image (manual.csv's local_file) into the
    standard location (see image_store.poster_dir), same naming scheme
    as everything else. Returns the new path, or "" on failure."""
    ext = os.path.splitext(src_path)[1] or ".jpg"
    filename = image_store.safe_filename(title, year, ext)
    dest_dir = image_store.poster_dir(config.OUTPUT_DIR, title, year, item_type, collection_name)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    try:
        shutil.copyfile(src_path, dest_path)
        return dest_path
    except Exception as exc:
        print(f"  (warning: couldn't copy local file: {exc})")
        return ""


def resolve_movie_collection_folder(collections, key, collection_choices_by_key):
    """
    Decides which (if any) Plex Collection subfolder a movie's poster
    should be filed under:
      - no collections            -> None (flat movies/ folder)
      - exactly one collection    -> that collection, automatically
      - two or more collections   -> whatever's recorded in
                                      collection_choices.csv's
                                      chosen_collection column for this
                                      movie, or None (stays flat) if
                                      that's still blank/unresolved
    """
    if not collections:
        return None
    if len(collections) == 1:
        return collections[0]
    row = collection_choices_by_key.get(key)
    chosen = (row or {}).get("chosen_collection", "").strip()
    if chosen and chosen in collections:
        return chosen
    return None


def sync_collection_choice_row(collection_choices_by_key, key, title, year, item_type, collections):
    """
    Keeps collection_choices.csv in sync for a movie with 2+ collections:
    adds a fresh row (chosen_collection blank, needing a decision) if
    this is the first time we've seen it, or refreshes available_collections
    on an existing row without ever touching a chosen_collection value the
    user already filled in. Returns True if the in-memory dict changed
    (caller decides whether/when to rewrite the file).
    """
    existing = collection_choices_by_key.get(key)
    available = ";".join(collections)
    if existing:
        if existing.get("available_collections") != available:
            existing["available_collections"] = available
            collection_choices_by_key[key] = existing
            return True
        return False
    collection_choices_by_key[key] = {
        "title": title,
        "year": year,
        "type": item_type,
        "available_collections": available,
        "chosen_collection": "",
    }
    return True


def determine_movie_collection_folder(key, title, year, collections, collection_choices_by_key, collection_choices_path):
    """
    Convenience wrapper combining sync_collection_choice_row +
    resolve_movie_collection_folder: syncs collection_choices.csv (only
    rewriting the file if something actually changed) and returns the
    folder name to use (or None for flat), in one call. Only meaningful
    for movies — pass collections=[] for shows to always get None back.
    """
    if len(collections) > 1:
        if sync_collection_choice_row(collection_choices_by_key, key, title, year, "movie", collections):
            _rewrite_report(collection_choices_path, COLLECTION_CHOICE_FIELDS, collection_choices_by_key)
    return resolve_movie_collection_folder(collections, key, collection_choices_by_key)


def process_manual_entries(tpdb, library_by_key, matched_by_key, unmatched_by_key,
                            matched_path, unmatched_path, manual_path, no_images, dry_run,
                            collection_choices_by_key=None, collection_choices_path=None):
    """
    Apply anything new in output/manual.csv. A manual entry always
    overrides whatever the automated pipeline found for that title.
    Returns the count actually applied this run.
    """
    manual_by_key = _load_report(manual_path)
    if not manual_by_key:
        return 0

    applied_count = 0
    for key, row in manual_by_key.items():
        if row.get("applied") == "True":
            continue

        lib_item = library_by_key.get(key)
        if lib_item is None:
            title, year, item_type = key
            print(f"manual.csv: no Plex item matches {title} ({year}) [{item_type}], skipping that row")
            continue

        title, year, item_type = lib_item["title"], lib_item["year"], lib_item["type"]
        label = f"{title} ({year})" if year else title

        local_file_in = (row.get("local_file") or "").strip()
        source_url = (row.get("source_url") or "").strip()

        image_url = None
        src_local_path = None

        if local_file_in:
            if not os.path.exists(local_file_in):
                print(f"{label} ... manual.csv local_file not found: {local_file_in}, skipping")
                continue
            src_local_path = local_file_in
        elif source_url:
            if "theposterdb.com/poster/" in source_url:
                try:
                    image_url = tpdb.get_poster_image_url(source_url)
                except Exception as exc:
                    print(f"{label} ... couldn't load manual source_url: {exc}, skipping")
                    continue
                if not image_url:
                    print(f"{label} ... no image found on that TPDb page, skipping")
                    continue
            else:
                image_url = source_url  # assume it's already a direct image URL
        else:
            print(f"{label} ... manual.csv row has neither source_url nor local_file, skipping")
            continue

        note = (row.get("note") or "").strip()
        print(f"{label} ... applying manual entry" + (f" ({note})" if note else ""))

        plex_client.apply_poster(lib_item["item"], image_url=image_url, filepath=src_local_path, dry_run=dry_run)

        collection_name = None
        if item_type == "movie" and collection_choices_by_key is not None:
            movie_collections = plex_client.get_movie_collections(lib_item["item"])
            collection_name = determine_movie_collection_folder(
                key, title, year, movie_collections,
                collection_choices_by_key, collection_choices_path,
            )

        saved_path = ""
        if not no_images:
            if src_local_path:
                saved_path = _copy_poster_locally(title, year, item_type, src_local_path, collection_name)
            elif image_url:
                saved_path = _save_poster_locally(title, year, item_type, image_url, tpdb.session, collection_name)

        matched_by_key[key] = {
            "title": title,
            "year": year,
            "type": item_type,
            "uploader": note or "manual",
            "tpdb_url": source_url,
            "image_url": image_url or "",
            "local_file": saved_path or src_local_path or "",
            "applied": not dry_run,
        }
        unmatched_by_key.pop(key, None)
        _rewrite_report(matched_path, MATCHED_FIELDS, matched_by_key)
        _rewrite_report(unmatched_path, UNMATCHED_FIELDS, unmatched_by_key)

        row["applied"] = "True" if not dry_run else "False"
        manual_by_key[key] = row
        _rewrite_report(manual_path, MANUAL_FIELDS, manual_by_key)

        applied_count += 1

    return applied_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Don't actually modify Plex")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N items (testing)")
    parser.add_argument(
        "--restart", action="store_true",
        help="Ignore any previous progress in output/ and reprocess everything from scratch",
    )
    parser.add_argument(
        "--no-images", action="store_true",
        help="Don't save local copies of matched posters (they're saved by default, for easy visual verification)",
    )
    parser.add_argument(
        "--use-cache", action="store_true",
        help="For items already matched but not yet applied (e.g. from an earlier --dry-run), "
             "apply straight from the cached image instead of re-querying TPDb",
    )
    parser.add_argument(
        "--reverify", action="store_true",
        help="Also re-check items previously recorded as unmatched (in case of a code fix or "
             "new TPDb upload). Items already matched AND applied are still skipped.",
    )
    parser.add_argument(
        "--backfill-multi-options", action="store_true",
        help="Read-only pass over already-matched items purely to populate multi_options.json "
             "(for a library that was fully processed before that feature existed). "
             "Never touches Plex, matched.csv, or downloaded images.",
    )
    parser.add_argument(
        "--new-user", metavar="USERNAME",
        help="Add USERNAME to TPDB_USERS (saved to .env) and quickly check it against "
             "everything already processed, reusing each title's known TPDb page instead "
             "of a full re-search.",
    )
    parser.add_argument(
        "--backfill-seasons", action="store_true",
        help="Read-mostly pass over already-matched shows to apply season-specific posters "
             "from the same uploader as the main poster (for a library processed before season "
             "support existed). Movies are skipped. Reuses each show's known TPDb page.",
    )
    parser.add_argument(
        "--skip-season-sync", action="store_true",
        help="Skip the end-of-run check for season posters that are missing for seasons "
             "Plex has (new seasons you've added since the last run).",
    )
    args = parser.parse_args()

    config.validate()
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    matched_path = os.path.join(config.OUTPUT_DIR, "matched.csv")
    unmatched_path = os.path.join(config.OUTPUT_DIR, "unmatched.csv")
    manual_path = os.path.join(config.OUTPUT_DIR, "manual.csv")
    multi_options_path = os.path.join(config.OUTPUT_DIR, "multi_options.json")
    checked_path = os.path.join(config.OUTPUT_DIR, "multi_options_checked.json")
    collection_choices_path = os.path.join(config.OUTPUT_DIR, "collection_choices.csv")
    season_state_path = os.path.join(config.OUTPUT_DIR, "seasons_state.json")

    if args.restart:
        for p in (matched_path, unmatched_path):
            if os.path.exists(p):
                os.remove(p)
        # manual.csv and collection_choices.csv are never cleared by
        # --restart — they're your own curated data.

    if not os.path.exists(manual_path):
        _rewrite_report(manual_path, MANUAL_FIELDS, {})
    if not os.path.exists(collection_choices_path):
        _rewrite_report(collection_choices_path, COLLECTION_CHOICE_FIELDS, {})

    matched_by_key = _load_report(matched_path)
    unmatched_by_key = _load_report(unmatched_path)
    multi_options_by_key = _load_multi_options(multi_options_path)
    collection_choices_by_key = _load_report(collection_choices_path)

    print("Connecting to Plex...")
    plex = plex_client.connect()
    library_items = plex_client.get_library_items(plex)
    if args.limit:
        library_items = library_items[: args.limit]
    print(f"Found {len(library_items)} movies/shows in your Plex library.")

    if args.new_user:
        updated_users = add_user_to_env(args.new_user)
        config.TPDB_USERS = updated_users
        print(f"Added {args.new_user!r} to TPDB_USERS (saved to .env). Full list now: {', '.join(updated_users)}")
        library_by_key = {
            _item_key(item["title"], item["year"], item["type"]): item for item in library_items
        }
        tpdb = TPDbClient()
        if args.dry_run:
            print("(--dry-run: nothing will actually be applied to Plex)")
        process_new_user(
            tpdb, args.new_user, matched_by_key, unmatched_by_key, multi_options_by_key,
            library_by_key, matched_path, unmatched_path, multi_options_path,
            args.no_images, args.dry_run,
        )
        return

    if args.backfill_multi_options:
        library_by_key = {
            _item_key(item["title"], item["year"], item["type"]): item for item in library_items
        }
        checked_keys = _load_checked_keys(checked_path)
        tpdb = TPDbClient()
        backfill_multi_options(
            tpdb, matched_by_key, library_by_key, multi_options_path,
            multi_options_by_key, checked_path, checked_keys,
        )
        return

    if args.backfill_seasons:
        library_by_key = {
            _item_key(item["title"], item["year"], item["type"]): item for item in library_items
        }
        tpdb = TPDbClient()
        if args.dry_run:
            print("(--dry-run: nothing will actually be applied to Plex)")
        backfill_seasons(tpdb, matched_by_key, library_by_key, args.no_images, args.dry_run)
        return

    print(f"Looking for posters from: {', '.join(config.TPDB_USERS)}")
    if args.dry_run:
        print("(--dry-run: nothing will actually be applied to Plex)")
    if args.use_cache:
        print("(--use-cache: previously-matched-but-unapplied items will be applied from cache, skipping TPDb)")
    if args.reverify:
        print("(--reverify: previously-unmatched items will be rechecked against TPDb)")

    tpdb = TPDbClient()

    library_by_key = {
        _item_key(item["title"], item["year"], item["type"]): item for item in library_items
    }
    manual_applied = process_manual_entries(
        tpdb, library_by_key, matched_by_key, unmatched_by_key,
        matched_path, unmatched_path, manual_path, args.no_images, args.dry_run,
        collection_choices_by_key, collection_choices_path,
    )
    if manual_applied:
        print(f"{manual_applied} manual.csv entr{'y' if manual_applied == 1 else 'ies'} applied this run.")

    already_applied = 0
    reapplied_count = 0
    matched_count = 0
    unmatched_count = 0
    to_process = []  # library items needing the full search/match pipeline

    for lib_item in library_items:
        key = _item_key(lib_item["title"], lib_item["year"], lib_item["type"])

        if key in unmatched_by_key and not args.reverify:
            continue  # recorded as unmatched previously; --reverify or delete unmatched.csv to force a recheck

        row = matched_by_key.get(key)
        if row is not None:
            if row["applied"] == "True":
                already_applied += 1
                continue
            if args.dry_run:
                continue  # already known as a dry-run match, nothing new to do
            if args.use_cache:
                title, year = lib_item["title"], lib_item["year"]
                label = f"{title} ({year})" if year else title
                local_file = row.get("local_file", "")
                use_local = local_file if local_file and os.path.exists(local_file) else None
                print(f"{label} ... applying from cache ({row['uploader']})")
                plex_client.apply_poster(
                    lib_item["item"], image_url=row["image_url"], filepath=use_local
                )
                row["applied"] = "True"
                matched_by_key[key] = row
                _rewrite_report(matched_path, MATCHED_FIELDS, matched_by_key)
                reapplied_count += 1
                continue
            # Real run, no --use-cache: fall through and re-verify via TPDb.

        to_process.append(lib_item)

    skipped = already_applied
    if skipped:
        print(f"{skipped} item(s) already applied in a previous run, skipping those.")
    if reapplied_count:
        print(f"{reapplied_count} item(s) applied from cache.")
    print(f"{len(to_process)} item(s) to search/match this run.")
    season_state = _load_season_state(season_state_path)

    try:
        for i, lib_item in enumerate(to_process, 1):
            title, year = lib_item["title"], lib_item["year"]
            label = f"{title} ({year})" if year else title
            print(f"[{i}/{len(to_process)}] {label} ...", end=" ", flush=True)

            result = process_item(tpdb, lib_item)
            key = _item_key(title, year, lib_item["type"])

            if result["status"] == "matched":
                plex_client.apply_poster(lib_item["item"], image_url=result["image_url"], dry_run=args.dry_run)

                collection_name = None
                if lib_item["type"] == "movie":
                    movie_collections = plex_client.get_movie_collections(lib_item["item"])
                    collection_name = determine_movie_collection_folder(
                        key, title, year, movie_collections,
                        collection_choices_by_key, collection_choices_path,
                    )

                saved_path = ""
                if not args.no_images:
                    saved_path = _save_poster_locally(
                        title, year, lib_item["type"], result["image_url"], tpdb.session, collection_name
                    )

                season_summary = ""
                if lib_item["type"] == "show" and result.get("season_posters"):
                    applied_seasons = apply_season_posters(
                        lib_item, result["season_posters"], title, year, tpdb.session,
                        args.no_images, args.dry_run,
                    )
                    if applied_seasons:
                        season_summary = f"  [+{len(applied_seasons)} season poster(s)]"
                        if not args.dry_run:
                            _record_seasons_applied(season_state, key, [sp["season_number"] for sp in applied_seasons])
                            _save_season_state(season_state_path, season_state)

                print(f"MATCHED ({result['uploader']}){season_summary}", end="")
                options = result.get("options", [])
                if len(options) > 1:
                    print(f"  [{len(options)} options]")
                    multi_options_by_key[key] = {
                        "title": title,
                        "year": year,
                        "type": lib_item["type"],
                        "tpdb_url": result["tpdb_url"],
                        "chosen_image_url": result["image_url"],
                        "options": [
                            {"uploader": o["uploader"], "poster_id": o["poster_id"], "image_url": o["image_url"]}
                            for o in options
                        ],
                    }
                    _rewrite_multi_options(multi_options_path, multi_options_by_key)
                else:
                    print()
                    multi_options_by_key.pop(key, None)

                matched_by_key[key] = {
                    "title": title,
                    "year": year,
                    "type": lib_item["type"],
                    "uploader": result["uploader"],
                    "tpdb_url": result["tpdb_url"],
                    "image_url": result["image_url"],
                    "local_file": saved_path,
                    "applied": not args.dry_run,
                }
                unmatched_by_key.pop(key, None)  # in case it flipped from a previous unmatched result
                _rewrite_report(matched_path, MATCHED_FIELDS, matched_by_key)
                matched_count += 1
            else:
                print(result["status"])
                unmatched_by_key[key] = {
                    "title": title,
                    "year": year,
                    "type": lib_item["type"],
                    "status": result["status"],
                    "reason": result.get("reason", ""),
                    "tpdb_url": result.get("tpdb_url", ""),
                }
                matched_by_key.pop(key, None)  # in case it flipped from a previous matched result
                if multi_options_by_key.pop(key, None) is not None:
                    _rewrite_multi_options(multi_options_path, multi_options_by_key)
                _rewrite_report(unmatched_path, UNMATCHED_FIELDS, unmatched_by_key)
                unmatched_count += 1
    finally:
        # Always leave both files in a consistent state, even on interruption.
        _rewrite_report(matched_path, MATCHED_FIELDS, matched_by_key)
        _rewrite_report(unmatched_path, UNMATCHED_FIELDS, unmatched_by_key)

    if not args.skip_season_sync:
        print()
        print("Checking already-applied shows for new seasons...")
        sync_new_seasons(
            tpdb, matched_by_key, library_by_key, season_state_path, season_state,
            args.no_images, args.dry_run,
        )

    print()
    print(f"Done this run: {matched_count} matched, {unmatched_count} not matched, {reapplied_count} applied from cache.")
    if skipped:
        print(f"(plus {skipped} already applied from a previous run)")
    print(f"Reports: {matched_path} and {unmatched_path}")


def _refresh_overrides_page():
    """Keep overrides.html in sync with multi_options.json after every run."""
    try:
        import generate_overrides_page
        count = generate_overrides_page.regenerate()
        if count is not None:
            print(f"overrides.html refreshed ({count} title(s) with multiple options): "
                  f"{generate_overrides_page.HTML_PATH}")
    except Exception as exc:  # never let this mask the real run's result
        print(f"(Couldn't refresh overrides.html: {exc})")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _refresh_overrides_page()
        sys.exit("\nInterrupted — progress so far is saved. Re-run the same command to resume.")
    _refresh_overrides_page()
