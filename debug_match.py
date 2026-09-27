"""
Diagnostic helper: shows exactly which TPDb candidates were found for a
title, and how the matcher scored each one — so we can see WHY something
got flagged ambiguous (or unmatched) instead of guessing.

Usage:
    python debug_match.py "Anomalisa" 2015 movie
"""
import sys

import config
import tpdb_client as tc
import matcher
from tpdb_client import TPDbClient

if len(sys.argv) < 4:
    sys.exit('Usage: python debug_match.py "Title" YEAR movie|show')

title, year, category = sys.argv[1], int(sys.argv[2]), sys.argv[3]

tpdb = TPDbClient()

search_title = tc.strip_trailing_year(title)
if search_title != title:
    print(f"Title has a trailing (YYYY) baked in -> searching with that stripped: {search_title!r}")

short = tc.shortened_title(search_title)
if short:
    print(f"Title has a subtitle separator -> also trying shortened search term: {short!r}")

numeral = tc.numeral_variant_title(search_title)
if numeral:
    print(f"Title has a swappable number word/digit -> also trying: {numeral!r}")
    numeral_short = tc.shortened_title(numeral)
    if numeral_short:
        print(f"  ...and its shortened form: {numeral_short!r}")

candidates = tpdb.search_with_fallback(search_title, category)

if not any(c.get("year") == year for c in candidates):
    print(f"Nothing on the quick pass matches year {year} — trying deep pagination "
          f"(up to 40 pages, stopping as soon as a page has a year match)...")
    deep, pages_fetched = tpdb.search_deep_until_year(search_title, category, year)
    seen = {c["url"] for c in candidates}
    new_ones = [c for c in deep if c["url"] not in seen]
    candidates += new_ones
    found_it = any(c.get("year") == year for c in new_ones)
    print(f"Deep search fetched {pages_fetched} page(s), adding {len(new_ones)} new candidate(s).")
    if found_it:
        print(f"Found a year-{year} match on page {pages_fetched}.")
    else:
        print(f"Did NOT find a year-{year} match — exhausted all {pages_fetched} available page(s).")
    print()

print(f"Plex title: {title!r} ({year})")
print(f"TPDb returned {len(candidates)} combined raw candidate(s) for section={category}:\n")

norm_target = matcher._normalize(search_title)
exact_year = [c for c in candidates if c.get("year") == year]
near_year = [
    c for c in candidates
    if c.get("year") and c["year"] != year and abs(c["year"] - year) <= matcher.YEAR_TOLERANCE
]
pool = exact_year or near_year or candidates
pool_desc = "exact-year" if exact_year else ("near-year" if near_year else "unfiltered (no year info matched)")
print(f"Year-filtered pool ({pool_desc}): {len(pool)} candidate(s) considered\n")

scored = [(matcher._strict_score(norm_target, matcher._normalize(c["title"])), c) for c in pool]
scored.sort(key=lambda x: x[0], reverse=True)

print("Strict (token_sort_ratio) scores:")
for score, c in scored:
    print(f"  {score:6.1f}  {c['title']!r} ({c['year']})  {c['url']}")

if not scored or scored[0][0] < matcher.TITLE_SCORE_THRESHOLD:
    print(f"\nNothing cleared {matcher.TITLE_SCORE_THRESHOLD} strictly — loose (subset) scores:")
    loose_scored = [
        (matcher._loose_score(norm_target, matcher._normalize(c["title"])), c)
        for c in pool
    ]
    loose_scored.sort(key=lambda x: x[0], reverse=True)
    for score, c in loose_scored:
        print(f"  {score:6.1f}  {c['title']!r} ({c['year']})  {c['url']}")

print()
match, ambiguous, tied = matcher.best_match(search_title, year, candidates)
print(f"RESULT: match={match}, ambiguous={ambiguous}, tied_count={len(tied)}")

if ambiguous:
    print(f"\nTied candidates ({len(tied)}) — checking each one's actual poster listing")
    print(f"for a target-uploader ({', '.join(config.TPDB_USERS)}) poster, same as main.py does:\n")
    for cand in tied:
        error_msg = None
        try:
            found = tpdb.find_target_posters(cand["url"], config.TPDB_USERS)
        except Exception as exc:
            found = None
            error_msg = str(exc)
        print(f"  {cand['url']}")
        if found is None:
            print(f"    -> ERROR checking this candidate: {error_msg}")
        elif not found:
            print(f"    -> no target-uploader poster found (0 results)")
        else:
            for f in found:
                season_note = f"  [season {f['season_number']}]" if f["season_number"] is not None else ""
                print(f"    -> {f['uploader']}  (data-poster-id={f['poster_id']}){season_note}  {f['image_url']}")

elif match is not None:
    print(f"\nChecking {match['url']} for a target-uploader ({', '.join(config.TPDB_USERS)}) poster...")
    try:
        found = tpdb.find_target_posters(match["url"], config.TPDB_USERS)
    except Exception as exc:
        found = None
        print(f"  ERROR: {exc}")

    if found is not None:
        main_posters = [f for f in found if f["season_number"] is None]
        if not main_posters:
            print("  no target-uploader main poster found on this page")
        else:
            priority = {u.lower(): i for i, u in enumerate(config.TPDB_USERS)}
            main_posters.sort(key=lambda f: priority.get(f["uploader"].lower(), 999))
            chosen = main_posters[0]
            print(f"  chosen main poster: {chosen['uploader']}  (data-poster-id={chosen['poster_id']})  {chosen['image_url']}")
            if len(main_posters) > 1:
                print(f"  ({len(main_posters)} target-uploader options total — see multi_options.json handling)")

            if not category.lower().startswith("movie"):
                if not chosen.get("set_url"):
                    print("  chosen poster has no Set page -> no season posters possible")
                else:
                    print(f"\n  Fetching Set page for season posters: {chosen['set_url']}")
                    try:
                        season_posters = tpdb.find_season_posters(chosen["set_url"], expected_show_url=match["url"])
                    except Exception as exc:
                        print(f"    ERROR: {exc}")
                        season_posters = []
                    if not season_posters:
                        print("    no season-suffixed entries found on this Set page")
                    else:
                        print(f"    found {len(season_posters)} season entr{'y' if len(season_posters)==1 else 'ies'}:")
                        for sp in season_posters:
                            print(f"      season {sp['season_number']}  {sp['uploader']}  "
                                  f"(data-poster-id={sp['poster_id']})  title={sp['title']!r}  {sp['image_url']}")
