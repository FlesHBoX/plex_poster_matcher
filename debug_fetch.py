"""
Diagnostic helper: fetches raw TPDb HTML and saves it to disk, so we can
see the ACTUAL markup (not a markdown-summarized version) and fix
tpdb_client.py's selectors to match.

Usage:
    python debug_fetch.py search "12 Monkeys"        -> debug_search.html
    python debug_fetch.py listing <full TPDb URL>     -> debug_listing.html

For "listing", pass the URL of a title's poster-listing page, e.g.:
    python debug_fetch.py listing https://theposterdb.com/posters/41443

Then send back (or open and describe) the saved HTML file.
"""
import sys

import requests

import config

if len(sys.argv) < 3:
    sys.exit(
        "Usage:\n"
        '  python debug_fetch.py search "Movie Title"\n'
        "  python debug_fetch.py listing <full TPDb posters URL>"
    )

mode, arg = sys.argv[1], sys.argv[2]
session = requests.Session()
session.headers.update({"User-Agent": config.USER_AGENT})

if mode == "search":
    resp = session.get(
        f"{config.TPDB_BASE}/search", params={"term": arg, "section": "movies"}, timeout=20
    )
    outfile = "debug_search.html"
elif mode == "listing":
    resp = session.get(arg, timeout=20)
    outfile = "debug_listing.html"
else:
    sys.exit("First argument must be 'search' or 'listing'")

resp.raise_for_status()
with open(outfile, "w", encoding="utf-8") as f:
    f.write(resp.text)

print(f"Saved {len(resp.text)} bytes to {outfile}")
print(f"Status: {resp.status_code}")
