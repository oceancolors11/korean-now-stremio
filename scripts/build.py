#!/usr/bin/env python3

import os
import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests


# ============================================================
# Configuration
# ============================================================

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"

TOKEN = os.environ.get("TMDB_TOKEN")

if not TOKEN:
    raise SystemExit("TMDB_TOKEN is not set")


# ============================================================
# TMDB session
# ============================================================

session = requests.Session()

session.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "accept": "application/json",
    "User-Agent": "KoreanNow-Stremio/1.0"
})


# ============================================================
# Dates
# ============================================================

TODAY = datetime.now(
    ZoneInfo("Asia/Riyadh")
).date()

MONTH_START = TODAY.replace(day=1)

# Recent enough to catch dramas that started shortly before
# the current month and are still relevant.
RECENT_START = TODAY - timedelta(days=120)

# Small future window for upcoming/current episodes.
FUTURE_END = TODAY + timedelta(days=21)


# ============================================================
# TMDB helpers
# ============================================================

def tmdb(path, params=None):
    """
    Request data from TMDB.
    """
    r = session.get(
        API + path,
        params=params or {},
        timeout=30
    )

    r.raise_for_status()

    return r.json()


def img(path, size):
    """
    Convert a TMDB image path to a full URL.
    """
    if not path:
        return None

    return f"{IMG}/{size}{path}"


def parse(d):
    """
    Parse YYYY-MM-DD safely.
    """
    try:
        return date.fromisoformat(d)
    except Exception:
        return None


def discover(params, pages=3):
    """
    Run TMDB /discover/tv over multiple pages.
    """
    out = []

    for p in range(1, pages + 1):
        q = dict(params)
        q["page"] = p

        data = tmdb("/discover/tv", q)

        out.extend(data.get("results", []))

        if p >= data.get("total_pages", 1):
            break

        time.sleep(0.12)

    return out


# ============================================================
# Genre filtering
# ============================================================

# TMDB TV genre IDs we actually want.
#
# Drama is the important one.
# The other genres are allowed as secondary genres.
ALLOWED_GENRES = {
    18,       # Drama
    35,       # Comedy
    80,       # Crime
    9648,     # Mystery
    10749,    # Romance
    10759,    # Action & Adventure
    10765,    # Sci-Fi & Fantasy
    10766,    # Soap
    10768,    # War & Politics
}


# These are the types of content we do NOT want.
EXCLUDED_GENRES = {
    16,       # Animation
    99,       # Documentary
    10751,    # Family
    10762,    # Kids
    10763,    # News
    10764,    # Reality
    10767,    # Talk
}


def is_drama_candidate(show):
    """
    Return True only for Korean drama-oriented TV series.

    The crucial rule is that the title must have Drama (18).
    This prevents variety/reality/talk shows such as Running Man
    from entering the catalog.
    """

    genre_ids = set(show.get("genre_ids") or [])

    # Must contain Drama.
    if 18 not in genre_ids:
        return False

    # Reject clearly unwanted TV categories.
    if genre_ids.intersection(EXCLUDED_GENRES):
        return False

    # Keep only titles that have at least one recognized genre.
    if not genre_ids.intersection(ALLOWED_GENRES):
        return False

    return True


# ============================================================
# Candidate pools
# ============================================================

pool = []


# ------------------------------------------------------------
# Pool 1:
# Currently airing / recently active Korean dramas
# ------------------------------------------------------------

pool += discover(
    {
        "language": "en-US",
        "with_original_language": "ko",
        "with_origin_country": "KR",
        "with_genres": "18",
        "include_adult": "false",
        "include_null_first_air_dates": "false",

        # Episode activity around the current period.
        "air_date.gte": str(RECENT_START),
        "air_date.lte": str(FUTURE_END),

        "sort_by": "popularity.desc",
        "vote_count.gte": "5",
    },
    pages=6
)


# ------------------------------------------------------------
# Pool 2:
# Dramas that started this month
# ------------------------------------------------------------

pool += discover(
    {
        "language": "en-US",
        "with_original_language": "ko",
        "with_origin_country": "KR",
        "with_genres": "18",
        "include_adult": "false",
        "include_null_first_air_dates": "false",

        "first_air_date.gte": str(MONTH_START),
        "first_air_date.lte": str(TODAY),

        "sort_by": "popularity.desc",
        "vote_count.gte": "3",
    },
    pages=5
)


# ------------------------------------------------------------
# Pool 3:
# Recent Korean dramas from the last 120 days
#
# This catches good/new shows that may no longer appear
# in the episode-date discovery pool.
# ------------------------------------------------------------

pool += discover(
    {
        "language": "en-US",
        "with_original_language": "ko",
        "with_origin_country": "KR",
        "with_genres": "18",
        "include_adult": "false",
        "include_null_first_air_dates": "false",

        "first_air_date.gte": str(RECENT_START),
        "first_air_date.lte": str(TODAY),

        "sort_by": "popularity.desc",
        "vote_count.gte": "3",
    },
    pages=5
)


# ============================================================
# De-duplicate candidates
# ============================================================

by_id = {}

for show in pool:
    show_id = show.get("id")

    if not show_id:
        continue

    if not is_drama_candidate(show):
        continue

    by_id[show_id] = show


candidates = list(by_id.values())


print(
    f"TMDB returned {len(pool)} raw candidates; "
    f"{len(candidates)} passed genre filtering."
)


# ============================================================
# Basic scoring
# ============================================================

def active_score(show):
    """
    Preliminary score used to decide which titles deserve
    detailed TMDB requests.
    """

    first = parse(show.get("first_air_date", ""))
    last = parse(show.get("last_air_date", ""))

    next_ep = show.get("next_episode_to_air")

    status = show.get("status", "")

    popularity = float(
        show.get("popularity") or 0
    )

    votes = int(
        show.get("vote_count") or 0
    )

    is_new = bool(
        first and MONTH_START <= first <= TODAY
    )

    is_recent = bool(
        first and first >= RECENT_START
    )

    # A show with an upcoming episode is strongly likely
    # to still be airing.
    airing = bool(next_ep)

    # Fallback for shows where next_episode_to_air is absent.
    if not airing:
        airing = bool(
            last
            and last >= TODAY - timedelta(days=21)
            and status in {
                "Returning Series",
                "In Production",
                "Planned",
            }
        )

    # --------------------------------------------------------
    # Score
    # --------------------------------------------------------

    score = popularity

    # Current airing gets the highest boost.
    if airing:
        score += 180

    # New this month.
    if is_new:
        score += 100

    # Recent.
    elif is_recent:
        score += 45

    # More votes = more stable popularity signal.
    score += min(votes, 10000) / 200

    return score


# ============================================================
# Sort candidates before detailed requests
# ============================================================

candidates.sort(
    key=active_score,
    reverse=True
)


# ============================================================
# Fetch detailed metadata
# ============================================================

selected = []


# Fetch more candidates than we finally need.
# This allows bad/unwanted results to be filtered out.
for basic in candidates[:100]:

    try:

        detail = tmdb(
            f"/tv/{basic['id']}",
            {
                "language": "en-US",

                "append_to_response":
                    "images,videos,credits",

                "include_image_language":
                    "en,null,ko",
            }
        )

        # ----------------------------------------------------
        # Detailed genre filtering
        # ----------------------------------------------------

        detailed_genre_ids = {
            g.get("id")
            for g in detail.get("genres", [])
            if g.get("id") is not None
        }

        # Must still be Drama.
        if 18 not in detailed_genre_ids:
            continue

        # Reject unwanted categories.
        if detailed_genre_ids.intersection(
            EXCLUDED_GENRES
        ):
            continue

        # ----------------------------------------------------
        # Dates / status
        # ----------------------------------------------------

        status = detail.get("status", "")

        first = parse(
            detail.get("first_air_date", "")
        )

        last = parse(
            detail.get("last_air_date", "")
        )

        next_ep = detail.get(
            "next_episode_to_air"
        )

        airing = bool(next_ep)

        if not airing:
            airing = bool(
                last
                and last >= TODAY - timedelta(days=21)
                and status in {
                    "Returning Series",
                    "In Production",
                    "Planned",
                }
            )

        is_new = bool(
            first
            and MONTH_START <= first <= TODAY
        )

        is_recent = bool(
            first
            and first >= RECENT_START
        )

        # ----------------------------------------------------
        # Keep only current / new / recent dramas
        # ----------------------------------------------------

        if not (
            airing
            or is_new
            or is_recent
        ):
            continue

        # ----------------------------------------------------
        # Store internal scoring flags
        # ----------------------------------------------------

        detail["_airing"] = airing
        detail["_is_new"] = is_new
        detail["_is_recent"] = is_recent
        detail["_score"] = active_score(detail)

        selected.append(detail)

    except Exception as e:

        print(
            "detail failed",
            basic.get("id"),
            e
        )


# ============================================================
# Final ordering
# ============================================================

def final_sort_key(show):

    # Current airing first.
    airing = 1 if show.get("_airing") else 0

    # Then new this month.
    new = 1 if show.get("_is_new") else 0

    # Then recent.
    recent = 1 if show.get("_is_recent") else 0

    score = float(
        show.get("_score") or 0
    )

    return (
        airing,
        new,
        recent,
        score,
    )


selected.sort(
    key=final_sort_key,
    reverse=True
)


# Keep a useful number for the catalog.
selected = selected[:36]


print(
    f"Selected {len(selected)} Korean dramas."
)


# ============================================================
# Backdrop selection
# ============================================================

def choose_backdrop(d):

    images = d.get("images") or {}

    backdrops = images.get(
        "backdrops"
    ) or []

    # Fallback to the main TMDB backdrop.
    if not backdrops:

        if d.get("backdrop_path"):
            return img(
                d["backdrop_path"],
                "w1920"
            )

        return None


    def score(x):

        aspect_ratio = float(
            x.get("aspect_ratio") or 0
        )

        width = int(
            x.get("width") or 0
        )

        height = int(
            x.get("height") or 0
        )

        vote_average = float(
            x.get("vote_average") or 0
        )

        vote_count = int(
            x.get("vote_count") or 0
        )

        language = x.get(
            "iso_639_1"
        )


        score_value = (
            vote_average * 8
        )

        score_value += (
            min(vote_count, 100) * 0.08
        )


        # Ideal cinematic landscape ratio.
        if 1.70 <= aspect_ratio <= 1.82:
            score_value += 20


        # Prefer large images.
        if width >= 1920:
            score_value += 12

        elif width >= 1280:
            score_value += 6


        # Prefer wider images.
        if width >= 1600 and height >= 700:
            score_value += 4


        # Text-free / neutral language images
        # are usually better for Hero backgrounds.
        if language is None:
            score_value += 8


        return score_value


    backdrops = sorted(
        backdrops,
        key=score,
        reverse=True
    )


    return img(
        backdrops[0]["file_path"],
        "w1920"
    ) if backdrops[0].get("file_path") else None


# ============================================================
# Logo selection
# ============================================================

def choose_logo(d):

    images = d.get("images") or {}

    logos = images.get(
        "logos"
    ) or []

    if not logos:
        return None


    def score(x):

        language = x.get(
            "iso_639_1"
        )

        width = int(
            x.get("width") or 0
        )

        vote_average = float(
            x.get("vote_average") or 0
        )

        score_value = (
            vote_average * 6
        )

        score_value += (
            min(width, 2000) / 500
        )


        # Prefer English logo where available.
        if language == "en":
            score_value += 10

        # Korean is also very useful.
        elif language == "ko":
            score_value += 8

        # Language-neutral logos.
        elif language is None:
            score_value += 4


        return score_value


    logos = sorted(
        logos,
        key=score,
        reverse=True
    )


    if not logos[0].get("file_path"):
        return None


    return img(
        logos[0]["file_path"],
        "w500"
    )


# ============================================================
# Convert TMDB details to Stremio Meta
# ============================================================

def meta(d):

    tid = d["id"]


    cast = (
        (d.get("credits") or {}).get("cast")
        or []
    )


    return {

        "id": f"krtmdb_{tid}",

        "type": "series",

        "name":
            d.get("name")
            or d.get("original_name")
            or "Unknown",

        "poster":
            img(
                d.get("poster_path"),
                "w500"
            ),

        "posterShape":
            "poster",

        # Hero background.
        "background":
            choose_backdrop(d),

        # Transparent title logo.
        "logo":
            choose_logo(d),

        "description":
            d.get("overview")
            or "",

        "releaseInfo":
            (
                str(
                    d.get(
                        "first_air_date",
                        ""
                    )[:4]
                )
                if d.get("first_air_date")
                else ""
            ),

        "imdbRating":
            (
                f"{float(d.get('vote_average', 0)):.1f}"
                if d.get("vote_average") is not None
                else None
            ),

        "genres":
            [
                g.get("name")
                for g in d.get("genres", [])
                if g.get("name")
            ],

        "cast":
            [
                c.get("name")
                for c in cast[:8]
                if c.get("name")
            ],
    }


# ============================================================
# Build previews and metadata
# ============================================================

previews = []

metas = {}


for d in selected:

    m = meta(d)

    metas[d["id"]] = m


    # Catalog preview intentionally stays lightweight.
    # Full background/logo data comes from Meta.
    previews.append(
        {
            "id": m["id"],
            "type": "series",
            "name": m["name"],
            "poster": m["poster"],
            "posterShape": "poster",
        }
    )


# ============================================================
# Ensure directories exist
# ============================================================

(
    SITE / "catalog/series"
).mkdir(
    parents=True,
    exist_ok=True
)


(
    SITE / "meta/series"
).mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# Remove old generated files
# ============================================================

for f in (
    SITE / "catalog/series"
).glob("korean_now*.json"):

    f.unlink()


for f in (
    SITE / "catalog/series"
).glob("skip=*.json"):

    f.unlink()


for f in (
    SITE / "meta/series"
).glob("krtmdb_*.json"):

    f.unlink()


# ============================================================
# Main catalog
# ============================================================

# Stremio's catalog request uses the first page here.
# Keep 20 items per page.

PAGE_SIZE = 20


for skip in range(
    0,
    len(previews),
    PAGE_SIZE
):

    chunk = previews[
        skip:
        skip + PAGE_SIZE
    ]


    if skip == 0:

        filename = (
            "korean_now.json"
        )

    else:

        filename = (
            f"skip={skip}.json"
        )


    (
        SITE
        / "catalog/series"
        / filename
    ).write_text(
        json.dumps(
            {
                "metas": chunk
            },
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


# ============================================================
# Meta files
# ============================================================

for tid, m in metas.items():

    (
        SITE
        / "meta/series"
        / f"krtmdb_{tid}.json"
    ).write_text(
        json.dumps(
            {
                "meta": m
            },
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


# ============================================================
# Manifest
# ============================================================

manifest = {

    "id":
        "com.korean.now",

    "version":
        "1.1.1",

    "name":
        "Korean Now",

    "description":
        "Current, popular and newly released Korean drama series with TMDB backdrops and logos optimized for Stremio/Nuvio.",

    "resources":
        [
            "catalog",
            "meta"
        ],

    "types":
        [
            "series"
        ],

    "idPrefixes":
        [
            "krtmdb_"
        ],

    "catalogs":
        [
            {
                "type":
                    "series",

                "id":
                    "korean_now",

                "name":
                    "🇰🇷 Korean Now",

                "extra":
                    [
                        {
                            "name":
                                "skip",

                            "isRequired":
                                False
                        }
                    ]
            }
        ],

    "logo":
        "https://www.themoviedb.org/assets/2/v4/logos/stacked-blue-square-400.svg"
}


(
    SITE / "manifest.json"
).write_text(
    json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2
    ),
    encoding="utf-8"
)


# ============================================================
# TMDB attribution page
# ============================================================

(
    SITE / "about.html"
).write_text(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Korean Now</title>
</head>
<body>
<h1>Korean Now</h1>

<p>
This product uses the TMDB API but is not endorsed or certified by TMDB.
</p>

<p>
Data and images are provided by The Movie Database (TMDB).
</p>

</body>
</html>
""",
    encoding="utf-8"
)


# ============================================================
# Final output
# ============================================================

print(
    f"Built {len(previews)} Korean dramas for {TODAY}"
)

for i, show in enumerate(selected, start=1):

    print(
        f"{i:02d}. "
        f"{show.get('name') or show.get('original_name')} "
        f"| airing={show.get('_airing')} "
        f"| new={show.get('_is_new')} "
        f"| score={show.get('_score'):.1f}"
    )
