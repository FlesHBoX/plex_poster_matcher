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
        # which of them actually has a poster from a target uploader —
        # TPDb sometimes carries a duplicate or empty entry for the same
        # title and year (e.g. "Ad Astra" has a real 2019 entry plus a
        # content-free 2018 duplicate). That's not a REAL ambiguity if
        # only one of the tied candidates has any relevant content; it
        # only counts as one when two or more tied candidates each have
        # their own target-uploader poster.
        resolved = []
        for cand in tied:
            try:
                cand_found = tpdb.find_target_posters(cand["url"], config.TPDB_USERS)
            except Exception:
                cand_found = []
            if cand_found:
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

        if not found:
            return {
                "status": "no_target_poster",
                "reason": "matched on TPDb but no poster from target uploader(s)",
                "tpdb_url": match["url"],
            }

    # Prefer whichever target uploader was listed first in config, in case
    # a title has posters from both.
    priority = {u.lower(): i for i, u in enumerate(config.TPDB_USERS)}
    found.sort(key=lambda triple: priority.get(triple[0].lower(), 999))
    uploader, poster_id, image_url = found[0]

    return {
        "status": "matched",
        "uploader": uploader,
        # match["url"] is the title's poster-listing page — confirmed
        # working. We don't have a verified direct-permalink URL scheme
        # for an individual poster (data-poster-id doesn't necessarily
        # correspond to one), so we point here instead of guessing.
        "tpdb_url": match["url"],
        "image_url": image_url,
        # Every target-uploader poster found for this title, priority-order
        # winner first — used to catalog cases where more than one option
        # existed (see multi_options.json / overrides.html).
        "options": found,
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
                        {"uploader": u, "poster_id": pid, "image_url": img}
                        for u, pid, img in options
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
        if not found:
            print("no poster from this user")
            continue

        new_opts = [{"uploader": u, "poster_id": pid, "image_url": img} for u, pid, img in found]
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
        print(f"found a {new_username} option -- see overrides.html")

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
        if not found:
            print("still nothing")
            continue

        uploader, poster_id, image_url = found[0]
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


def _save_poster_locally(title, year, item_type, image_url, session):
    """Download a matched poster to output/posters/<movies|shows>/, return
    the saved path (or "" on failure — caller treats that as no local copy)."""
    subdir = "movies" if item_type == "movie" else "shows"
    ext = image_store.ext_from_url(image_url)
    filename = image_store.safe_filename(title, year, ext)
    dest_dir = os.path.join(config.OUTPUT_DIR, "posters", subdir)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    try:
        image_store.download_poster(session, image_url, dest_path)
        return dest_path
    except Exception as exc:
        print(f"  (warning: couldn't save local copy: {exc})")
        return ""


def _copy_poster_locally(title, year, item_type, src_path):
    """Copy a user-supplied local image (manual.csv's local_file) into the
    standard output/posters/<movies|shows>/ location, same naming scheme
    as everything else. Returns the new path, or "" on failure."""
    subdir = "movies" if item_type == "movie" else "shows"
    ext = os.path.splitext(src_path)[1] or ".jpg"
    filename = image_store.safe_filename(title, year, ext)
    dest_dir = os.path.join(config.OUTPUT_DIR, "posters", subdir)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, filename)
    try:
        shutil.copyfile(src_path, dest_path)
        return dest_path
    except Exception as exc:
        print(f"  (warning: couldn't copy local file: {exc})")
        return ""


def process_manual_entries(tpdb, library_by_key, matched_by_key, unmatched_by_key,
                            matched_path, unmatched_path, manual_path, no_images, dry_run):
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

        saved_path = ""
        if not no_images:
            if src_local_path:
                saved_path = _copy_poster_locally(title, year, item_type, src_local_path)
            elif image_url:
                saved_path = _save_poster_locally(title, year, item_type, image_url, tpdb.session)

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
    args = parser.parse_args()

    config.validate()
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    matched_path = os.path.join(config.OUTPUT_DIR, "matched.csv")
    unmatched_path = os.path.join(config.OUTPUT_DIR, "unmatched.csv")
    manual_path = os.path.join(config.OUTPUT_DIR, "manual.csv")
    multi_options_path = os.path.join(config.OUTPUT_DIR, "multi_options.json")
    checked_path = os.path.join(config.OUTPUT_DIR, "multi_options_checked.json")

    if args.restart:
        for p in (matched_path, unmatched_path):
            if os.path.exists(p):
                os.remove(p)
        # manual.csv is never cleared by --restart — it's your own curated data.

    if not os.path.exists(manual_path):
        _rewrite_report(manual_path, MANUAL_FIELDS, {})

    matched_by_key = _load_report(matched_path)
    unmatched_by_key = _load_report(unmatched_path)
    multi_options_by_key = _load_multi_options(multi_options_path)

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

    try:
        for i, lib_item in enumerate(to_process, 1):
            title, year = lib_item["title"], lib_item["year"]
            label = f"{title} ({year})" if year else title
            print(f"[{i}/{len(to_process)}] {label} ...", end=" ", flush=True)

            result = process_item(tpdb, lib_item)
            key = _item_key(title, year, lib_item["type"])

            if result["status"] == "matched":
                plex_client.apply_poster(lib_item["item"], image_url=result["image_url"], dry_run=args.dry_run)

                saved_path = ""
                if not args.no_images:
                    saved_path = _save_poster_locally(title, year, lib_item["type"], result["image_url"], tpdb.session)

                print(f"MATCHED ({result['uploader']})", end="")
                options = result.get("options", [])
                if len(options) > 1:
                    print(f"  [{len(options)} options — see overrides.html]")
                    multi_options_by_key[key] = {
                        "title": title,
                        "year": year,
                        "type": lib_item["type"],
                        "tpdb_url": result["tpdb_url"],
                        "chosen_image_url": result["image_url"],
                        "options": [
                            {"uploader": u, "poster_id": pid, "image_url": img}
                            for u, pid, img in options
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

    print()
    print(f"Done this run: {matched_count} matched, {unmatched_count} not matched, {reapplied_count} applied from cache.")
    if skipped:
        print(f"(plus {skipped} already applied from a previous run)")
    print(f"Reports: {matched_path} and {unmatched_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted — progress so far is saved. Re-run the same command to resume.")
