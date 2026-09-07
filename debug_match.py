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
candidates = tpdb.search_with_fallback(search_title, category)

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
            for uploader, poster_id, image_url in found:
                print(f"    -> {uploader}  (data-poster-id={poster_id})  {image_url}")
