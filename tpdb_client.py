"""
Minimal scraping client for TheposterDB (theposterdb.com).

TPDb has no public search/download API, so this works entirely off the
public HTML pages:

  1. GET /search?term=<title>            -> find the movie/show's page
  2. GET /posters/<id> (paginated)        -> list every uploader's poster
                                             for that title
  3. GET /poster/<poster_id>              -> the individual poster page,
                                             whose og:image meta tag gives
                                             the direct, unauthenticated
                                             CDN image URL

No login or session is required for any of this — the "Download" button
on the site redirects to /login, but the underlying image itself is
public (it has to be, for link previews/social cards to work), so we
just skip that button entirely and read the CDN URL straight from the
page's metadata.

NOTE: this is HTML scraping of a third-party site, not an official API.
TPDb's markup could change without notice, which would break the CSS
selectors below. If searches suddenly stop finding anything, that's the
first thing to check — open a poster/search page in a browser, view
source, and see whether the patterns below (`/poster/<id>`, `/posters/<id>`,
`/user/<name>`) still look the same.
"""
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import config

POSTERS_LINK_RE = re.compile(r"^/posters/\d+/?$")
TITLE_YEAR_RE = re.compile(r"^(.*?)\s*\((\d{4}|N/A)\)\s*$")
_SUBTITLE_SPLIT_RE = re.compile(r"\s*:\s*|\s+-\s+")


def shortened_title(title):
    """
    Returns the portion of a title before a colon or ' - ' separator,
    e.g. "2010: The Year We Make Contact" -> "2010". Returns None if
    there's no such separator.

    Used as a fallback search term: TPDb's own search seems to rank
    poorly for long, subtitle-heavy queries when the actual database
    entry is filed under just the short title. Confirmed via
    debug_match.py — searching TPDb for the full title "2010: The Year
    We Make Contact" returns 36 results, none of which is the bare
    "2010" entry that DOES exist and DOES have a target-uploader poster.
    """
    parts = _SUBTITLE_SPLIT_RE.split(title, maxsplit=1)
    if len(parts) > 1 and parts[0].strip():
        return parts[0].strip()
    return None


class TPDbClient:
    def __init__(self, delay=None):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self.delay = config.REQUEST_DELAY if delay is None else delay

    def _get_soup(self, url, params=None):
        time.sleep(self.delay)
        resp = self.session.get(url, params=params, timeout=20)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")

    def search(self, term, category):
        """
        category: 'movies' or 'shows'.
        Returns a list of dicts: {"title": str, "year": int|None, "url": str}

        TPDb's search page renders results for exactly one category at a
        time, selected via a &section= query param (movies/shows/...) —
        without it, the server defaults to "movies" regardless of what
        you're actually looking for. So we always pass it explicitly.
        """
        section = "movies" if category.lower().startswith("movie") else "shows"
        soup = self._get_soup(
            f"{config.TPDB_BASE}/search", params={"term": term, "section": section}
        )

        # Results for the selected section live inside #tabs-active.
        container = soup.select_one("#tabs-active") or soup

        results = []
        for a in container.find_all("a", href=True):
            if not POSTERS_LINK_RE.match(_path(a["href"])):
                continue
            results.append(_parse_result_link(a))

        # De-dupe while preserving order
        seen = set()
        deduped = []
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                deduped.append(r)
        return deduped

    def search_with_fallback(self, title, category):
        """
        search(), plus: if the title has a colon/dash subtitle separator,
        also search using just the part before it, and merge in any new
        results. Caller still does its own title/year matching against
        the combined list — this only widens the candidate pool.
        """
        candidates = self.search(title, category)
        short = shortened_title(title)
        if short:
            more = self.search(short, category)
            seen = {c["url"] for c in candidates}
            candidates = candidates + [c for c in more if c["url"] not in seen]
        return candidates

    def get_poster_image_url(self, poster_detail_url):
        """
        Given an individual /poster/<id> detail page URL, return its
        direct CDN image URL via the page's og:image meta tag.

        Not used by the automated search/match pipeline (which reads the
        image URL straight off the listing page's <picture> element
        instead, saving a request) — this is for manually-specified
        matches in manual.csv, where a person browsed to a specific
        poster's own page themselves and pasted that URL, possibly from
        an uploader outside the configured TPDB_USERS list entirely.
        """
        soup = self._get_soup(poster_detail_url)
        meta = soup.find("meta", attrs={"property": "og:image"})
        if meta and meta.get("content"):
            return meta["content"]
        return None

    def find_target_posters(self, item_url, target_users, max_pages=20):
        """
        Walk the (paginated) poster-listing page for a movie/show/collection
        and return every poster uploaded by one of target_users, as a list
        of (uploader, poster_id, image_url) tuples. image_url is the direct
        CDN URL taken straight from the listing page itself — TPDb embeds
        it in the card's <picture> element, so no extra per-poster request
        is needed to resolve it.
        """
        found = []
        url = item_url
        wanted_lower = {u.lower() for u in target_users}

        for _ in range(max_pages):
            if not url:
                break
            soup = self._get_soup(url)

            for overlay in soup.select("div.overlay.rounded-poster[data-poster-id]"):
                uploader_link = overlay.select_one(".uploaded-by a")
                if not uploader_link:
                    continue
                uploader = uploader_link.get_text(strip=True)
                if uploader.lower() not in wanted_lower:
                    continue

                # The <picture> with the actual image is a sibling of this
                # overlay div, inside their shared .hovereffect container.
                container = overlay.find_parent(class_="hovereffect")
                image_url = None
                if container:
                    jpeg_source = container.select_one('picture source[type="image/jpeg"]')
                    if jpeg_source and jpeg_source.get("srcset"):
                        image_url = jpeg_source["srcset"]

                if image_url:
                    found.append((uploader, overlay.get("data-poster-id"), image_url))

            next_link = soup.select_one('a.page-link[rel="next"]')
            url = urljoin(config.TPDB_BASE, next_link["href"]) if (
                next_link and next_link.get("href")
            ) else None

        return found


def _path(href):
    """Strip domain/query from a URL so regexes only see the path."""
    href = href.split("?", 1)[0]
    if href.startswith("http"):
        href = "/" + href.split("/", 3)[-1]
    return href


def _parse_result_link(a_tag):
    # separator=" " matters here: TPDb's markup has the title in a <strong>
    # and the "(year)" as a trailing text node with no space in the source,
    # so a plain get_text(strip=True) would glue them together as
    # "Titleword(1995)" with no space before the parenthesis.
    text = a_tag.get_text(" ", strip=True)
    m = TITLE_YEAR_RE.match(text)
    if m:
        title, year_str = m.group(1), m.group(2)
        year = int(year_str) if year_str != "N/A" else None
    else:
        title, year = text, None
    return {"title": title, "year": year, "url": urljoin(config.TPDB_BASE, a_tag["href"])}
