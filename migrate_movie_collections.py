"""
One-time (or repeatable) pass: organizes your EXISTING movie posters by
Plex Collection membership.

    - A movie with no Plex collections stays in the flat movies/ folder.
    - A movie with exactly one Plex collection is moved automatically
      into output/posters/movies/<Collection Name>/.
    - A movie with two or more Plex collections needs a decision: a row
      gets added to output/collection_choices.csv (if not already
      there) with the available collections listed, and its
      chosen_collection column left blank. Fill that in yourself, then
      re-run this script to actually move it.

Safe to run repeatedly — already-organized files are skipped, and your
existing chosen_collection picks in collection_choices.csv are never
touched or overwritten.

Usage:
    python migrate_movie_collections.py [--dry-run]
"""
import argparse
import os
import shutil

import config
import image_store
import main as m
import plex_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show what would move, without moving anything")
    args = parser.parse_args()

    matched_path = os.path.join(config.OUTPUT_DIR, "matched.csv")
    collection_choices_path = os.path.join(config.OUTPUT_DIR, "collection_choices.csv")
    if not os.path.exists(matched_path):
        raise SystemExit(f"{matched_path} not found — nothing to migrate.")

    matched_by_key = m._load_report(matched_path)
    if not os.path.exists(collection_choices_path):
        m._rewrite_report(collection_choices_path, m.COLLECTION_CHOICE_FIELDS, {})
    collection_choices_by_key = m._load_report(collection_choices_path)

    print("Connecting to Plex...")
    plex = plex_client.connect()
    library_items = plex_client.get_library_items(plex)
    library_by_key = {
        m._item_key(item["title"], item["year"], item["type"]): item for item in library_items
    }

    moved = 0
    already_placed = 0
    pending_decision = 0
    skipped_no_file = 0
    matched_changed = False

    movie_rows = [(k, r) for k, r in matched_by_key.items() if r["type"] == "movie"]
    total = len(movie_rows)

    for i, (key, row) in enumerate(movie_rows, 1):
        old_path = row.get("local_file", "")
        title, year = row["title"], row["year"]
        label = f"{title} ({year})" if year else title
        print(f"[{i}/{total}] {label} ...", end=" ", flush=True)

        if not old_path or not os.path.exists(old_path):
            skipped_no_file += 1
            print("no local file on record")
            continue

        lib_item = library_by_key.get(key)
        collections = plex_client.get_movie_collections(lib_item["item"]) if lib_item else []

        collection_name = m.determine_movie_collection_folder(
            key, title, year, collections, collection_choices_by_key, collection_choices_path,
        )

        if len(collections) > 1 and not collection_name:
            pending_decision += 1
            print(f"needs a decision ({len(collections)} collections)")
            continue  # needs a decision in collection_choices.csv first

        if not collection_name:
            already_placed += 1
            print("no collection" if not collections else "already placed")
            continue  # no collection (or already flat, correctly)

        ext = os.path.splitext(old_path)[1] or ".jpg"
        new_dir = image_store.poster_dir(config.OUTPUT_DIR, title, year, "movie", collection_name)
        new_path = os.path.join(new_dir, image_store.safe_filename(title, year, ext))

        if os.path.abspath(old_path) == os.path.abspath(new_path):
            already_placed += 1
            print("already placed")
            continue

        print(f"-> {collection_name}/")
        if not args.dry_run:
            os.makedirs(new_dir, exist_ok=True)
            shutil.move(old_path, new_path)
            row["local_file"] = new_path
            matched_by_key[key] = row
            matched_changed = True
        moved += 1

    if matched_changed:
        m._rewrite_report(matched_path, m.MATCHED_FIELDS, matched_by_key)

    print()
    verb = "Would move" if args.dry_run else "Moved"
    print(f"{verb} {moved} movie poster(s) into collection folders.")
    print(f"{already_placed} already correctly placed, {skipped_no_file} had no local file on record.")
    if pending_decision:
        print(f"{pending_decision} movie(s) belong to multiple collections and need a decision — "
              f"fill in chosen_collection in {collection_choices_path}, then re-run this script.")
    if args.dry_run:
        print("Re-run without --dry-run to actually move files.")


if __name__ == "__main__":
    main()
