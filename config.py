"""
Configuration, loaded from a .env file in this directory (falls back to
plain environment variables if you'd rather set those instead — either
works, .env just means you don't have to re-type `set PLEX_URL=...` every
time you open a new terminal).

Required:
    PLEX_URL    e.g. "http://192.168.1.50:32400"
    PLEX_TOKEN  Your Plex auth token (see README for how to find it)

Optional:
    TPDB_USERS       Comma-separated uploader names, in priority order.
                      Default: "DarkMatte,djchrisallen"
    REQUEST_DELAY     Seconds to wait between requests to TheposterDB.
                      Default: 1.0 — be polite, this is someone else's server.
    OUTPUT_DIR        Where reports get written. Default: "./output"

See .env.example for a template. Copy it to .env and fill in your real
values — .env is listed in .gitignore, so it never gets committed even
if you push this project to a repo.
"""
import os

from dotenv import load_dotenv

load_dotenv()  # loads .env into the environment if present; does nothing if it's not

PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")

TPDB_USERS = [
    u.strip() for u in os.environ.get("TPDB_USERS", "DarkMatte,djchrisallen").split(",") if u.strip()
]

REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "1.0"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "./output")

TPDB_BASE = "https://theposterdb.com"
USER_AGENT = "PlexPosterMatcher/1.0 (personal-use script; +https://github.com/)"


def validate():
    missing = []
    if not PLEX_URL:
        missing.append("PLEX_URL")
    if not PLEX_TOKEN:
        missing.append("PLEX_TOKEN")
    if missing:
        raise SystemExit(
            f"Missing required setting(s): {', '.join(missing)}\n"
            "Easiest fix: copy .env.example to .env in this folder and fill in your "
            "real values — it'll be picked up automatically next run.\n"
            "Or set them as environment variables for this session instead, e.g.:\n"
            '  export PLEX_URL="http://localhost:32400"   (Windows: set PLEX_URL=...)\n'
            '  export PLEX_TOKEN="your-token-here"'
        )
