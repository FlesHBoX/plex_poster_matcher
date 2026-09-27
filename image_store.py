"""Downloads matched poster images to disk with human-readable filenames,
sorted into movies/ and shows/ subfolders — mainly so they're easy to
eyeball for verification before (or instead of) trusting the CSV report.
"""
import os
import re

import requests

import config

_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*]')


def safe_filename(title, year, ext):
    """'Alien', 1979, '.jpg' -> 'Alien (1979).jpg', with filesystem-unsafe
    characters stripped so it works on Windows too."""
    name = f"{title} ({year})" if year else title
    name = _INVALID_FILENAME_CHARS_RE.sub("", name).strip()
    return f"{name}{ext}"


def poster_dir(output_dir, title, year, item_type, collection_name=None):
    """
    Where a poster for this item should live.
    Movies: flat output_dir/posters/movies/, or a collection subfolder
    output_dir/posters/movies/<Collection Name>/ when collection_name is
    given (only meaningful when the movie belongs to exactly one Plex
    collection, or one has been manually chosen — see main.py).
    Shows: one subfolder per show, output_dir/posters/shows/<Show (Year)>/
    — so a show's season posters (once fetched) can sit alongside its
    main poster in the same place, e.g.:
        output/posters/shows/Ted Lasso (2020)/Ted Lasso (2020).jpg
        output/posters/shows/Ted Lasso (2020)/Season 1.jpg
    """
    if item_type == "movie":
        base = os.path.join(output_dir, "posters", "movies")
        if collection_name:
            return os.path.join(base, safe_filename(collection_name, None, ""))
        return base
    show_folder = safe_filename(title, year, "")
    return os.path.join(output_dir, "posters", "shows", show_folder)


def season_filename(season_number, ext):
    """0 -> 'Specials.jpg', 1 -> 'Season 1.jpg', etc. — sits alongside the
    main poster inside that show's own subfolder, so no need to repeat
    the show name/year in this one."""
    label = "Specials" if season_number == 0 else f"Season {season_number}"
    return f"{label}{ext}"


def ext_from_url(url):
    path = url.split("?", 1)[0]
    _, ext = os.path.splitext(path)
    return ext if ext else ".jpg"


def download_poster(session, image_url, dest_path):
    """Download image_url to dest_path using the given requests.Session
    (reuse the TPDbClient's session so headers/connection pooling carry
    over). Raises on failure — caller decides how to handle that."""
    resp = session.get(image_url, timeout=30)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
