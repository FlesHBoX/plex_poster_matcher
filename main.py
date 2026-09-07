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
    args = parser.parse_args()

    config.validate()
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    matched_path = os.path.join(config.OUTPUT_DIR, "matched.csv")
    unmatched_path = os.path.join(config.OUTPUT_DIR, "unmatched.csv")
    manual_path = os.path.join(config.OUTPUT_DIR, "manual.csv")

    if args.restart:
        for p in (matched_path, unmatched_path):
            if os.path.exists(p):
                os.remove(p)
        # manual.csv is never cleared by --restart — it's your own curated data.

    if not os.path.exists(manual_path):
        _rewrite_report(manual_path, MANUAL_FIELDS, {})

    matched_by_key = _load_report(matched_path)
    unmatched_by_key = _load_report(unmatched_path)

    print("Connecting to Plex...")
    plex = plex_client.connect()
    library_items = plex_client.get_library_items(plex)
    if args.limit:
        library_items = library_items[: args.limit]
    print(f"Found {len(library_items)} movies/shows in your Plex library.")
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

                print(f"MATCHED ({result['uploader']})")
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
