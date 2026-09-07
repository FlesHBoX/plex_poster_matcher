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
