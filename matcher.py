"""Fuzzy-matches a Plex library item against TPDb search results."""
import re

from rapidfuzz import fuzz

TITLE_SCORE_THRESHOLD = 85  # rapidfuzz token_sort_ratio, 0-100
YEAR_TOLERANCE = 1  # allow off-by-one-year mismatches (metadata source drift)

_STRIP_RE = re.compile(r"[^a-z0-9 ]")

# TPDb (sourced from TMDb) sometimes uses the spelled-out official title
# ("Twelve Monkeys") where Plex/other metadata sources use the numeral
# ("12 Monkeys"), or vice versa. Covers common small numbers seen in
# movie titles; deliberately not exhaustive.
_NUMBER_WORDS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
    "10": "ten", "11": "eleven", "12": "twelve", "13": "thirteen",
    "14": "fourteen", "15": "fifteen", "16": "sixteen", "17": "seventeen",
    "18": "eighteen", "19": "nineteen", "20": "twenty",
}
_WORD_NUMBERS = {v: k for k, v in _NUMBER_WORDS.items()}


def _normalize(title):
    t = title.lower()
    t = re.sub(r"^\s*the\s+", "", t)  # drop leading "the"
    t = _STRIP_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _number_variant(normalized_title):
    """Swap digit<->word numbers token-by-token. Returns None if no swap applied."""
    tokens = normalized_title.split()
    changed = False
    out = []
    for tok in tokens:
        if tok in _NUMBER_WORDS:
            out.append(_NUMBER_WORDS[tok])
            changed = True
        elif tok in _WORD_NUMBERS:
            out.append(_WORD_NUMBERS[tok])
            changed = True
        else:
            out.append(tok)
    return " ".join(out) if changed else None


def _strict_score(norm_a, norm_b):
    """
    token_sort_ratio only (plus number-word variants) — a real,
    close-to-full-string match. Doesn't credit one title merely
    containing the other's words amid unrelated extra text.
    """
    candidates_a = [norm_a]
    if (v := _number_variant(norm_a)) is not None:
        candidates_a.append(v)
    candidates_b = [norm_b]
    if (v := _number_variant(norm_b)) is not None:
        candidates_b.append(v)

    return max(fuzz.token_sort_ratio(a, b) for a in candidates_a for b in candidates_b)


def _loose_score(norm_a, norm_b):
    """
    Adds token_set_ratio on top of _strict_score: credits one title's
    words being a full subset of the other's, e.g. TPDb listing "2010"
    where Plex has "2010: The Year We Make Contact".

    Deliberately used only as a fallback (see best_match) rather than
    blended into the primary score — token_set_ratio is easily fooled by
    something like a making-of documentary called "Crafting Anomalisa",
    which is trivially "contains all the words of Anomalisa" but is
    obviously not the same title. Reserving it for when nothing clears
    the bar strictly means it never gets to outrank a real exact match.
    """
    candidates_a = [norm_a]
    if (v := _number_variant(norm_a)) is not None:
        candidates_a.append(v)
    candidates_b = [norm_b]
    if (v := _number_variant(norm_b)) is not None:
        candidates_b.append(v)

    scores = []
    for a in candidates_a:
        for b in candidates_b:
            scores.append(fuzz.token_sort_ratio(a, b))
            scores.append(fuzz.token_set_ratio(a, b))
    return max(scores)


def _pick(pool, score_fn):
    """Score every candidate in pool, return (best_score, sorted list of (score, candidate))."""
    scored = [(score_fn(c), c) for c in pool]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][0], scored


def _tiered_title_match(norm_target, pool):
    """
    Run the strict->loose title-scoring tiers against a single candidate
    pool. Returns the tied list (see best_match) if something clears
    TITLE_SCORE_THRESHOLD, or None if nothing in this pool does.
    """
    if not pool:
        return None

    best_strict, strict_scored = _pick(
        pool, lambda c: _strict_score(norm_target, _normalize(c["title"]))
    )
    if best_strict >= TITLE_SCORE_THRESHOLD:
        return [c for s, c in strict_scored if best_strict - s <= 3]

    best_loose, loose_scored = _pick(
        pool, lambda c: _loose_score(norm_target, _normalize(c["title"]))
    )
    if best_loose >= TITLE_SCORE_THRESHOLD:
        return [c for s, c in loose_scored if best_loose - s <= 3]

    return None


def best_match(plex_title, plex_year, candidates):
    """
    candidates: list of {"title", "year", "url"} from TPDbClient.search()

    Returns (match_dict, ambiguous: bool, tied: list[dict]).
    - match_dict is None if nothing scored high enough (tied will be []).
    - tied is every candidate within 3 points of the best score — always
      includes match_dict itself, even when there's only one (len 1).
      Callers can use this to try to resolve an apparent tie by some
      means other than title text (e.g. checking which candidate(s)
      actually have relevant content) before treating it as a real
      ambiguity.
    - ambiguous=True is just a convenience flag for len(tied) > 1.

    Year is used to narrow the candidate pool in tiers, tried in order
    until one produces a confident title match:
      1. exact year match
      2. within YEAR_TOLERANCE of Plex's year (absorbs small metadata
         drift — e.g. TPDb lists "Aniara" as 2019, a wider/int'l release
         year, where Plex has 2018)
      3. no year filtering at all, but ONLY when Plex has no year on
         record for this item in the first place

    Each tier is only used if the previous one failed to find anything
    confident — an exact-year candidate that doesn't actually look like
    the right title (e.g. a same-year unrelated film that happens to
    share a title with next year's release) must not block falling
    through to the next tier. This also still protects the earlier fix
    for heavily-sequeled titles ("Alien" -> "Aliens", "Alien 3", ...): if
    the exact-year candidate DOES look right, later tiers are never even
    considered, so a sequel from a different year can't sneak in as a
    tie.

    Title scoring within each tier is itself tiered: try a strict
    (token_sort_ratio) match first. Only if NOTHING clears the threshold
    strictly do we fall back to the looser subset-aware score. This keeps
    a real exact match from ever getting outranked or tied by an
    unrelated title that merely contains the same word(s) amid extra text
    (a "making of" documentary, a clip compilation, etc.).
    """
    norm_target = _normalize(plex_title)

    if plex_year:
        exact_year = [c for c in candidates if c.get("year") == plex_year]
        near_year = [
            c for c in candidates
            if c.get("year") and c["year"] != plex_year
            and abs(c["year"] - plex_year) <= YEAR_TOLERANCE
        ]
        for pool in (exact_year, near_year):
            tied = _tiered_title_match(norm_target, pool)
            if tied:
                return tied[0], len(tied) > 1, tied
        return None, False, []

    tied = _tiered_title_match(norm_target, candidates)
    if tied:
        return tied[0], len(tied) > 1, tied
    return None, False, []
