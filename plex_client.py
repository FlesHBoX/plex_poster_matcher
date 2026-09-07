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
