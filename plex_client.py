"""Thin wrapper around plexapi for pulling library items and applying posters."""
from plexapi.server import PlexServer

import config


def connect():
    return PlexServer(config.PLEX_URL, config.PLEX_TOKEN)


def get_library_items(plex):
    """
    Returns a list of dicts for every movie and show in every relevant
    library section:
        {"title": str, "year": int|None, "type": "movie"|"show", "item": <plexapi object>}

    Deliberately does NOT fetch Collection membership here — see
    get_movie_collections, called lazily per-item instead. Collections
    aren't reliably included in the bulk listing response, and eagerly
    accessing that attribute for the whole library up front (before any
    per-item progress is printed) turned a 1500-item library into 1500+
    extra blocking requests with zero visible output — looked exactly
    like a hang.
    """
    items = []
    for section in plex.library.sections():
        if section.type not in ("movie", "show"):
            continue
        for entry in section.all():
            items.append(
                {
                    "title": entry.title,
                    "year": getattr(entry, "year", None),
                    "type": section.type,
                    "item": entry,
                }
            )
    return items


def get_movie_collections(plex_item):
    """
    Fetches a single Plex item's Collection tag names, on demand — call
    this only for the specific movie being processed right now, never
    across the whole library up front (see get_library_items).

    Calls .reload() first: Plex's bulk library-listing response can
    return a truncated view of multi-valued fields like collections (to
    save bandwidth), with the complete list only available via a
    per-item detail fetch. Unverified against a live server — if a movie
    still shows fewer collections than it actually has in Plex, this
    reload is the first thing to double check.
    """
    try:
        plex_item.reload()
    except Exception:
        pass  # fall back to whatever's already loaded rather than failing outright
    try:
        return [c.tag for c in getattr(plex_item, "collections", [])]
    except Exception:
        return []


def apply_poster(plex_item, image_url=None, filepath=None, dry_run=False):
    """Set a Plex item's poster, either from a local file or a (publicly
    reachable) URL that Plex itself fetches server-side. filepath takes
    priority when both are given.
    """
    if dry_run:
        return
    if filepath:
        plex_item.uploadPoster(filepath=filepath)
    else:
        plex_item.uploadPoster(url=image_url)
