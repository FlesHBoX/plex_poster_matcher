"""
Merges output/overrides.csv (downloaded from overrides.html) into
output/manual.csv, so your overrides get picked up on the next main.py run.

Usage:
    python merge_overrides.py                      # uses output/overrides.csv
    python merge_overrides.py path\\to\\overrides.csv  # or a specific file

Rows are matched by (title, year, type). If a row already exists in
manual.csv for that title, it's replaced by the override (with `applied`
reset to blank so main.py actually applies it fresh); otherwise it's
added as a new row.
"""
import os
import sys

import config
import main as m

DEFAULT_PATH = os.path.join(config.OUTPUT_DIR, "overrides.csv")


def main():
    overrides_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    manual_path = os.path.join(config.OUTPUT_DIR, "manual.csv")

    if not os.path.exists(overrides_path):
        sys.exit(
            f"{overrides_path} not found.\n"
            "Export it from overrides.html first (the 'Export overrides.csv' button)."
        )

    overrides_by_key = m._load_report(overrides_path)
    if not overrides_by_key:
        sys.exit(f"{overrides_path} has no rows — nothing to merge.")

    manual_by_key = m._load_report(manual_path) if os.path.exists(manual_path) else {}

    added, updated = 0, 0
    for key, row in overrides_by_key.items():
        row["applied"] = ""  # always reset, so main.py applies it fresh
        if key in manual_by_key:
            updated += 1
        else:
            added += 1
        manual_by_key[key] = row

    m._rewrite_report(manual_path, m.MANUAL_FIELDS, manual_by_key)
    print(f"Merged into {manual_path}: {added} new row(s), {updated} updated row(s).")
    print("Run main.py to apply them.")


if __name__ == "__main__":
    main()
