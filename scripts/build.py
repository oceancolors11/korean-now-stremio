#!/usr/bin/env python3

import os
import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
import cv2
import numpy as np


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
                    "ar,en,null,ko",
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

BACKDROP_CACHE = {}


def analyze_backdrop(url):
    """
    Lightweight visual analysis for Hero suitability.

    The goal is not to identify the "perfect" image, but to avoid:
    - blurry/low-quality images
    - poster-like crops
    - faces pushed against edges
    - faces in the title-safe area
    - very busy/text-heavy title areas
    """
    if not url:
        return {
            "score": -100,
            "width": 0,
            "height": 0,
        }

    if url in BACKDROP_CACHE:
        return BACKDROP_CACHE[url]

    result = {
        "score": 0,
        "width": 0,
        "height": 0,
    }

    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()

        data = np.frombuffer(r.content, dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)

        if image is None:
            BACKDROP_CACHE[url] = result
            return result

        height, width = image.shape[:2]
        result["width"] = width
        result["height"] = height

        # Reject images that are too small for a large Hero.
        if width < 1000 or height < 500:
            result["score"] -= 80

        # Strong preference for proper cinematic landscape images.
        ratio = width / height if height else 0
        if 1.68 <= ratio <= 1.86:
            result["score"] += 18
        elif ratio < 1.55:
            result["score"] -= 35

        # Downscale for inexpensive visual analysis.
        scale = min(1.0, 900 / max(width, 1))
        if scale < 1:
            small = cv2.resize(
                image,
                (
                    max(1, int(width * scale)),
                    max(1, int(height * scale)),
                ),
                interpolation=cv2.INTER_AREA,
            )
        else:
            small = image

        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        # Sharpness: very low Laplacian variance often means a soft/blurry image.
        sharpness = float(
            cv2.Laplacian(gray, cv2.CV_64F).var()
        )

        if sharpness >= 180:
            result["score"] += 16
        elif sharpness >= 90:
            result["score"] += 8
        elif sharpness < 40:
            result["score"] -= 28
        elif sharpness < 65:
            result["score"] -= 14

        h, w = gray.shape[:2]

        # Title-safe zone: left side, avoiding the extreme top and bottom.
        # Nuvio can place title/logo information here, so calmer imagery
        # and fewer faces are preferred.
        x2 = int(w * 0.58)
        y1 = int(h * 0.14)
        y2 = int(h * 0.68)

        safe = gray[y1:y2, :x2]

        if safe.size:
            # Edge density is a useful proxy for visual clutter.
            edges = cv2.Canny(safe, 80, 160)
            edge_density = float(
                cv2.countNonZero(edges) / edges.size
            )

            if edge_density < 0.07:
                result["score"] += 16
            elif edge_density < 0.12:
                result["score"] += 8
            elif edge_density > 0.22:
                result["score"] -= 18
            elif edge_density > 0.17:
                result["score"] -= 9

            # Extreme local contrast can make title/logo readability worse.
            brightness_std = float(safe.std())

            if brightness_std < 38:
                result["score"] += 12
            elif brightness_std > 72:
                result["score"] -= 12

        # Face detection.
        # Faces are not forbidden; we only penalize poor Hero placement.
        try:
            cascade_path = cv2.data.haarcascades + (
                "haarcascade_frontalface_default.xml"
            )
            face_detector = cv2.CascadeClassifier(cascade_path)

            faces = face_detector.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=5,
                minSize=(
                    max(24, int(w * 0.035)),
                    max(24, int(h * 0.035)),
                ),
            )

            for (fx, fy, fw, fh) in faces:
                cx = fx + fw / 2
                cy = fy + fh / 2

                # Face touching an edge is bad for Hero cropping.
                if (
                    fx < w * 0.045
                    or fy < h * 0.045
                    or fx + fw > w * 0.955
                    or fy + fh > h * 0.955
                ):
                    result["score"] -= 12

                # Large face in the title-safe zone is strongly discouraged.
                if (
                    cx < w * 0.58
                    and y1 <= cy <= y2
                ):
                    area_ratio = (
                        (fw * fh) / max(w * h, 1)
                    )

                    if area_ratio > 0.16:
                        result["score"] -= 24
                    elif area_ratio > 0.08:
                        result["score"] -= 14
                    else:
                        result["score"] -= 6

        except Exception:
            pass

        # MSER text-like regions: this helps penalize backdrops with
        # prominent baked-in typography without requiring OCR packages.
        try:
            mser = cv2.MSER_create()
            regions, _ = mser.detectRegions(gray)

            text_like = 0

            for pts in regions[:250]:
                x, y, rw, rh = cv2.boundingRect(pts)

                if (
                    x < w * 0.60
                    and y > h * 0.08
                    and y < h * 0.78
                    and rw >= 8
                    and rh >= 5
                    and rw / max(rh, 1) >= 1.2
                    and rw / max(rh, 1) <= 18
                ):
                    text_like += 1

            if text_like > 45:
                result["score"] -= 18
            elif text_like > 28:
                result["score"] -= 10
            elif text_like < 12:
                result["score"] += 5

        except Exception:
            pass

    except Exception as e:
        print("visual backdrop analysis failed:", e)

    BACKDROP_CACHE[url] = result
    return result


def choose_backdrop(d):

    images = d.get("images") or {}
    backdrops = images.get("backdrops") or []

    if not backdrops:
        if d.get("backdrop_path"):
            return img(
                d["backdrop_path"],
                "w1920"
            )

        return None

    candidates = []

    for x in backdrops:
        path = x.get("file_path")
        if not path:
            continue

        aspect_ratio = float(
            x.get("aspect_ratio") or 0
        )

        width = int(
            x.get("width") or 0
        )

        height = int(
            x.get("height") or 0
        )

        # Backdrop should be genuinely landscape.
        if width < 1280 or height < 700:
            continue

        if aspect_ratio < 1.60 or aspect_ratio > 1.90:
            continue

        language = x.get("iso_639_1")
        vote_average = float(
            x.get("vote_average") or 0
        )
        vote_count = int(
            x.get("vote_count") or 0
        )

        score_value = vote_average * 7

        score_value += min(
            vote_count,
            100
        ) * 0.08

        # Strong preference for clean, neutral backdrops.
        if language is None:
            score_value += 16
        elif language == "en":
            score_value += 3
        elif language in {"ko", "ar"}:
            score_value -= 2
        else:
            score_value -= 5

        if width >= 1920:
            score_value += 15
        elif width >= 1600:
            score_value += 9
        else:
            score_value += 3

        if 1.70 <= aspect_ratio <= 1.82:
            score_value += 12

        # Analyze only the strongest metadata candidates first.
        candidates.append(
            (
                score_value,
                x
            )
        )

    if not candidates:
        if d.get("backdrop_path"):
            return img(
                d["backdrop_path"],
                "w1920"
            )

        return None

    candidates.sort(
        key=lambda item: item[0],
        reverse=True
    )

    # Visual analysis is intentionally limited to the top candidates
    # to keep GitHub Actions fast and avoid unnecessary downloads.
    visual_candidates = candidates[:8]

    best = None
    best_score = -10**9

    for metadata_score, x in visual_candidates:

        url = img(
            x.get("file_path"),
            "w1280"
        )

        visual = analyze_backdrop(url)

        final_score = (
            metadata_score
            + visual["score"]
        )

        print(
            "backdrop candidate:",
            x.get("file_path"),
            f"metadata={metadata_score:.1f}",
            f"visual={visual['score']:.1f}",
            f"final={final_score:.1f}",
        )

        if final_score > best_score:
            best_score = final_score
            best = x

    if not best:
        best = candidates[0][1]

    return img(
        best.get("file_path"),
        "w1920"
    )


# ============================================================
# Logo selection
# ============================================================

def choose_logo(d):

    images = d.get("images") or {}
    logos = images.get("logos") or []

    if not logos:
        return None

    def score(x):

        language = x.get("iso_639_1")

        width = int(
            x.get("width") or 0
        )

        vote_average = float(
            x.get("vote_average") or 0
        )

        score_value = vote_average * 6

        score_value += min(
            width,
            2000
        ) / 500

        # Requested language priority:
        # Arabic → English → Korean → neutral.
        if language == "ar":
            score_value += 20
        elif language == "en":
            score_value += 16
        elif language == "ko":
            score_value += 10
        elif language is None:
            score_value += 5

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

    # --------------------------------------------------------
    # Arabic localization
    # --------------------------------------------------------

    arabic_name = None
    arabic_overview = None

    try:
        ar = tmdb(
            f"/tv/{tid}",
            {
                "language": "ar-SA"
            }
        )

        arabic_name = (
            ar.get("name")
            or None
        )

        arabic_overview = (
            ar.get("overview")
            or None
        )

    except Exception as e:
        print(
            "Arabic localization failed",
            tid,
            e
        )

    # Title priority:
    # Arabic → English → original Korean.
    title = (
        arabic_name
        or d.get("name")
        or d.get("original_name")
        or "Unknown"
    )

    description = (
        arabic_overview
        or d.get("overview")
        or ""
    )

    return {

        "id": f"krtmdb_{tid}",

        "type": "series",

        "name": title,

        "poster":
            img(
                d.get("poster_path"),
                "w500"
            ),

        "posterShape":
            "poster",

        # Clean Hero background.
        "background":
            choose_backdrop(d),

        # Separate transparent title logo.
        "logo":
            choose_logo(d),

        "description":
            description,

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
        "1.2.0",

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
