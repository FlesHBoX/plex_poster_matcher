"""
One-time migration: moves your EXISTING show posters from the old flat
layout (output/posters/shows/Show (Year).jpg) into the new per-show
subfolder layout (output/posters/shows/Show (Year)/Show (Year).jpg),
and updates matched.csv's local_file column to match.

Only needed once, right after updating to the version of main.py that
introduced per-show subfolders — anything downloaded from that point
onward already lands in the right place. Safe to run more than once;
already-migrated files are simply skipped.

Movies are untouched — they stay in the flat output/posters/movies/
layout.

Usage:
    python migrate_show_folders.py [--dry-run]
"""
import argparse
import os
import shutil

import config
import image_store
import main as m


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show what would move, without moving anything")
    args = parser.parse_args()

    matched_path = os.path.join(config.OUTPUT_DIR, "matched.csv")
    if not os.path.exists(matched_path):
        raise SystemExit(f"{matched_path} not found — nothing to migrate.")

    matched_by_key = m._load_report(matched_path)

    moved = 0
    skipped_no_file = 0
    already_migrated = 0
    changed_rows = False

    for key, row in matched_by_key.items():
        if row["type"] != "show":
            continue
        old_path = row.get("local_file", "")
        if not old_path or not os.path.exists(old_path):
            skipped_no_file += 1
            continue

        title, year = row["title"], row["year"]
        new_dir = image_store.poster_dir(config.OUTPUT_DIR, title, year, "show")
        ext = os.path.splitext(old_path)[1] or ".jpg"
        new_path = os.path.join(new_dir, image_store.safe_filename(title, year, ext))

        if os.path.abspath(old_path) == os.path.abspath(new_path):
            already_migrated += 1
            continue

        label = f"{title} ({year})" if year else title
        print(f"{label}: {old_path}  ->  {new_path}")

        if not args.dry_run:
            os.makedirs(new_dir, exist_ok=True)
            shutil.move(old_path, new_path)
            row["local_file"] = new_path
            matched_by_key[key] = row
            changed_rows = True

        moved += 1

    if changed_rows:
        m._rewrite_report(matched_path, m.MATCHED_FIELDS, matched_by_key)

    print()
    verb = "Would move" if args.dry_run else "Moved"
    print(f"{verb} {moved} show poster(s). {already_migrated} already in the new layout, "
          f"{skipped_no_file} had no local file on record (skipped).")
    if args.dry_run:
        print("Re-run without --dry-run to actually move files.")


if __name__ == "__main__":
    main()
